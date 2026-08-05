"""Track media URLs that have returned 404 and drop posts whose media is gone.

mark_url_dead_and_maybe_drop(url, item_id, db) is called from the proxy and
the prefetch warmer on every upstream non-success. It records the URL in
dead_urls, then for each item that contains it, deletes the item row and
writes a tombstone to unavailable_guids when every URL of that item is now
dead. Tombstones are read by _refresh_feed to skip re-insert on the next
feed poll.
"""

from __future__ import annotations

import logging

import aiosqlite

from src.media.normalize import item_slides

logger = logging.getLogger(__name__)


async def _candidate_items(db: aiosqlite.Connection, url: str, item_id: str | None) -> list[aiosqlite.Row]:
    """Return item rows that may contain `url`, deduplicated by item id.

    Searches two ways and merges the results:
    1. By item_id (given by the caller who observed the failure), but only
       if that item actually contains `url`.
    2. By media_url (to find all items sharing the same primary URL).

    Non-primary gallery slide URLs are intentionally not scanned here —
    real callers (proxy + prefetch) always pass item_id when they observed
    a non-primary slide 404.
    """
    seen: dict[str, aiosqlite.Row] = {}

    if item_id is not None:
        async with db.execute(
            "SELECT id, feed_id, guid, media_url, media_type, media_json FROM items WHERE id = ?",
            (item_id,),
        ) as cur:
            for row in await cur.fetchall():
                # The caller supplies item_id and url independently — the proxy
                # takes both from the query string — so an item that does not
                # actually contain this URL must not be a deletion candidate (R5).
                if url in _item_urls(row):
                    seen[row["id"]] = row

    async with db.execute(
        "SELECT id, feed_id, guid, media_url, media_type, media_json FROM items WHERE media_url = ?",
        (url,),
    ) as cur:
        for row in await cur.fetchall():
            seen[row["id"]] = row

    return list(seen.values())


def _item_urls(row: aiosqlite.Row) -> list[str]:
    """Return the full media URL list for an item row (primary + gallery)."""
    return [slide["url"] for slide in item_slides(row)]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def is_known_media_url(url: str, db: aiosqlite.Connection) -> bool:
    """True if `url` is the primary media_url of some item, or any slide of a gallery.

    Two-tier: the indexed primary lookup covers single-media items and a
    gallery's primary URL; the media_json scan covers gallery slide URLs that
    live only in the JSON array. Exact membership is verified in Python after
    the LIKE prefilter, so LIKE special characters in `url` cannot cause a
    false negative to slip past (the LIKE is a prefilter only).
    """
    async with db.execute("SELECT 1 FROM items WHERE media_url = ? LIMIT 1", (url,)) as cur:
        if await cur.fetchone() is not None:
            return True
    pattern = f'%"{_escape_like(url)}"%'
    async with db.execute(
        "SELECT id, media_url, media_type, media_json FROM items WHERE media_json LIKE ? ESCAPE '\\'",
        (pattern,),
    ) as cur:
        for row in await cur.fetchall():
            if url in _item_urls(row):
                return True
    return False


async def _all_dead(db: aiosqlite.Connection, urls: list[str]) -> bool:
    """True if every URL in `urls` is recorded in dead_urls."""
    if not urls:
        return False
    placeholders = ",".join("?" * len(urls))
    # Only placeholder count is interpolated; URL values remain bound.
    async with db.execute(f"SELECT url FROM dead_urls WHERE url IN ({placeholders})", urls) as cur:  # noqa: S608
        dead = {row["url"] for row in await cur.fetchall()}
    return dead.issuperset(urls)


async def mark_url_dead_and_maybe_drop(url: str, item_id: str | None, db: aiosqlite.Connection) -> list[str]:
    """Record `url` as dead. For every item that contains it, if every URL
    of that item is now dead, DELETE the row and tombstone it. Returns the
    IDs of items dropped by this call."""
    logger.debug(f"mark_url_dead_and_maybe_drop: recording dead url={url} item_id={item_id}")
    await db.execute("INSERT OR IGNORE INTO dead_urls (url) VALUES (?)", (url,))

    candidates = await _candidate_items(db, url, item_id)
    if not candidates:
        await db.commit()
        return []

    dropped: list[str] = []
    for row in candidates:
        urls = _item_urls(row)
        if not await _all_dead(db, urls):
            continue
        await db.execute("DELETE FROM items WHERE id = ?", (row["id"],))
        await db.execute(
            "INSERT OR IGNORE INTO unavailable_guids (feed_id, guid, marked_at) VALUES (?, ?, datetime('now'))",
            (row["feed_id"], row["guid"]),
        )
        dropped.append(row["id"])
        logger.debug(
            "dropped item %s (feed=%s guid=%s): all %d media URL(s) dead",
            row["id"],
            row["feed_id"],
            row["guid"],
            len(urls),
        )

    logger.debug(f"mark_url_dead_and_maybe_drop dropped {len(dropped)} item(s)")
    await db.commit()
    return dropped
