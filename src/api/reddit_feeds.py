"""GET /api/reddit-feeds/status — proxy the Reddit Feeds status endpoint."""

import logging

from fastapi import APIRouter, HTTPException, Response

from src.config import settings
from src.scheduler import get_http_client

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/reddit-feeds/status")
async def reddit_feeds_status() -> Response:
    client = get_http_client()
    url = f"{settings.reddit_feeds_api_url.rstrip('/')}/status"
    logger.debug(f"reddit_feeds_status fetching {url}")
    try:
        resp = await client.get(url, timeout=10, follow_redirects=True)
    except Exception as exc:
        logger.warning(f"reddit_feeds_status unreachable: {exc}")
        raise HTTPException(status_code=502, detail="Reddit Feeds API unreachable") from None
    if not resp.is_success:
        logger.warning(f"reddit_feeds_status upstream returned {resp.status_code}")
        raise HTTPException(status_code=502, detail="Reddit Feeds API error")
    try:
        resp.json()
    except Exception:
        logger.warning("reddit_feeds_status upstream returned non-JSON body")
        raise HTTPException(status_code=502, detail="Reddit Feeds API returned non-JSON body") from None
    # Pass the body through rather than returning a parsed value: a `-> dict`
    # annotation makes FastAPI validate the return *after* this function exits,
    # outside the try, so a JSON array (`[]` for "no feeds yet") became a 500
    # instead of the 502-or-pass-through this endpoint promises (R4).
    return Response(content=resp.content, media_type="application/json")
