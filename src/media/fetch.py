"""Upstream media fetching, shared by the proxy and the background prefetcher.

Both callers want the same thing: pull a URL from its origin into the disk
cache, record its content digest for dedup, and mark it dead if the origin
says it is gone. The only difference is that the proxy also needs the bytes
as they arrive, so the primitive here is a generator that yields each chunk
onward while writing it.

The proxy used to download the whole file to disk and only then reply, so the
browser saw nothing at all until the origin transfer finished — a full-screen
spinner for the entire download. Teeing the response means first byte out is
first byte in.

Every DB write here goes through run_with_own_db: a streaming response body
runs *after* the route function returned, by which point the request-scoped
connection is closed.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import aclosing
from urllib.parse import urlsplit

import httpx

from src.config import settings
from src.db.connection import run_with_own_db
from src.media.availability import mark_url_dead_and_maybe_drop
from src.media.cache import cache_stream_tee, download_claim
from src.media.dedup import record_media_hash

logger = logging.getLogger(__name__)

CHUNK_SIZE = 65536
# Per-operation httpx budget: connect, read, write and pool each get this.
UPSTREAM_TIMEOUT_S = 30
# Redirect hops the manual loop will follow. httpx's own follow_redirects is
# off here because each hop has to be re-validated (R1).
MAX_REDIRECTS = 5


class UpstreamError(Exception):
    """The origin refused the request. The URL has already been marked dead."""


class NonMediaUpstreamError(Exception):
    """The origin served a non-media content type; nothing cached, nothing marked dead.

    Distinct from UpstreamError because the URL is deliberately NOT marked
    dead here: a WAF page or transient HTML can flip back to media, and the
    item is not gone. Raised from open_upstream so the prefetch path cannot
    cache HTML and later serve it as a cache hit (F5).
    """


def _resolve(host: str) -> list[str]:
    """Return the IP addresses `host` resolves to. A literal IP resolves to itself.

    A module-level function so tests can replace it: the suite mocks HTTP
    transports, not name resolution.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return [info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]
    return [host]


