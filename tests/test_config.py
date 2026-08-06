import os
from collections.abc import Iterator

import pytest

from src.config import Settings, _load_settings, settings


def test_settings_defaults() -> None:
    s = Settings()
    assert s.port == 8080
    assert s.log_level == "info"
    assert s.feed_initial_count == 10
    assert s.cache_max_items == 500
    assert s.opml_sync_interval == 3600


def test_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "9090")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    s = _load_settings()
    assert s.port == 9090
    assert s.log_level == "debug"


def test_ui_debug_defaults_off_and_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().ui_debug == 0
    monkeypatch.setenv("UI_DEBUG", "1")
    assert _load_settings().ui_debug == 1


def test_auth_settings_defaults() -> None:
    assert settings.auth_lockout_attempts == 5
    assert settings.auth_lockout_minutes == 15


def test_auth_settings_are_present() -> None:
    assert hasattr(settings, "auth_username")
    assert hasattr(settings, "auth_password")
    assert hasattr(settings, "auth_secret_key")


@pytest.fixture
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in list(os.environ):
        if key.startswith("AUTH_") or key in ("DB_PATH", "CACHE_DIR", "OPML_PATH", "FEEDS_DIR"):
            monkeypatch.delenv(key, raising=False)
    yield None


def test_empty_auth_secret_key_raises(monkeypatch: pytest.MonkeyPatch, _clean_env: None) -> None:
    monkeypatch.setenv("AUTH_USERNAME", "u")
    monkeypatch.setenv("AUTH_PASSWORD", "p")
    monkeypatch.delenv("AUTH_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="AUTH_SECRET_KEY"):
        _load_settings()


def test_set_auth_secret_key_loads(monkeypatch: pytest.MonkeyPatch, _clean_env: None) -> None:
    monkeypatch.setenv("AUTH_USERNAME", "u")
    monkeypatch.setenv("AUTH_PASSWORD", "p")
    monkeypatch.setenv("AUTH_SECRET_KEY", "x" * 32)
    s = _load_settings()
    assert s.auth_secret_key == "x" * 32


def test_one_empty_credential_raises(monkeypatch: pytest.MonkeyPatch, _clean_env: None) -> None:
    monkeypatch.setenv("AUTH_USERNAME", "u")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.setenv("AUTH_SECRET_KEY", "x" * 32)
    with pytest.raises(RuntimeError, match="AUTH_USERNAME and AUTH_PASSWORD"):
        _load_settings()


def test_both_credentials_empty_raises(monkeypatch: pytest.MonkeyPatch, _clean_env: None) -> None:
    # Both-empty is not a safe "no-auth mode": /login then redirects to /setup
    # with a setup cookie and any visitor can complete setup to become admin.
    monkeypatch.delenv("AUTH_USERNAME", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.setenv("AUTH_SECRET_KEY", "x" * 32)
    with pytest.raises(RuntimeError, match="AUTH_USERNAME and AUTH_PASSWORD"):
        _load_settings()


def test_feed_initial_count_above_the_api_bound_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch, _clean_env: None
) -> None:
    """The browser sends size=FEED_INITIAL_COUNT and /api/items caps size at
    200, in files that cannot see each other. Above 200 every request 422s
    before list_items runs: the feed renders empty, item-store.js retries
    forever because `if (!resp.ok) return` leaves hasMore true, and nothing
    logs it (M1)."""
    monkeypatch.setenv("AUTH_USERNAME", "u")
    monkeypatch.setenv("AUTH_PASSWORD", "p")
    monkeypatch.setenv("AUTH_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("FEED_INITIAL_COUNT", "250")
    with pytest.raises(RuntimeError, match="FEED_INITIAL_COUNT"):
        _load_settings()


def test_feed_initial_count_at_the_bound_is_accepted(monkeypatch: pytest.MonkeyPatch, _clean_env: None) -> None:
    monkeypatch.setenv("AUTH_USERNAME", "u")
    monkeypatch.setenv("AUTH_PASSWORD", "p")
    monkeypatch.setenv("AUTH_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("FEED_INITIAL_COUNT", "200")
    assert _load_settings().feed_initial_count == 200
