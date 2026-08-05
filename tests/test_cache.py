import asyncio
import os
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


async def test_concurrent_writes_same_url_keep_entry_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two writers racing on one URL must leave a complete file and its sidecar.

    This is the normal case, not an edge case: the browser's proxy GET for an
    item routinely overlaps the background _warm task for the same URL. With a
    shared .tmp name the second writer's open("wb") truncates the first's
    in-flight file, and the loser's rename failure used to unlink the winner's
    sidecar -- leaving a cache hit that the proxy serves as text/plain.
    """
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "https://example.com/big.jpg"
    payload = b"A" * 4096 + b"B" * 4096
    resumed = asyncio.Event()

    async def slow_chunks() -> AsyncGenerator[bytes]:
        yield payload[:4096]
        await resumed.wait()  # the other writer completes while we hold the fd
        yield payload[4096:]

    async def fast_chunks() -> AsyncGenerator[bytes]:
        yield payload
        resumed.set()

    await asyncio.gather(
        cache_mod.cache_stream_write(url, slow_chunks(), "image/jpeg"),
        cache_mod.cache_stream_write(url, fast_chunks(), "image/jpeg"),
    )

    path = cache_mod.cache_read(url)
    assert path is not None
    assert path.read_bytes() == payload
    assert cache_mod.cache_read_meta(url) == "image/jpeg"
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir())  # noqa: ASYNC240


async def test_evict_ignores_inflight_tmp_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A .tmp file is an in-flight download, not a cache entry.

    Counting it toward cache_max_items evicts a real entry too early, and
    unlinking it mid-download breaks the writer that owns it.
    """
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(cache_mod.settings, "cache_max_items", 2)
    monkeypatch.setattr(cache_mod.settings, "cache_max_age_hours", 0)
    inflight = tmp_path / "abc123.tmp"
    inflight.write_bytes(b"partial")
    await _write("https://example.com/keep.gif", b"x", "image/gif")

    await cache_mod.evict()

    assert inflight.exists()


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


async def test_evict_drops_oldest_until_under_byte_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R7: eviction counted files, never bytes, so 500 multi-gigabyte entries
    stayed under the limit while filling the volume."""
    import time

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(cache_mod.settings, "cache_max_items", 500)
    monkeypatch.setattr(cache_mod.settings, "cache_max_age_hours", 48)
    monkeypatch.setattr(cache_mod.settings, "cache_max_bytes", 1000)

    now = time.time()
    for n, size in enumerate([600, 600, 600]):
        f = tmp_path / f"file{n}"
        f.write_bytes(b"x" * size)
        os.utime(f, (now - (10 - n), now - (10 - n)))

    await cache_mod.evict()

    remaining = sorted(p.name for p in tmp_path.iterdir())  # noqa: ASYNC240
    assert remaining == ["file2"]


def test_cache_name_matches_cache_path_name() -> None:
    import hashlib

    from src.media.cache import _cache_path, cache_name

    url = "http://example.com/x.jpg"
    assert cache_name(url) == _cache_path(url).name
    assert cache_name(url) == hashlib.sha256(url.encode()).hexdigest()


def test_cache_name_is_the_single_source() -> None:
    from src.media.cache import _cache_path, cache_name

    url = "http://example.com/y.png"
    assert cache_name(url) == _cache_path(url).name


def test_cache_present_names_excludes_meta_and_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The `cached` hint on /api/items is a membership test on a bare sha256
    name, so adding a .tmp or .meta entry to the returned set can never flip
    it — deleting the suffix filter here kept that test green. This is the
    only place the exclusion is observable.
    """
    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    data_name = cache_mod.cache_name("http://example.com/warm.jpg")
    (tmp_path / data_name).write_bytes(b"x")
    (tmp_path / f"{data_name}.meta").write_text("image/jpeg")
    (tmp_path / "abc123.tmp").write_bytes(b"partial")

    assert cache_mod.cache_present_names() == {data_name}


def test_cache_lookup_returns_path_and_type_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three blocking filesystem calls per proxy request — cache_read's
    Path.exists, the route's Path.stat, and cache_read_meta's Path.exists plus
    read_text — become one offload. Two of them sat behind helpers where Ruff's
    ASYNC rules cannot see them.

    The stat is not returned: handing one to FileResponse suppresses Starlette's
    own os.stat, which is the check that catches an eviction before any bytes go
    out (R2). Returning it invited exactly that call.
    """
    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/warm.jpg"
    (tmp_path / cache_mod.cache_name(url)).write_bytes(b"hello")
    (tmp_path / f"{cache_mod.cache_name(url)}.meta").write_text("image/jpeg")

    hit = cache_mod.cache_lookup(url)
    assert hit is not None
    path, media_type = hit
    assert path.name == cache_mod.cache_name(url)
    assert media_type == "image/jpeg"


def test_cache_lookup_misses_without_the_data_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    assert cache_mod.cache_lookup("http://example.com/never.jpg") is None


def test_cache_lookup_defaults_the_type_without_a_meta_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/typeless.bin"
    (tmp_path / cache_mod.cache_name(url)).write_bytes(b"x")
    hit = cache_mod.cache_lookup(url)
    assert hit is not None
    assert hit[1] == "application/octet-stream"
