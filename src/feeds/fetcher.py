"""RSS feed fetcher.

Fetches a single feed URL over HTTP, parses it with feedparser, and
returns a list of item dicts ready to INSERT into the database.
Items without a detectable media URL are silently skipped.
"""

import asyncio
import hashlib
import json
import logging

import feedparser
import httpx

from src.media.detector import detect_all_media

logger = logging.getLogger(__name__)


def _feed_id(url: str) -> str:
    """Stable, collision-resistant ID derived from the feed URL."""
    return hashlib.sha256(url.encode()).hexdigest()


def _item_id(feed_id: str, guid: str) -> str:
    """Stable item ID derived from the feed ID and the entry's GUID."""
    return hashlib.sha256((feed_id + guid).encode()).hexdigest()


async def fetch_feed(url: str, client: httpx.AsyncClient) -> list[dict]:
    """Fetch and parse one RSS feed; return media items as a list of dicts.

    Each dict matches the columns of the items table.
    Entries without a recognisable media URL are excluded.
    """
    response = await client.get(url, follow_redirects=True, timeout=30)
    logger.debug(f"Fetched feed {url} with status code {response.status_code}")

    feed = await asyncio.to_thread(feedparser.parse, response.text)
    feed_id = _feed_id(url)

    items = []
    for entry in feed.entries:
        results = detect_all_media(entry)
        if not results:
            logger.debug(f"No media detected in entry {entry.get('title')}")
            continue

        media_url, media_type = results[0]
        logger.debug(f"Detected media in entry {entry.get('title')}: {media_url} ({media_type})")
        logger.debug(
            "Built media_json for %s: %d slide(s) [first=%s]",
            entry.get("title"),
            len(results),
            media_url,
        )

        # Use entry.id as the canonical GUID; fall back to link, then media URL.
        guid = entry.get("id") or entry.get("link") or media_url
        items.append(
            {
                "id": _item_id(feed_id, guid),
                "feed_id": feed_id,
                "guid": guid,
                "title": entry.get("title"),
                "media_url": media_url,
                "media_type": media_type,
                "media_json": json.dumps([{"url": u, "type": t} for u, t in results]),
                "pub_date": entry.get("published") or entry.get("updated"),
            }
        )
    return items
