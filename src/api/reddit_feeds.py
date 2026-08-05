"""GET /api/reddit-feeds/status — proxy the Reddit Feeds status endpoint."""

import logging

from fastapi import APIRouter, HTTPException, Response

from src.config import settings
from src.scheduler import get_http_client
from src.timing import timer

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/reddit-feeds/status", response_model=None)
async def reddit_feeds_status() -> Response:
    client = get_http_client()
    url = f"{settings.reddit_feeds_api_url.rstrip('/')}/status"
    logger.debug(f"reddit_feeds_status fetching {url}")
    elapsed = timer()
    try:
        resp = await client.get(url, timeout=10, follow_redirects=False)
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
    if not resp.is_success:
        logger.warning(f"reddit_feeds_status upstream returned {resp.status_code} for {url} in {elapsed():.0f}ms")
        raise HTTPException(status_code=502, detail="Reddit Feeds API error")
    try:
        resp.json()
    except Exception as exc:
        # An HTML login page from a reverse proxy, a truncated body and a gzip
        # mismatch used to produce one identical line with no status, no
        # content-type and no bound exception (R11). exc_info keeps the
        # traceback at the level CLAUDE.md prescribes for recoverable errors.
        logger.warning(
            f"reddit_feeds_status non-JSON body from {url}: {type(exc).__name__} "
            f"status={resp.status_code} type={resp.headers.get('content-type', '?')}",
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail="Reddit Feeds API returned non-JSON body") from exc
    logger.debug(
        f"reddit_feeds_status {resp.status_code} from {url} in {elapsed():.0f}ms "
        f"bytes={len(resp.content)} type={resp.headers.get('content-type', '?')}"
    )
    # Pass the body through rather than returning a parsed value: a `-> dict`
    # annotation makes FastAPI validate the return *after* this function exits,
    # outside the try, so a JSON array (`[]` for "no feeds yet") became a 500
    # instead of the 502-or-pass-through this endpoint promises (R4).
    return Response(
        content=resp.content,
        media_type="application/json",
        headers={"X-Content-Type-Options": "nosniff"},
    )
