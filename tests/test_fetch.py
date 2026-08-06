"""Tests for the shared upstream-fetch path used by the proxy and the prefetcher."""

import logging
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


def _pinned(url: str) -> str:
    """The url as open_upstream now sends it: host replaced by the stubbed IP."""
    return fetch_mod._pinned_url(url, "93.184.216.34")


async def test_tee_streams_bytes_and_fills_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    with respx.mock:
        respx.get(_pinned(URL)).mock(
            return_value=httpx.Response(200, content=PAYLOAD, headers={"content-type": "image/jpeg"})
        )
        async with httpx.AsyncClient() as client:
            response, content_type = await open_upstream(URL, None, client)
            received = b"".join([chunk async for chunk in tee_to_cache(URL, response, content_type)])

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
        respx.get(_pinned(URL)).mock(
            return_value=httpx.Response(200, content=PAYLOAD, headers={"content-type": "image/jpeg"})
        )
        async with httpx.AsyncClient() as client:
            response, content_type = await open_upstream(URL, None, client)
            stream = tee_to_cache(URL, response, content_type)
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
        respx.get(_pinned(URL)).mock(return_value=httpx.Response(404))
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
        route = respx.get(_pinned(URL)).mock(return_value=httpx.Response(200, content=PAYLOAD))
        async with httpx.AsyncClient() as client:
            with download_claim(URL):  # someone else is downloading it
                await fetch_to_cache(URL, "item-1", client)

    assert route.call_count == 0
    assert cache_read(URL) is None


