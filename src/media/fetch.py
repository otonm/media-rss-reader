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
from urllib.parse import urljoin, urlsplit

import httpx

from src.config import settings
from src.db.connection import run_with_own_db
from src.logging_utils import loggable
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


def _pinned_url(original: str, ip: str) -> str:
    """Return `original` with its host replaced by the literal IP `ip`."""
    parts = urlsplit(original)
    port = parts.port
    netloc = (f"[{ip}]:{port}" if port else f"[{ip}]") if ":" in ip else f"{ip}:{port}" if port else ip
    return parts._replace(netloc=netloc).geturl()


async def _check_url(url: str) -> list[str]:
    """Return the validated public IP(s) for `url`, or raise UpstreamError.

    The caller pins the httpx request to one of these IPs (with the original
    Host header + SNI) so httpx cannot re-resolve the host and reach a
    different address than the one validated here (DNS-rebinding TOCTOU).
    """
    # Escaped once here rather than at each call site below: this value also
    # ends up embedded raw in every UpstreamError message this function
    # raises, and those messages resurface — unescaped, if this weren't done —
    # in whatever outer log line later renders the exception via `{exc}`
    # (e.g. proxy_media's "502 for ... — {exc}").
    safe_url = loggable(url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        logger.warning(f"_check_url: refusing non-http(s) URL {safe_url}")
        raise UpstreamError(f"refusing non-http(s) URL {safe_url}")
    host = parts.hostname
    if not host:
        logger.warning(f"_check_url: refusing URL with no host: {safe_url}")
        raise UpstreamError(f"refusing URL with no host: {safe_url}")
    if settings.allow_private_media_hosts:
        addrs = await asyncio.to_thread(_resolve, host)
    else:
        try:
            addrs = await asyncio.to_thread(_resolve, host)
        except OSError as exc:
            logger.warning(f"_check_url: DNS resolution failed for {host} ({safe_url}): {exc}")
            raise UpstreamError(f"cannot resolve {host} for {safe_url}: {exc}") from exc
        validated: list[str] = []
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
                logger.warning(f"_check_url: refusing {safe_url} — {host} resolves to non-public address {ip}")
                raise UpstreamError(f"refusing non-public address {ip} for {safe_url}")
            validated.append(str(ip))
        addrs = validated
    if not addrs:
        logger.warning(f"_check_url: {host} resolved to no usable address for {safe_url}")
        raise UpstreamError(f"cannot resolve {host} for {safe_url}")
    return addrs


async def open_upstream(
    url: str, item_id: str | None, client: httpx.AsyncClient, request_id: str | None = None
) -> tuple[httpx.Response, str]:
    """Open a streaming upstream response, or raise UpstreamError.

    Returns (response, content_type): the caller must not re-derive the type
    from response.headers itself, since this is where it was validated.

    The body is left unread so the caller can tee it. Ownership of the response
    passes to tee_to_cache, which always closes it.

    Every fetch target — the original URL and each redirect hop — is checked
    against _check_url first, which is why redirects are followed manually
    here rather than by httpx (R1).

    On 404 or 410 the URL is marked dead (and a fully-dead item is dropped)
    before raising — that is what stops a 404'd post coming back on the next
    sync. Any other non-success raises without touching the database. A
    non-media content type raises NonMediaUpstreamError instead: the URL is
    NOT marked dead, but the response is closed and nothing is cached (F5).
    """
    # Escaped once here, same reasoning as _check_url's safe_url: this also
    # feeds UpstreamError/NonMediaUpstreamError messages and run_with_own_db's
    # label, both of which resurface in log lines this function does not own.
    safe_url = loggable(url)
    logger.debug(
        f"open_upstream: GET {safe_url} (item_id={item_id}, timeout={UPSTREAM_TIMEOUT_S}s, request_id={request_id})"
    )
    logical = url
    for _ in range(MAX_REDIRECTS + 1):
        validated = await _check_url(logical)
        pinned = _pinned_url(logical, validated[0])
        host = urlsplit(logical).hostname or ""
        request = client.build_request(
            "GET",
            pinned,
            timeout=UPSTREAM_TIMEOUT_S,
            headers={"Host": host} if host else None,
            extensions={"sni_hostname": host} if host else None,
        )
        response = await client.send(request, stream=True, follow_redirects=False)
        if not response.has_redirect_location:
            break
        location = response.headers["location"]
        await response.aclose()
        # Join the location against the LOGICAL url (original host), not the
        # pinned IP, so the original hostname survives relative redirects.
        logical = urljoin(logical, location)
        logger.debug(f"open_upstream: {safe_url} redirected to {loggable(logical)} (request_id={request_id})")
    else:
        logger.warning(f"open_upstream: exceeded {MAX_REDIRECTS} redirects for {safe_url} (request_id={request_id})")
        raise UpstreamError(f"more than {MAX_REDIRECTS} redirects for {safe_url}")
    if not response.is_success:
        status = response.status_code
        await response.aclose()
        # Only a permanent answer may reach mark_url_dead_and_maybe_drop: it
        # DELETEs the item and tombstones its guid so the next sync will not
        # re-insert it. A 429 or 503 from a busy CDN is transient, and marking
        # dead on those erased posts permanently (R5).
        if status in (404, 410):
            logger.warning(
                f"open_upstream: {safe_url} returned {status}, marking dead "
                f"(item_id={item_id}, request_id={request_id})"
            )
            await run_with_own_db(
                f"mark_url_dead_and_maybe_drop for {safe_url}",
                lambda db: mark_url_dead_and_maybe_drop(url, item_id, db),
            )
        else:
            logger.warning(
                f"open_upstream: {safe_url} returned {status}; transient, not marking dead (request_id={request_id})"
            )
        raise UpstreamError(f"upstream returned {status} for {safe_url}")
    content_type = response.headers.get("content-type", "application/octet-stream")
    media_type = content_type.split(";")[0].strip().lower()
    # SVG starts with image/ but is an active document: served from our own
    # origin its <script> runs there with the session cookie attached (R8).
    if media_type == "image/svg+xml" or not (
        media_type.startswith("image/") or media_type.startswith("video/") or media_type == "application/octet-stream"
    ):
        await response.aclose()
        logger.warning(f"open_upstream: refusing non-media content-type {content_type} for {safe_url}")
        raise NonMediaUpstreamError(f"upstream returned non-media content type {content_type} for {safe_url}")
    declared = response.headers.get("content-length", "")
    if settings.media_max_bytes and declared.isdigit() and int(declared) > settings.media_max_bytes:
        await response.aclose()
        logger.warning(
            f"open_upstream: {safe_url} declared {declared} bytes, over MEDIA_MAX_BYTES "
            f"({settings.media_max_bytes}); refusing (request_id={request_id})"
        )
        raise UpstreamError(
            f"upstream declared {declared} bytes for {safe_url}, over MEDIA_MAX_BYTES ({settings.media_max_bytes})"
        )
    logger.debug(
        f"open_upstream: {safe_url} -> {response.status_code} "
        f"type={response.headers.get('content-type', '?')} "
        f"length={response.headers.get('content-length', 'unknown')} "
        f"request_id={request_id}"
    )
    return response, content_type


async def tee_to_cache(
    url: str, response: httpx.Response, content_type: str, request_id: str | None = None
) -> AsyncIterator[bytes]:
    """Yield the response body onward while writing it into the cache.

    `content_type` is the value open_upstream already resolved and validated —
    not re-derived from response.headers here, so the streamed response and
    the .meta sidecar it feeds always agree with what the gate checked.

    A client that disconnects mid-stream cancels this generator, so the partial
    download is discarded rather than cached — cache_stream_tee only publishes a
    file it finished writing, and the prefetcher warms it properly later.

    The dedup digest is only recorded on a complete transfer, for the same
    reason: half a file has the wrong hash.
    """
    digest = hashlib.sha256()
    sent = 0
    complete = False
    server_abort = False
    non_client_abort = False
    safe_url = loggable(url)
    # Held for the whole transfer so the prefetcher leaves this URL alone while
    # a client is already pulling it.
    with download_claim(url):
        # aclosing, not a bare async-for: closing this generator does not close
        # the one it is iterating, so an abandoned download would leave its temp
        # file behind until the event loop got round to finalising the inner
        # generator. aclosing makes the cleanup happen now.
        cached = cache_stream_tee(url, response.aiter_bytes(CHUNK_SIZE), content_type, request_id=request_id)
        try:
            async with aclosing(cached):
                try:
                    async for chunk in cached:
                        digest.update(chunk)
                        sent += len(chunk)
                        if settings.media_max_bytes and sent > settings.media_max_bytes:
                            # The response body has already started, so the client
                            # sees a truncated file. That is the trade for not
                            # letting an undeclared stream fill the volume (R7).
                            server_abort = True
                            logger.warning(
                                f"tee_to_cache: server aborted {safe_url} after {sent} bytes "
                                f"(over MEDIA_MAX_BYTES={settings.media_max_bytes}); client sees a truncated file "
                                f"(request_id={request_id})"
                            )
                            raise UpstreamError(
                                f"upstream body for {safe_url} passed MEDIA_MAX_BYTES "
                                f"({settings.media_max_bytes}) after {sent} bytes; aborting"
                            )
                        yield chunk
                except UpstreamError:
                    raise
                except Exception as exc:
                    logger.warning(
                        f"tee_to_cache: aborted {safe_url} after {sent} bytes: {type(exc).__name__}: {exc} "
                        f"(request_id={request_id})"
                    )
                    non_client_abort = True
                    # Wrapped so fetch_to_cache's split handler recognizes this as
                    # already-reported and does not log it a second time.
                    raise UpstreamError(f"tee_to_cache aborted for {safe_url}: {type(exc).__name__}: {exc}") from exc
                complete = True
        finally:
            await response.aclose()
            if complete:
                logger.debug(
                    f"tee_to_cache: streamed {sent} bytes of {safe_url} to client and cache (request_id={request_id})"
                )
            elif server_abort or non_client_abort:
                pass  # already logged at WARNING above
            else:
                logger.debug(
                    f"tee_to_cache: client stopped reading {safe_url} after {sent} bytes; "
                    f"nothing cached, the prefetcher will warm it later (request_id={request_id})"
                )

    await run_with_own_db(
        f"record_media_hash for {safe_url}",
        lambda db: record_media_hash(url, digest.hexdigest(), db),
    )


async def fetch_to_cache(url: str, item_id: str, client: httpx.AsyncClient, request_id: str | None = None) -> None:
    """Download `url` into the cache, discarding the bytes. Never raises.

    Skips URLs another download already holds, which is the common case: the
    prefetch hint fires on every scroll event and re-queues overlapping windows,
    and those windows overlap what the browser is fetching through the proxy.
    """
    safe_url = loggable(url)
    with download_claim(url) as first:
        if not first:
            logger.debug(f"fetch_to_cache: {safe_url} already in flight, skipping (request_id={request_id})")
            return
        try:
            logger.debug(f"fetch_to_cache: warming {safe_url} (item_id={item_id}, request_id={request_id})")
            response, content_type = await open_upstream(url, item_id, client, request_id=request_id)
            async for _ in tee_to_cache(url, response, content_type, request_id=request_id):
                pass
            logger.debug(f"fetch_to_cache: warmed {safe_url} (request_id={request_id})")
        except (UpstreamError, NonMediaUpstreamError) as exc:
            # Every raise site in _check_url, open_upstream and tee_to_cache warns
            # once, at the point that knows why, before raising — logging it again
            # here would double it.
            logger.debug(f"fetch_to_cache: {safe_url} not cached — {exc}")
        except Exception as exc:
            # The only outcome signal the prefetcher has. At DEBUG a wholly broken
            # warm path was invisible at the default level while the endpoint kept
            # answering {"status": "ok"}.
            logger.warning(f"fetch_to_cache failed for {safe_url}: {type(exc).__name__}: {exc}", exc_info=True)
