"""GET /api/reddit-feeds/status — proxy the Reddit Feeds status endpoint."""

import logging

from fastapi import APIRouter, HTTPException

from src.config import settings
from src.scheduler import get_http_client

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/reddit-feeds/status")
async def reddit_feeds_status() -> dict:
    client = get_http_client()
    url = f"{settings.reddit_feeds_api_url.rstrip('/')}/status"
    logger.debug(f"reddit_feeds_status fetching {url}")
    try:
        resp = await client.get(url, timeout=10, follow_redirects=True)
    except Exception as exc:
        logger.warning(f"reddit_feeds_status unreachable: {exc}")
        raise HTTPException(status_code=502, detail="Reddit Feeds API unreachable") from None
    if resp.status_code != 200:
        logger.warning(f"reddit_feeds_status upstream returned {resp.status_code}")
        raise HTTPException(status_code=502, detail="Reddit Feeds API error")
    try:
        return resp.json()
    except Exception:
        logger.warning("reddit_feeds_status upstream returned non-JSON body")
        raise HTTPException(status_code=502, detail="Reddit Feeds API returned non-JSON body") from None
