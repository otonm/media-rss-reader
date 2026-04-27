import pytest

from src.config import Settings


def test_settings_defaults() -> None:
    s = Settings()
    assert s.port == 8080
    assert s.log_level == "info"
    assert s.prefetch_ahead == 5
    assert s.cache_max_items == 500
    assert s.opml_sync_interval == 3600


def test_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "9090")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    s = Settings()
    assert s.port == 9090
    assert s.log_level == "debug"
