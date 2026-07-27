"""GET /api/reddit-feeds/status — proxy the Reddit Feeds status endpoint."""

from fastapi import APIRouter, HTTPException

from src.config import settings
from src.scheduler import get_http_client

router = APIRouter()


@router.get("/reddit-feeds/status")
async def reddit_feeds_status() -> dict:
    client = get_http_client()
    url = f"{settings.reddit_feeds_api_url.rstrip('/')}/status"
    try:
        resp = await client.get(url)
    except Exception:
        raise HTTPException(status_code=502, detail="Reddit Feeds API unreachable")
    if resp.is_error:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()
