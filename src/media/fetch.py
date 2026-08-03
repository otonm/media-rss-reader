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

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing

import httpx

from src.db.connection import run_with_own_db
from src.media.availability import mark_url_dead_and_maybe_drop
from src.media.cache import cache_stream_tee, download_claim
from src.media.dedup import record_media_hash

logger = logging.getLogger(__name__)

CHUNK_SIZE = 65536
# Per-operation httpx budget: connect, read, write and pool each get this.
UPSTREAM_TIMEOUT_S = 30


class UpstreamError(Exception):
    """The origin refused the request. The URL has already been marked dead."""


async def open_upstream(url: str, item_id: str | None, client: httpx.AsyncClient) -> httpx.Response:
    """Open a streaming upstream response, or raise UpstreamError.

    The body is left unread so the caller can tee it. Ownership of the response
    passes to tee_to_cache, which always closes it.

    On a non-success status the URL is marked dead (and a fully-dead item is
    dropped) before raising — that is what stops a 404'd post coming back on
    the next sync.
    """
    response = await client.send(
        client.build_request("GET", url, timeout=UPSTREAM_TIMEOUT_S),
        stream=True,
        follow_redirects=True,
    )
    if not response.is_success:
        status = response.status_code
        await response.aclose()
        logger.debug(f"open_upstream: {url} returned {status}, marking dead")
        await run_with_own_db(
            f"mark_url_dead_and_maybe_drop for {url}",
            lambda db: mark_url_dead_and_maybe_drop(url, item_id, db),
        )
        raise UpstreamError(f"upstream returned {status} for {url}")
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
                    yield chunk
        finally:
            await response.aclose()

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
        response = await open_upstream(url, item_id, client)
        async for _ in tee_to_cache(url, response):
            pass
    except Exception as exc:
        logger.debug(f"fetch_to_cache failed for {url}: {exc}")
