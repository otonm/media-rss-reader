"""RSS feed fetcher.

Fetches a single feed URL over HTTP, parses it with feedparser, and
returns a list of item dicts ready to INSERT into the database.
Items without a detectable media URL are silently skipped.
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime

import feedparser
import httpx

from src.media.detector import detect_all_media
from src.media.normalize import media_key

logger = logging.getLogger(__name__)


def _feed_id(url: str) -> str:
    """Stable, collision-resistant ID derived from the feed URL."""
    return hashlib.sha256(url.encode()).hexdigest()


def _item_id(feed_id: str, guid: str) -> str:
    """Stable item ID derived from the feed ID and the entry's GUID."""
    return hashlib.sha256((feed_id + guid).encode()).hexdigest()


def _parse_pub_date(entry: dict) -> str | None:
    """Normalise feed dates to a SQLite-sortable ISO string (UTC).

    feedparser hands us a struct_time in `published_parsed`/`updated_parsed`;
    the raw `published` string is RFC-822 for RSS 2.0, which sorts
    alphabetically by weekday name in SQLite TEXT comparison (F1).
    Returns None when no parseable date is present.
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        return datetime(*parsed[:6]).isoformat(sep=" ")
    except TypeError, ValueError:
        return None


def entry_to_item(feed_id: str, entry: dict) -> dict | None:
    """Convert one parsed feed entry into an items-table row dict.

    Returns None when the entry carries no media with a recognisable
    extension, which is the caller's signal to skip it.
    """
    results = detect_all_media(entry)
    if not results:
        logger.debug(f"No media detected in entry {entry.get('title')}")
        return None

    media_url, media_type = results[0]
    logger.debug(f"Detected {len(results)} slide(s) in entry {entry.get('title')}: {media_url} ({media_type})")

    # Use entry.id as the canonical GUID; fall back to link, then media URL.
    guid = entry.get("id") or entry.get("link") or media_url
    return {
        "id": _item_id(feed_id, guid),
        "feed_id": feed_id,
        "guid": guid,
        "title": entry.get("title"),
        "media_url": media_url,
        "media_key": media_key(media_url),
        "media_type": media_type,
        "media_json": json.dumps([{"url": u, "type": t} for u, t in results]),
        "pub_date": _parse_pub_date(entry),
    }


async def fetch_feed(url: str, client: httpx.AsyncClient) -> list[dict]:
    """Fetch and parse one RSS feed; return media items as a list of dicts.

    Each dict matches the columns of the items table.
    Entries without a recognisable media URL are excluded.
    """
    response = await client.get(url, follow_redirects=True, timeout=30)
    logger.debug(f"Fetched feed {url} with status code {response.status_code}")

    feed = await asyncio.to_thread(feedparser.parse, response.text)
    feed_id = _feed_id(url)

    items = [entry_to_item(feed_id, entry) for entry in feed.entries]
    return [item for item in items if item is not None]
