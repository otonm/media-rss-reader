import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from src.media import cache as cache_mod


async def _write(url: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    async def chunks() -> AsyncGenerator[bytes]:
        yield data

    await cache_mod.cache_stream_write(url, chunks(), content_type)


async def test_write_and_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    await _write("https://example.com/img.jpg", b"bytes", "image/jpeg")
    path = cache_mod.cache_read("https://example.com/img.jpg")
    assert path is not None
    assert path.read_bytes() == b"bytes"


async def test_read_miss_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    assert cache_mod.cache_read("https://example.com/missing.jpg") is None


async def test_write_records_content_type_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    await _write("https://example.com/anim.gif", b"GIF89a", "image/gif")
    assert cache_mod.cache_read_meta("https://example.com/anim.gif") == "image/gif"


async def test_read_meta_miss_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    assert cache_mod.cache_read_meta("https://example.com/missing.gif") is None


async def test_stream_write_records_content_type_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    async def chunks() -> bytes:
        for b in (b"GIF", b"89a"):
            yield b

    await cache_mod.cache_stream_write("https://example.com/anim.gif", chunks(), "image/gif")
    path = cache_mod.cache_read("https://example.com/anim.gif")
    assert path is not None
    assert path.read_bytes() == b"GIF89a"
    assert cache_mod.cache_read_meta("https://example.com/anim.gif") == "image/gif"


async def test_stream_write_failure_cleans_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    async def bad_chunks() -> bytes:
        raise RuntimeError("boom")
        yield b""  # pragma: no cover

    with pytest.raises(RuntimeError, match="boom"):
        await cache_mod.cache_stream_write("https://example.com/bad.gif", bad_chunks(), "image/gif")
    assert cache_mod.cache_read("https://example.com/bad.gif") is None
    assert cache_mod.cache_read_meta("https://example.com/bad.gif") is None
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir())  # noqa: ASYNC240


async def test_evict_by_count_removes_sidecars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(cache_mod.settings, "cache_max_items", 2)
    monkeypatch.setattr(cache_mod.settings, "cache_max_age_hours", 9999)
    for i in range(3):
        await _write(f"https://example.com/{i}.gif", b"x", "image/gif")
        await asyncio.sleep(0.01)
    await cache_mod.evict()
    remaining = list(tmp_path.iterdir())  # noqa: ASYNC240
    assert len(remaining) == 4
    data_files = [p for p in remaining if p.suffix != ".meta"]
    assert len(data_files) == 2


async def test_evict_by_age_removes_sidecars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(cache_mod.settings, "cache_max_items", 9999)
    monkeypatch.setattr(cache_mod.settings, "cache_max_age_hours", 0)
    await _write("https://example.com/stale.gif", b"x", "image/gif")
    await cache_mod.evict()
    assert list(tmp_path.iterdir()) == []  # noqa: ASYNC240


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
