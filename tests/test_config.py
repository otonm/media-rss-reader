import pytest

from src.config import Settings, settings


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
    from src.config import _load_settings

    s = _load_settings()
    assert s.port == 9090
    assert s.log_level == "debug"


def test_ui_debug_defaults_off_and_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().ui_debug == 0
    monkeypatch.setenv("UI_DEBUG", "1")
    from src.config import _load_settings

    assert _load_settings().ui_debug == 1


def test_auth_settings_defaults() -> None:
    assert settings.auth_lockout_attempts == 5
    assert settings.auth_lockout_minutes == 15


def test_auth_settings_are_present() -> None:
    assert hasattr(settings, "auth_username")
    assert hasattr(settings, "auth_password")
    assert hasattr(settings, "auth_secret_key")
