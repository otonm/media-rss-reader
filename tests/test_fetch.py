"""Tests for the shared upstream-fetch path used by the proxy and the prefetcher."""

from pathlib import Path

import aiosqlite
import httpx
import pytest
import respx

from src.media import cache as cache_mod
from src.media.cache import cache_read, cache_read_meta, download_claim
from src.media.fetch import NonMediaUpstreamError, UpstreamError, fetch_to_cache, open_upstream, tee_to_cache

URL = "http://example.com/photo.jpg"
PAYLOAD = b"x" * 200_000  # several 64 KiB chunks, so a stream can be abandoned mid-way


def _tmp_files(d: Path) -> list[Path]:
    return [p for p in d.iterdir() if p.suffix == ".tmp"]


async def test_tee_streams_bytes_and_fills_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, content=PAYLOAD, headers={"content-type": "image/jpeg"}))
        async with httpx.AsyncClient() as client:
            response = await open_upstream(URL, None, client)
            received = b"".join([chunk async for chunk in tee_to_cache(URL, response)])

    assert received == PAYLOAD
    path = cache_read(URL)
    assert path is not None
    assert path.read_bytes() == PAYLOAD
    assert cache_read_meta(URL) == "image/jpeg"
    assert _tmp_files(tmp_path) == []


async def test_abandoned_stream_leaves_no_cache_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A browser that scrolls past mid-download must not leave a partial file cached.

    Half a file is worse than no file: cache_read would report a hit forever
    after, and the truncated bytes fail to decode every single time.
    """
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, content=PAYLOAD, headers={"content-type": "image/jpeg"}))
        async with httpx.AsyncClient() as client:
            response = await open_upstream(URL, None, client)
            stream = tee_to_cache(URL, response)
            first = await anext(stream)
            assert len(first) < len(PAYLOAD)  # genuinely mid-transfer
            await stream.aclose()  # the client went away

    assert cache_read(URL) is None
    assert _tmp_files(tmp_path) == []


async def test_open_upstream_marks_dead_on_error_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db: aiosqlite.Connection
) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(404))
        async with httpx.AsyncClient() as client:
            with pytest.raises(UpstreamError):
                await open_upstream(URL, None, client)

    async with db.execute("SELECT url FROM dead_urls") as cur:
        assert [r[0] for r in await cur.fetchall()] == [URL]


async def test_fetch_to_cache_skips_url_already_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The prefetcher must not re-download what someone else is already pulling.

    prefetch_ahead fires on every scroll event over overlapping windows, and
    those windows overlap what the browser is fetching through the proxy, so
    without this guard the same URL is pulled from the origin several times at
    once.
    """
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    with respx.mock:
        route = respx.get(URL).mock(return_value=httpx.Response(200, content=PAYLOAD))
        async with httpx.AsyncClient() as client:
            with download_claim(URL):  # someone else is downloading it
                await fetch_to_cache(URL, "item-1", client)

    assert route.call_count == 0
    assert cache_read(URL) is None


async def test_open_upstream_refuses_non_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HTML served for a media URL must be rejected, not streamed or cached (F5)."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, content=b"<html/>", headers={"content-type": "text/html"}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(NonMediaUpstreamError):
                await open_upstream(URL, "item-1", client)

    assert cache_read(URL) is None
    assert _tmp_files(tmp_path) == []


async def test_fetch_to_cache_html_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: fetch_to_cache must not leave an HTML file in the cache (F5)."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, content=b"<html/>", headers={"content-type": "text/html"}))
        async with httpx.AsyncClient() as client:
            await fetch_to_cache(URL, "item-1", client)  # never raises

    assert cache_read(URL) is None
    assert _tmp_files(tmp_path) == []
