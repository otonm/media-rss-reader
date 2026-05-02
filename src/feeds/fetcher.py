import hashlib
import logging

import feedparser
import httpx

from src.media.detector import detect_media

logger = logging.getLogger(__name__)

def _feed_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _item_id(feed_id: str, guid: str) -> str:
    return hashlib.sha256((feed_id + guid).encode()).hexdigest()


async def fetch_feed(url: str, client: httpx.AsyncClient) -> list[dict]:
    response = await client.get(url, follow_redirects=True, timeout=30)
    logger.debug(f"Fetched feed {url} with status code {response.status_code}")

    feed = feedparser.parse(response.text)
    feed_id = _feed_id(url)

    items = []
    for entry in feed.entries:
        result = detect_media(entry)
        if result is None:
            logger.debug(f"No media detected in entry {entry.get('title')}")
            continue

        media_url, media_type = result
        logger.debug(f"Detected media in entry {entry.get('title')}: {media_url} ({media_type})")

        guid = entry.get("id") or entry.get("link") or media_url
        items.append({
            "id": _item_id(feed_id, guid),
            "feed_id": feed_id,
            "guid": guid,
            "title": entry.get("title"),
            "media_url": media_url,
            "media_type": media_type,
            "pub_date": entry.get("published") or entry.get("updated"),
        })
    return items
