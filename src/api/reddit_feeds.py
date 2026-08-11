"""GET /api/reddit-feeds/status — proxy the Reddit Feeds status endpoint."""

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Response

from src.config import settings
from src.http_client import StatusDep
from src.logging_utils import loggable
from src.timing import timer

logger = logging.getLogger(__name__)
router = APIRouter()

# Two bounds on the exchange, because neither covers the other. MAX_STATUS_BYTES
# caps memory; asyncio.timeout caps total duration. httpx's own timeout is
# per-operation — every arriving chunk resets the read clock — so a companion
# emitting one byte every nine seconds would trip neither the read timeout nor
# the byte cap, and this handler would run until the client disconnects.
# (A third bound lives elsewhere: the status client's own small pool in
# src/http_client.py keeps an absent companion from exhausting the media proxy's
# connections.)
MAX_STATUS_BYTES = 1 << 20
STATUS_TIMEOUT_S = 10


class _Status:
    last_reachable: bool | None = None  # None = never polled


_status = _Status()


def _log_outcome(reachable: bool, message: str, *, exc_info: bool = False) -> None:
    """Log a poll outcome at a level that reflects the change, not the repeat.

    The status modal polls at 1 Hz while it is open, so an unreachable
    companion would otherwise emit an identical WARNING every second. Only the
    transition into failure warns; while it persists the outcome drops to
    DEBUG, and a return to reachable logs INFO. The first poll of a process
    counts as a transition either way, since last_reachable starts as None.

    Callers pass the message without a prefix — this adds the one.
    """
    level = logging.DEBUG if reachable == _status.last_reachable else (logging.INFO if reachable else logging.WARNING)
    logger.log(level, f"reddit_feeds_status {message}", exc_info=exc_info)
    _status.last_reachable = reachable


@router.get("/reddit-feeds/status")
async def reddit_feeds_status(client: StatusDep) -> Response:
    """Proxy the companion service's /status, or 502 with the reason.

    The body is read into memory under MAX_STATUS_BYTES and parsed once as a
    validity check — the parsed value is discarded, the original bytes are
    forwarded. Without it an HTML login page from a reverse proxy would reach
    the browser labelled application/json.

    The companion is optional: many deployments do not run it, so its absence
    is treated and logged as a recoverable condition.
    """
    url = f"{settings.reddit_feeds_api_url.rstrip('/')}/status"
    logger.debug(f"reddit_feeds_status fetching {url}")
    elapsed = timer()
    try:
        async with (
            asyncio.timeout(STATUS_TIMEOUT_S),
            client.stream("GET", url, timeout=STATUS_TIMEOUT_S, follow_redirects=False) as resp,
        ):
            if not resp.is_success:
                _log_outcome(False, f"upstream returned {resp.status_code} for {url} in {elapsed():.0f}ms")
                raise HTTPException(status_code=502, detail="Reddit Feeds API error")
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_STATUS_BYTES:
                    _log_outcome(
                        False,
                        f"body from {url} exceeded {MAX_STATUS_BYTES} bytes after {elapsed():.0f}ms, aborting",
                    )
                    raise HTTPException(status_code=502, detail="Reddit Feeds API body too large")
                chunks.append(chunk)
            body = b"".join(chunks)
            content_type = resp.headers.get("content-type", "?")
            status_code = resp.status_code
    except HTTPException:
        raise
    except Exception as exc:
        # _log_outcome picks the level, so an absent optional service never rises
        # above a single WARNING — the frontend already renders it as recoverable.
        # exc_info carries the traceback regardless of level, which matters
        # because httpx timeouts routinely stringify to an empty string;
        # `from exc` keeps the cause on the HTTPException.
        _log_outcome(
            False,
            f"unreachable: {type(exc).__name__} for {url} after {elapsed():.0f}ms",
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail="Reddit Feeds API unreachable") from exc

    if not body:
        _log_outcome(False, f"empty body from {url} status={status_code}")
        raise HTTPException(status_code=502, detail="Reddit Feeds API returned an empty body")

    try:
        json.loads(body)
    except Exception as exc:
        _log_outcome(
            False,
            f"non-JSON body from {url}: {type(exc).__name__} status={status_code} type={loggable(content_type)}",
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail="Reddit Feeds API returned non-JSON body") from exc

    _log_outcome(
        True, f"ok: {status_code} from {url} in {elapsed():.0f}ms bytes={len(body)} type={loggable(content_type)}"
    )
    return Response(content=body, media_type="application/json")