async def test_open_upstream_refuses_non_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HTML served for a media URL must be rejected, not streamed or cached (F5)."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    with respx.mock:
        respx.get(_pinned(URL)).mock(
            return_value=httpx.Response(200, content=b"<html/>", headers={"content-type": "text/html"})
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(NonMediaUpstreamError):
                await open_upstream(URL, "item-1", client)

    assert cache_read(URL) is None
    assert _tmp_files(tmp_path) == []


async def test_fetch_to_cache_html_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: fetch_to_cache must not leave an HTML file in the cache (F5)."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    with respx.mock:
        respx.get(_pinned(URL)).mock(
            return_value=httpx.Response(200, content=b"<html/>", headers={"content-type": "text/html"})
        )
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
        respx.get(_pinned(url)).mock(side_effect=handler)
        async with httpx.AsyncClient() as client:
            await asyncio.gather(
                fetch_to_cache(url, "i1", client),
                fetch_to_cache(url, "i1", client),
            )

    assert calls == 1, f"expected 1 upstream GET, got {calls}"


async def test_a_failing_warm_is_visible_without_debug(
    db: aiosqlite.Connection,
    mock_http: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every outcome log on the warm path was DEBUG, so a prefetcher failing
    100% of the time is indistinguishable from one warming every item — and the
    endpoint answers {"status": "ok"} either way (M6)."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    caplog.set_level(logging.INFO)
    url = "http://example.com/broken.jpg"
    mock_http.get(_pinned(url)).mock(side_effect=RuntimeError("boom"))

    async with httpx.AsyncClient() as c:
        await fetch_to_cache(url, "item-1", c)

    assert any(r.levelno >= logging.WARNING for r in caplog.records), "a broken prefetcher must be visible"


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
    respx.get(_pinned("http://example.com/x.jpg")).mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1:9090/status"})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamError):
            await open_upstream("http://example.com/x.jpg", None, client)


@respx.mock
async def test_open_upstream_follows_public_redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The manual redirect loop must still follow an ordinary CDN redirect."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    # Both example.com and cdn.example.com pin to the same stubbed IP, so the
    # routes are told apart by the Host header the request carries.
    respx.get(_pinned("http://example.com/x.jpg"), headers={"Host": "example.com"}).mock(
        return_value=httpx.Response(302, headers={"location": "http://cdn.example.com/x.jpg"})
    )
    respx.get(_pinned("http://cdn.example.com/x.jpg"), headers={"Host": "cdn.example.com"}).mock(
        return_value=httpx.Response(200, content=b"bytes", headers={"content-type": "image/jpeg"})
    )
    async with httpx.AsyncClient() as client:
        response, _ = await open_upstream("http://example.com/x.jpg", None, client)
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
            response, _ = await open_upstream("http://10.0.0.5/x.jpg", None, client)
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

    respx.get(_pinned(url)).mock(return_value=httpx.Response(429))
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
    respx.get(_pinned(url)).mock(
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
    respx.get(_pinned(url)).mock(
        return_value=httpx.Response(200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(b"x" * 1000))
    )
    async with httpx.AsyncClient() as client:
        response, content_type = await open_upstream(url, None, client)
        with pytest.raises(UpstreamError):
            async for _ in tee_to_cache(url, response, content_type):
                pass
    assert list(tmp_path.iterdir()) == []  # noqa: ASYNC240


@respx.mock
async def test_open_upstream_refuses_svg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R8: an SVG is an active document. Served from /api/media/proxy it runs
    its <script> on the app's own origin, with the session cookie attached."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/evil.svg"
    respx.get(_pinned(url)).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/svg+xml"}, content=b"<svg><script>x</script></svg>"
        )
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NonMediaUpstreamError):
            await open_upstream(url, None, client)


async def test_open_upstream_refuses_dns_rebinding(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host whose first resolution is public and second is private must be
    refused: the SSRF guard must not be TOCTOU-vulnerable to DNS rebinding."""
    import httpx
    import respx

    import src.media.fetch as fetch_mod

    calls = {"n": 0}

    def _rebinding_resolve(host: str) -> list[str]:
        calls["n"] += 1
        # _check_url resolves first (public), httpx would resolve again (private).
        return ["93.184.216.34"] if calls["n"] == 1 else ["169.254.169.254"]

    monkeypatch.setattr(fetch_mod, "_resolve", _rebinding_resolve)
    monkeypatch.setattr(fetch_mod.settings, "allow_private_media_hosts", 0)

    url = "http://rebind.example.com/x.jpg"
    seen_hosts: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        return httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"})

    with respx.mock:
        respx.get(fetch_mod._pinned_url(url, "93.184.216.34")).mock(side_effect=_handler)
        async with httpx.AsyncClient() as client:
            resp, _ = await fetch_mod.open_upstream(url, None, client)
            assert resp.status_code == 200
            await resp.aclose()
    # The request must go to the one validated IP, not to the hostname that
    # (in an un-mocked world) would next resolve to a link-local address.
    assert calls["n"] == 1, "open_upstream must not let httpx re-resolve the host"
    assert seen_hosts == ["93.184.216.34"], f"request went to {seen_hosts}"


async def test_tee_to_cache_server_abort_logs_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A byte-budget abort is a lossy event: the client sees a truncated file.

    It must log at WARNING and not be mislabelled as a client disconnect.
    """
    import httpx
    import respx

    import src.media.cache as cache_mod
    from src.media import fetch as fetch_mod

    monkeypatch_fetch = pytest.MonkeyPatch()
    monkeypatch_fetch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    monkeypatch_fetch.setattr(fetch_mod.settings, "media_max_bytes", 4)
    try:
        url = "http://example.com/big.jpg"
        body = b"0123456789"
        with respx.mock:
            # No Content-Length: open_upstream cannot pre-trip on the declared
            # size, so the running budget inside tee_to_cache is what aborts.
            respx.get(_pinned(url)).mock(
                return_value=httpx.Response(
                    200,
                    headers={"content-type": "image/jpeg"},
                    stream=httpx.ByteStream(body),
                )
            )
            async with httpx.AsyncClient() as client:
                resp, content_type = await fetch_mod.open_upstream(url, None, client)
                caplog.set_level(logging.DEBUG)
                with pytest.raises(fetch_mod.UpstreamError, match="MEDIA_MAX_BYTES"):
                    async for _ in fetch_mod.tee_to_cache(url, resp, content_type):
                        pass
    finally:
        monkeypatch_fetch.undo()

    assert any(r.levelno == logging.WARNING and "server aborted" in r.getMessage() for r in caplog.records), (
        "a size-check abort must log at WARNING, not be mislabelled a client disconnect"
    )


async def test_tee_to_cache_client_disconnect_logs_debug(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A real client disconnect is expected and stays at DEBUG."""
    import httpx
    import respx

    import src.media.cache as cache_mod
    from src.media import fetch as fetch_mod

    monkeypatch_fetch = pytest.MonkeyPatch()
    monkeypatch_fetch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    monkeypatch_fetch.setattr(fetch_mod.settings, "media_max_bytes", 0)
    try:
        url = "http://example.com/stream.jpg"
        with respx.mock:
            respx.get(_pinned(url)).mock(
                return_value=httpx.Response(200, content=b"abcdefghij", headers={"content-type": "image/jpeg"})
            )
            async with httpx.AsyncClient() as client:
                resp, content_type = await fetch_mod.open_upstream(url, None, client)
                caplog.set_level(logging.DEBUG)
                gen = fetch_mod.tee_to_cache(url, resp, content_type)
                await gen.__anext__()  # pull one chunk then abandon
                await gen.aclose()
    finally:
        monkeypatch_fetch.undo()

    assert any(r.levelno == logging.DEBUG and "client stopped reading" in r.getMessage() for r in caplog.records)


async def test_tee_to_cache_non_client_abort_not_mislabeled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A cache-side failure (disk full, write error) raised after the first
    chunk must NOT fall through to the "client stopped reading" debug log —
    T5 only covered the byte-budget path. Regression guard."""
    import collections.abc

    import httpx
    import respx

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    # Wrap the bound cache_stream_tee in fetch_mod so the second yield raises.
    orig = fetch_mod.cache_stream_tee

    async def _failing_tee(
        url: str, chunks: collections.abc.AsyncIterable[bytes], content_type: str = "application/octet-stream"
    ) -> None:
        yielded = 0
        async for chunk in orig(url, chunks, content_type):
            yielded += 1
            yield chunk
            if yielded >= 1:
                raise OSError("simulated cache write failure")

    monkeypatch.setattr(fetch_mod, "cache_stream_tee", _failing_tee)

    url = "http://example.com/boomboom.jpg"
    with respx.mock:
        respx.get(_pinned(url)).mock(
            return_value=httpx.Response(200, content=b"x" * 4096, headers={"content-type": "image/jpeg"})
        )
        caplog.set_level(logging.DEBUG)
        async with httpx.AsyncClient() as client:
            resp, content_type = await fetch_mod.open_upstream(url, None, client)
            with pytest.raises(OSError, match="simulated cache write failure"):
                async for _ in fetch_mod.tee_to_cache(url, resp, content_type):
                    pass

    assert any(r.levelno == logging.WARNING and "aborted" in r.getMessage() for r in caplog.records), (
        "cache-side abort must log at WARNING"
    )
    assert not any(r.levelno == logging.DEBUG and "client stopped reading" in r.getMessage() for r in caplog.records), (
        "non-client abort must NOT be mislabelled as a client disconnect"
    )
