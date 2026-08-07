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


def entry_to_item(feed_id: str, entry: dict, skip_guids: frozenset[str] = frozenset()) -> dict | None:
    """Convert one parsed feed entry into an items-table row dict.

    Returns None when the entry carries no media with a recognisable
    extension, or when its GUID is in `skip_guids` — either way the caller's
    signal to skip it.

    `skip_guids` holds the GUIDs already stored or already tombstoned for this
    feed. Detection is a pure function of the entry and entries never change,
    so re-deriving it for a GUID we have already resolved is pure waste — hence
    the check runs *before* detect_all_media rather than after the INSERT.
    """
    # Use entry.id as the canonical GUID; fall back to link, then media URL.
    guid = entry.get("id") or entry.get("link")
    if guid and guid in skip_guids:
        logger.debug(f"Skipping known guid {guid}; no detection needed")
        return None

    results = detect_all_media(entry)
    if not results:
        logger.debug(f"No media detected in entry {entry.get('title')}")
        return None

    media_url, media_type = results[0]
    logger.debug(f"Detected {len(results)} slide(s) in entry {entry.get('title')}: {media_url} ({media_type})")

    # ponytail: an entry with neither id nor link only gets a GUID once
    # detection has run, so it is re-detected on every poll. Rare; fixing it
    # needs a detection cache keyed on something cheaper than the GUID.
    if not guid:
        guid = media_url
        if guid in skip_guids:
            logger.debug(f"Skipping known media-url guid {guid}; detection already spent")
            return None
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


async def fetch_feed(
    url: str,
    client: httpx.AsyncClient,
    skip_guids: frozenset[str] = frozenset(),
    etag: str | None = None,
    last_modified: str | None = None,
) -> tuple[list[dict], str | None, str | None]:
    """Fetch and parse one RSS feed; return (items, etag, last_modified).

    Each item dict matches the columns of the items table. Entries without a
    recognisable media URL, and entries whose GUID is in `skip_guids`, are
    excluded.

    `etag` and `last_modified` are whatever the previous fetch stored. They are
    replayed as conditional headers, and a 304 returns them unchanged with an
    empty item list — nothing is downloaded, parsed or detected. The return
    shape is the same either way so the caller's write-back is uniform.
    """
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    response = await client.get(url, headers=headers, follow_redirects=True, timeout=30)
    logger.debug(f"Fetched feed {url} with status code {response.status_code}")

    if response.status_code == 304:
        logger.debug(f"Feed {url} unchanged (304); skipping parse and detection")
        return [], etag, last_modified

    feed = await asyncio.to_thread(feedparser.parse, response.text)
    feed_id = _feed_id(url)

    items = [entry_to_item(feed_id, entry, skip_guids) for entry in feed.entries]
    return (
        [item for item in items if item is not None],
        response.headers.get("etag"),
        response.headers.get("last-modified"),
    )
