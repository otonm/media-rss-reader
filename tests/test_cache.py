import asyncio
from pathlib import Path

import pytest

from src.media import cache as cache_mod


async def test_write_and_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    await cache_mod.cache_write("https://example.com/img.jpg", b"bytes")
    path = cache_mod.cache_read("https://example.com/img.jpg")
    assert path is not None
    assert path.read_bytes() == b"bytes"


async def test_read_miss_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    assert cache_mod.cache_read("https://example.com/missing.jpg") is None


async def test_evict_by_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(cache_mod.settings, "cache_max_items", 2)
    monkeypatch.setattr(cache_mod.settings, "cache_max_age_hours", 9999)
    for i in range(3):
        (tmp_path / f"file{i}").write_bytes(b"x")
        await asyncio.sleep(0.01)
    await cache_mod.evict()
    assert len(list(tmp_path.iterdir())) == 2  # noqa: ASYNC240


async def test_evict_by_age(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(cache_mod.settings, "cache_max_items", 9999)
    monkeypatch.setattr(cache_mod.settings, "cache_max_age_hours", 0)
    (tmp_path / "stale").write_bytes(b"x")
    await cache_mod.evict()
    assert len(list(tmp_path.iterdir())) == 0  # noqa: ASYNC240


async def test_evict_nonexistent_dir_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", "/nonexistent/cache")
    await cache_mod.evict()  # must not raise
