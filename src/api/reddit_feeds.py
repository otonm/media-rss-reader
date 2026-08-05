"""GET /api/reddit-feeds/status — proxy the Reddit Feeds status endpoint."""

import json
import logging

from fastapi import APIRouter, HTTPException, Response

from src.config import settings
from src.scheduler import get_http_client
from src.timing import timer

logger = logging.getLogger(__name__)
router = APIRouter()

# The status payload is a short JSON summary. src/media/fetch.py bounds an
# upstream body the same way; httpx's timeout is per-operation, not a
# whole-request budget, so a trickling companion could otherwise hold the
# connection open and grow the buffer without limit.
MAX_STATUS_BYTES = 1 << 20


@router.get("/reddit-feeds/status", response_model=None)
async def reddit_feeds_status() -> Response:
    """Proxy the companion service's /status, or 502 with the reason.

    The body is passed through as bytes rather than returned as a parsed value:
    a `-> dict` annotation makes FastAPI validate the return *after* this
    function exits, outside the try, so a JSON array (`[]` for "no feeds yet")
    became a 500 instead of the 502-or-pass-through this endpoint promises (R4).
    The parse below is a validity check only — an HTML login page from a
    reverse proxy must not reach the browser as application/json.

    The companion is optional: many deployments do not run it, and the status
    modal polls at 1 Hz while it is open. Its absence is logged as recoverable.
    """
    client = get_http_client()
    url = f"{settings.reddit_feeds_api_url.rstrip('/')}/status"
    logger.debug(f"reddit_feeds_status fetching {url}")
    elapsed = timer()
    try:
        async with client.stream("GET", url, timeout=10, follow_redirects=False) as resp:
            if not resp.is_success:
                logger.warning(
                    f"reddit_feeds_status upstream returned {resp.status_code} for {url} in {elapsed():.0f}ms"
                )
                raise HTTPException(status_code=502, detail="Reddit Feeds API error")
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_STATUS_BYTES:
                    logger.warning(
                        f"reddit_feeds_status body from {url} exceeded {MAX_STATUS_BYTES} bytes "
                        f"after {elapsed():.0f}ms, aborting"
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
        logger.warning(
            f"reddit_feeds_status unreachable: {type(exc).__name__} for {url} after {elapsed():.0f}ms",
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail="Reddit Feeds API unreachable") from exc

    try:
        json.loads(body)
    except Exception as exc:
        logger.warning(
            f"reddit_feeds_status non-JSON body from {url}: {type(exc).__name__} "
            f"status={status_code} type={content_type}",
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail="Reddit Feeds API returned non-JSON body") from exc

    logger.debug(
        f"reddit_feeds_status {status_code} from {url} in {elapsed():.0f}ms bytes={len(body)} type={content_type}"
    )
    return Response(content=body, media_type="application/json")