async def _check_url(url: str) -> None:
    """Raise UpstreamError unless `url` is an http(s) URL on a public address.

    This is the only place that sees every fetch target: the proxy passes a
    client-supplied `url` and the prefetcher passes URLs taken straight from
    third-party feed content, with no session involved at all. Without it the
    reader will fetch anything on the Docker network, or a cloud metadata
    endpoint, and stream the body back (R1).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UpstreamError(f"refusing non-http(s) URL {url}")
    host = parts.hostname
    if not host:
        raise UpstreamError(f"refusing URL with no host: {url}")
    if settings.allow_private_media_hosts:
        return
    try:
        addrs = await asyncio.to_thread(_resolve, host)
    except OSError as exc:
        raise UpstreamError(f"cannot resolve {host} for {url}: {exc}") from exc
    for addr in addrs:
        ip = ipaddress.ip_address(addr)
        # ::ffff:127.0.0.1 is loopback wearing an IPv6 hat.
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            logger.warning(f"_check_url: refusing {url} — {host} resolves to non-public address {ip}")
            raise UpstreamError(f"refusing non-public address {ip} for {url}")


async def open_upstream(url: str, item_id: str | None, client: httpx.AsyncClient) -> httpx.Response:
    """Open a streaming upstream response, or raise UpstreamError.

    The body is left unread so the caller can tee it. Ownership of the response
    passes to tee_to_cache, which always closes it.

    Every fetch target — the original URL and each redirect hop — is checked
    against _check_url first, which is why redirects are followed manually
    here rather than by httpx (R1).

    On a non-success status the URL is marked dead (and a fully-dead item is
    dropped) before raising — that is what stops a 404'd post coming back on
    the next sync. A non-media content type raises NonMediaUpstreamError
    instead: the URL is NOT marked dead, but the response is closed and
    nothing is cached (F5).
    """
    logger.debug(f"open_upstream: GET {url} (item_id={item_id}, timeout={UPSTREAM_TIMEOUT_S}s)")
    target = url
    for _ in range(MAX_REDIRECTS + 1):
        await _check_url(target)
        response = await client.send(
            client.build_request("GET", target, timeout=UPSTREAM_TIMEOUT_S),
            stream=True,
            follow_redirects=False,
        )
        if not response.has_redirect_location:
            break
        location = response.headers["location"]
        await response.aclose()
        target = str(response.url.join(location))
        logger.debug(f"open_upstream: {url} redirected to {target}")
    else:
        raise UpstreamError(f"more than {MAX_REDIRECTS} redirects for {url}")
    if not response.is_success:
        status = response.status_code
        await response.aclose()
        logger.debug(f"open_upstream: {url} returned {status}, marking dead")
        await run_with_own_db(
            f"mark_url_dead_and_maybe_drop for {url}",
            lambda db: mark_url_dead_and_maybe_drop(url, item_id, db),
        )
        raise UpstreamError(f"upstream returned {status} for {url}")
    content_type = response.headers.get("content-type", "application/octet-stream")
    if not (
        content_type.startswith("image/")
        or content_type.startswith("video/")
        or content_type == "application/octet-stream"
    ):
        await response.aclose()
        logger.warning(f"open_upstream: refusing non-media content-type {content_type} for {url}")
        raise NonMediaUpstreamError(f"upstream returned non-media content type {content_type} for {url}")
    logger.debug(
        f"open_upstream: {url} -> {response.status_code} "
        f"type={response.headers.get('content-type', '?')} "
        f"length={response.headers.get('content-length', 'unknown')}"
    )
    return response


async def tee_to_cache(url: str, response: httpx.Response) -> AsyncIterator[bytes]:
    """Yield the response body onward while writing it into the cache.

    A client that disconnects mid-stream cancels this generator, so the partial
    download is discarded rather than cached — cache_stream_tee only publishes a
    file it finished writing, and the prefetcher warms it properly later.

    The dedup digest is only recorded on a complete transfer, for the same
    reason: half a file has the wrong hash.
    """
    content_type = response.headers.get("content-type", "application/octet-stream")
    digest = hashlib.sha256()
    sent = 0
    complete = False
    # Held for the whole transfer so the prefetcher leaves this URL alone while
    # a client is already pulling it.
    with download_claim(url):
        # aclosing, not a bare async-for: closing this generator does not close
        # the one it is iterating, so an abandoned download would leave its temp
        # file behind until the event loop got round to finalising the inner
        # generator. aclosing makes the cleanup happen now.
        cached = cache_stream_tee(url, response.aiter_bytes(CHUNK_SIZE), content_type)
        try:
            async with aclosing(cached):
                async for chunk in cached:
                    digest.update(chunk)
                    sent += len(chunk)
                    yield chunk
            complete = True
        finally:
            await response.aclose()
            if complete:
                logger.debug(f"tee_to_cache: streamed {sent} bytes of {url} to client and cache")
            else:
                logger.debug(
                    f"tee_to_cache: client stopped reading {url} after {sent} bytes; "
                    "nothing cached, the prefetcher will warm it later"
                )

    await run_with_own_db(
        f"record_media_hash for {url}",
        lambda db: record_media_hash(url, digest.hexdigest(), db),
    )


async def fetch_to_cache(url: str, item_id: str, client: httpx.AsyncClient) -> None:
    """Download `url` into the cache, discarding the bytes. Never raises.

    Skips URLs another download already holds, which is the common case: the
    prefetch hint fires on every scroll event and re-queues overlapping windows,
    and those windows overlap what the browser is fetching through the proxy.
    """
    with download_claim(url) as first:
        if not first:
            logger.debug(f"fetch_to_cache: {url} already in flight, skipping")
            return
        try:
            logger.debug(f"fetch_to_cache: warming {url} (item_id={item_id})")
            response = await open_upstream(url, item_id, client)
            async for _ in tee_to_cache(url, response):
                pass
            logger.debug(f"fetch_to_cache: warmed {url}")
        except Exception as exc:
            logger.debug(f"fetch_to_cache failed for {url}: {type(exc).__name__}: {exc}")
