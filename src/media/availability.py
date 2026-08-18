"""Track media URLs that are permanently gone and drop posts whose media is dead.

mark_url_dead_and_maybe_drop(url, item_id, db) is called from the proxy (on
a failed upstream fetch and on a browser-reported load failure) and the
prefetch warmer when an upstream fetch fails permanently (404, 403, ...) or
returns a non-media body. It records the URL in dead_urls, then for each
item that contains it, deletes the item row and writes a tombstone to
resolved_guids when every URL of that item is now dead. Tombstones are
read by _refresh_feed to skip re-insert on the next feed poll.
"""

from __future__ import annotations

import logging

import aiosqlite

from src.logging_utils import loggable

logger = logging.getLogger(__name__)


async def _candidate_items(db: aiosqlite.Connection, url: str) -> list[aiosqlite.Row]:
    """Every item row that actually contains `url`. One join through media_urls."""
    async with db.execute(
        "SELECT i.id, i.feed_id, i.guid FROM items i JOIN media_urls m ON m.item_id = i.id WHERE m.url = ?",
        (url,),
    ) as cur:
        return list(await cur.fetchall())


async def is_known_media_url(url: str, db: aiosqlite.Connection) -> bool:
    """True if `url` is any media URL — primary or gallery slide — of some item.

    One indexed lookup. See spec.md §8.3 and §12.4: this is the gate that stops
    the media proxy being an open relay and stops a client deleting rows for a
    URL the library has never stored.
    """
    async with db.execute("SELECT 1 FROM media_urls WHERE url = ? LIMIT 1", (url,)) as cur:
        return await cur.fetchone() is not None


async def _all_dead(db: aiosqlite.Connection, item_id: str) -> bool:
    """True if every media URL of `item_id` is recorded in dead_urls."""
    async with db.execute(
        "SELECT COUNT(*) FROM media_urls m "
        "LEFT JOIN dead_urls d ON d.url = m.url "
        "WHERE m.item_id = ? AND d.url IS NULL",
        (item_id,),
    ) as cur:
        alive = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM media_urls WHERE item_id = ?", (item_id,)) as cur:
        total = (await cur.fetchone())[0]
    return total > 0 and alive == 0


async def drop_item(db: aiosqlite.Connection, row: aiosqlite.Row) -> None:
    """Delete an item row and tombstone its (feed_id, guid) against re-insert.

    The single path by which an item leaves the library.

    Does not log: the two call sites log at different levels from their own
    module loggers, and their tests assert on both facts. Does not commit:
    the caller owns the transaction (see mark_url_dead_and_maybe_drop).
    """
    await db.execute("DELETE FROM items WHERE id = ?", (row["id"],))
    await db.execute(
        "INSERT OR IGNORE INTO resolved_guids (feed_id, guid, resolved_at) VALUES (?, ?, datetime('now'))",
        (row["feed_id"], row["guid"]),
    )


async def mark_url_dead_and_maybe_drop(url: str, item_id: str | None, db: aiosqlite.Connection) -> list[str]:
    """Record `url` as dead. For every item that contains it, if every URL
    of that item is now dead, DELETE the row and tombstone it. Returns the
    IDs of items dropped by this call.

    Does not commit — the caller owns the transaction boundary."""
    logger.debug(f"mark_url_dead_and_maybe_drop: recording dead url={loggable(url)} item_id={loggable(item_id)}")
    await db.execute("INSERT OR IGNORE INTO dead_urls (url) VALUES (?)", (url,))

    # item_id is no longer used to look candidates up — every slide URL is
    # indexed in media_urls now — it only feeds the log line above.
    candidates = await _candidate_items(db, url)
    if not candidates:
        return []

    dropped: list[str] = []
    for row in candidates:
        if not await _all_dead(db, row["id"]):
            continue
        await drop_item(db, row)
        dropped.append(row["id"])
        logger.debug(
            f"dropped item {loggable(row['id'])} (feed={loggable(row['feed_id'])} "
            f"guid={loggable(row['guid'])}): every media URL is dead"
        )

    logger.debug(f"mark_url_dead_and_maybe_drop dropped {len(dropped)} item(s)")
    return dropped
