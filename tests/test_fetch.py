"""Tests for the shared upstream-fetch path used by the proxy and the prefetcher."""

from pathlib import Path

import aiosqlite
import httpx
import pytest
import respx

from src.media import cache as cache_mod
from src.media import fetch as fetch_mod
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


async def test_fetch_to_cache_dedupes_concurrent_same_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F19: two concurrent fetch_to_cache for the same URL must issue one
    upstream GET. Before the fix, the outer claim released before open_upstream,
    so both passed the check and both pulled the origin."""
    import asyncio

    import httpx
    import respx

    import src.media.cache as cache_mod
    from src.media.fetch import fetch_to_cache

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/dup.jpg"
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, content=b"bytes", headers={"content-type": "image/jpeg"})

    with respx.mock:
        respx.get(url).mock(side_effect=handler)
        async with httpx.AsyncClient() as client:
            await asyncio.gather(
                fetch_to_cache(url, "i1", client),
                fetch_to_cache(url, "i1", client),
            )

    assert calls == 1, f"expected 1 upstream GET, got {calls}"


async def test_open_upstream_refuses_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R1 (blocker): the proxy and the prefetcher both reach this function with
    URLs from third-party feeds. A loopback target reaches anything on the
    Docker network."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamError):
            await open_upstream("http://127.0.0.1:9090/status", None, client)


async def test_open_upstream_refuses_link_local_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamError):
            await open_upstream("http://169.254.169.254/latest/meta-data/", None, client)


async def test_open_upstream_refuses_non_http_scheme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamError):
            await open_upstream("file:///etc/passwd", None, client)


async def test_open_upstream_refuses_hostname_resolving_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public-looking hostname that resolves into RFC1918 must be refused."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr("src.media.fetch._resolve", lambda host: ["10.0.0.5"])
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamError):
            await open_upstream("http://evil.example.com/x.jpg", None, client)


@respx.mock
async def test_open_upstream_refuses_redirect_into_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """follow_redirects used to be True, so a public host could bounce the
    fetch into the Docker network on the second hop."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    respx.get("http://example.com/x.jpg").mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1:9090/status"})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamError):
            await open_upstream("http://example.com/x.jpg", None, client)


@respx.mock
async def test_open_upstream_follows_public_redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The manual redirect loop must still follow an ordinary CDN redirect."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    respx.get("http://example.com/x.jpg").mock(
        return_value=httpx.Response(302, headers={"location": "http://cdn.example.com/x.jpg"})
    )
    respx.get("http://cdn.example.com/x.jpg").mock(
        return_value=httpx.Response(200, content=b"bytes", headers={"content-type": "image/jpeg"})
    )
    async with httpx.AsyncClient() as client:
        response = await open_upstream("http://example.com/x.jpg", None, client)
        assert response.status_code == 200
        await response.aclose()


async def test_open_upstream_allows_private_when_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ALLOW_PRIVATE_MEDIA_HOSTS=1 is the escape hatch for self-hosted media."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(fetch_mod.settings, "allow_private_media_hosts", 1)
    with respx.mock:
        respx.get("http://10.0.0.5/x.jpg").mock(
            return_value=httpx.Response(200, content=b"bytes", headers={"content-type": "image/jpeg"})
        )
        async with httpx.AsyncClient() as client:
            response = await open_upstream("http://10.0.0.5/x.jpg", None, client)
            assert response.status_code == 200
            await response.aclose()


@respx.mock
async def test_open_upstream_429_does_not_mark_dead(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5: a CDN rate-limiting a burst of <img> loads used to mark the URL dead
    and DELETE the item, tombstoning its guid so the next sync would not
    re-insert it. A 429 is transient; it must change nothing."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/busy.jpg"
    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/feed', 'F')")
    await db.execute(
        """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
           VALUES ('i1', 'f1', 'g1', 'T', ?, 'image', '2026-01-01T00:00:00')""",
        (url,),
    )
    await db.commit()

    respx.get(url).mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamError):
            await open_upstream(url, "i1", client)

    async with db.execute("SELECT COUNT(*) FROM dead_urls") as cur:
        assert (await cur.fetchone())[0] == 0
    async with db.execute("SELECT COUNT(*) FROM items WHERE id = 'i1'") as cur:
        assert (await cur.fetchone())[0] == 1


@respx.mock
async def test_open_upstream_rejects_oversized_content_length(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R7: Content-Length was logged and never checked."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(fetch_mod.settings, "media_max_bytes", 100)
    url = "http://example.com/huge.mp4"
    respx.get(url).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "video/mp4", "content-length": "999999"}, content=b"x"
        )
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamError):
            await open_upstream(url, None, client)


@respx.mock
async def test_tee_to_cache_aborts_past_the_byte_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R7: a server that declares no Content-Length could stream forever."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(fetch_mod.settings, "media_max_bytes", 10)
    url = "http://example.com/drip.mp4"
    # A streaming body with no Content-Length: the declared-length check in
    # open_upstream cannot trip, so the running budget in tee_to_cache is the
    # only thing that can stop it (the scenario the test names).
    respx.get(url).mock(
        return_value=httpx.Response(200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(b"x" * 1000))
    )
    async with httpx.AsyncClient() as client:
        response = await open_upstream(url, None, client)
        with pytest.raises(UpstreamError):
            async for _ in tee_to_cache(url, response):
                pass
    assert list(tmp_path.iterdir()) == []  # noqa: ASYNC240


@respx.mock
async def test_open_upstream_refuses_svg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R8: an SVG is an active document. Served from /api/media/proxy it runs
    its <script> on the app's own origin, with the session cookie attached."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/evil.svg"
    respx.get(url).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/svg+xml"}, content=b"<svg><script>x</script></svg>"
        )
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NonMediaUpstreamError):
            await open_upstream(url, None, client)
