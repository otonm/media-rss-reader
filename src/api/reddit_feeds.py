"""GET /api/reddit-feeds/status — proxy the Reddit Feeds status endpoint."""

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Response

from src.config import settings
from src.http_client import StatusDep
from src.timing import timer

logger = logging.getLogger(__name__)
router = APIRouter()

# Three bounds, because one is not enough. MAX_STATUS_BYTES caps memory;
# asyncio.timeout caps the exchange. httpx's timeout is per-operation — each
# arriving chunk resets the read clock. Without the outer timeout, a companion
# emitting one byte every nine seconds would trip neither the read timeout nor
# the byte cap, and the handler would run until the client disconnects. The
# status client's own small pool (src/http_client.py) is the third bound: even
# if both of these failed, an absent companion cannot reach the media proxy's
# connections.
MAX_STATUS_BYTES = 1 << 20
STATUS_TIMEOUT_S = 10

# The last outcome, so repeats of an expected failure do not log at WARNING.
# Module state is normally a smell; here it is the smallest a transition
# detector can be, and it lives beside its only reader.
_last_reachable: bool | None = None  # None = never polled


def _log_outcome(reachable: bool, message: str, *, exc_info: bool = False) -> None:
    """WARNING on the transition into failure, DEBUG while it persists, INFO on recovery."""
    global _last_reachable
    if reachable:
        if _last_reachable is False:
            logger.info(f"reddit_feeds_status recovered: {message}")
        else:
            logger.debug(f"reddit_feeds_status ok: {message}")
    elif _last_reachable is False:
        logger.debug(f"reddit_feeds_status still unreachable: {message}")
    else:
        logger.warning(message, exc_info=exc_info)
    _last_reachable = reachable


@router.get("/reddit-feeds/status")
async def reddit_feeds_status(client: StatusDep) -> Response:
    """Proxy the companion service's /status, or 502 with the reason.

    The parse below is a validity check only — an HTML login page from a
    reverse proxy must not reach the browser as application/json.

    The companion is optional: many deployments do not run it, and the status
    modal polls at 1 Hz while it is open. Its absence is logged as recoverable.
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
                _log_outcome(
                    False, f"reddit_feeds_status upstream returned {resp.status_code} for {url} in {elapsed():.0f}ms"
                )
                raise HTTPException(status_code=502, detail="Reddit Feeds API error")
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_STATUS_BYTES:
                    _log_outcome(
                        False,
                        f"reddit_feeds_status body from {url} exceeded {MAX_STATUS_BYTES} bytes "
                        f"after {elapsed():.0f}ms, aborting",
                    )
                    raise HTTPException(status_code=502, detail="Reddit Feeds API body too large")
                chunks.append(chunk)
            body = b"".join(chunks)
            content_type = resp.headers.get("content-type", "?")
            status_code = resp.status_code
    except HTTPException:
        raise
    except Exception as exc:
        # warning, not exception(): an absent optional service is a recoverable
        # condition, and the frontend already renders it as one. exc_info keeps
        # the traceback, which matters because httpx timeouts routinely
        # stringify to empty. from exc keeps __cause__ (R11).
        _log_outcome(
            False,
            f"reddit_feeds_status unreachable: {type(exc).__name__} for {url} after {elapsed():.0f}ms",
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail="Reddit Feeds API unreachable") from exc

    if not body:
        _log_outcome(False, f"reddit_feeds_status empty body from {url} status={status_code}")
        raise HTTPException(status_code=502, detail="Reddit Feeds API returned an empty body")

    try:
        json.loads(body)
    except Exception as exc:
        _log_outcome(
            False,
            f"reddit_feeds_status non-JSON body from {url}: {type(exc).__name__} "
            f"status={status_code} type={content_type}",
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail="Reddit Feeds API returned non-JSON body") from exc

    _log_outcome(True, f"{status_code} from {url} in {elapsed():.0f}ms bytes={len(body)} type={content_type}")
    return Response(content=body, media_type="application/json")
