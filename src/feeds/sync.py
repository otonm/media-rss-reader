"""Feed synchronisation: OPML sync and per-feed item refresh.

opml_sync()         — reconcile the feeds table against the OPML file
refresh_all_feeds() — fetch new items for every known feed, then prune
prune_items()       — enforce KEEP_ITEMS and ITEMS_MAX_AGE_HOURS limits
"""

import logging

import aiosqlite
import httpx

from src.config import settings
from src.feeds.fetcher import _feed_id, fetch_feed
from src.feeds.opml import parse_opml
from src.media.cache import evict

logger = logging.getLogger(__name__)


async def opml_sync(db: aiosqlite.Connection, opml_path: str, client: httpx.AsyncClient) -> None:
    """Reconcile the feeds table with the current OPML file.

    New feeds are inserted; feeds no longer in the file are deleted.
    Deletion cascades automatically to the items table via the FK constraint.
    The HTTP client is accepted as a parameter but not used here — it is
    forwarded to allow callers to trigger an immediate fetch after sync if needed.
    """
    feeds = parse_opml(opml_path)
    logger.debug(f"Syncing {len(feeds)} feeds from OPML file {opml_path}")

    feed_ids = []
    for feed in feeds:
        fid = _feed_id(feed["url"])
        feed_ids.append(fid)
        logger.debug(f"Storing feed {feed['title']} with URL {feed['url']} and ID {fid}")

        # INSERT OR IGNORE preserves existing rows (title, last_fetched_at, etc.)
        await db.execute(
            "INSERT OR IGNORE INTO feeds (id, url, title) VALUES (?, ?, ?)",
            (fid, feed["url"], feed["title"]),
        )

    # Delete feeds whose IDs are not in the current OPML set.
    if feed_ids:
        placeholders = ",".join("?" * len(feed_ids))
        await db.execute(f"DELETE FROM feeds WHERE id NOT IN ({placeholders})", feed_ids)
    else:
        # OPML is empty — remove everything.
        await db.execute("DELETE FROM feeds")

    await db.commit()


async def _refresh_feed(
    db: aiosqlite.Connection,
    feed_id: str,
    url: str,
    client: httpx.AsyncClient,
) -> None:
    """Fetch new items for one feed and write them to the database.

    INSERT OR IGNORE on (feed_id, guid) silently skips items that are
    already in the database, so this function is safe to call repeatedly.
    """
    items = await fetch_feed(url, client)
    inserted = 0
    for item in items:
        logger.debug(f"Storing item {item['title']} with media URL {item['media_url']} and ID {item['id']}")
        cursor = await db.execute(
            """INSERT OR IGNORE INTO items
               (id, feed_id, guid, title, media_url, media_type, pub_date)
               VALUES (:id, :feed_id, :guid, :title, :media_url, :media_type, :pub_date)""",
            item,
        )
        inserted += cursor.rowcount
    if items:
        logger.debug(f"Feed {url}: {inserted} new, {len(items) - inserted} already in DB")

    # Restore seen_at for items that were pruned and then re-inserted from the feed.
    await db.execute(
        """UPDATE items
           SET seen_at = (
               SELECT sg.seen_at FROM seen_guids sg
               WHERE sg.feed_id = items.feed_id AND sg.guid = items.guid
           )
           WHERE feed_id = ? AND seen_at IS NULL
             AND EXISTS (
               SELECT 1 FROM seen_guids sg
               WHERE sg.feed_id = items.feed_id AND sg.guid = items.guid
           )""",
        (feed_id,),
    )
    await db.execute(
        "UPDATE feeds SET last_fetched_at = datetime('now') WHERE id = ?",
        (feed_id,),
    )
    await db.commit()


async def prune_items(db: aiosqlite.Connection) -> None:
    """Enforce item retention limits.

    Two-phase strategy:
    1. Age-based: delete seen items older than ITEMS_MAX_AGE_HOURS; delete unseen items
       older than 4× ITEMS_MAX_AGE_HOURS (by pub_date).
    2. Count-based: if still over KEEP_ITEMS, delete oldest seen items first (by pub_date),
       then oldest unseen as a last resort.
    """
    # Phase 1: age-based eviction — seen items by fetched_at, unseen items by pub_date.
    # Unseen items are kept 4× longer since the user hasn't had a chance to see them yet.
    logger.debug(f"Pruning items older than {settings.items_max_age_hours} hours")
    await db.execute(
        "DELETE FROM items WHERE seen_at IS NOT NULL AND fetched_at < datetime('now', ? || ' hours')",
        (f"-{settings.items_max_age_hours}",),
    )
    unseen_max_age = settings.items_max_age_hours * 4
    await db.execute(
        "DELETE FROM items WHERE seen_at IS NULL AND pub_date < datetime('now', ? || ' hours')",
        (f"-{unseen_max_age}",),
    )

    # Phase 2: count-based eviction
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        row = await cur.fetchone()
    total: int = row[0]
    logger.debug(f"Total items after age pruning: {total}")

    if total <= settings.keep_items:
        await db.commit()
        return

    excess = total - settings.keep_items

    # Prefer deleting seen items over unseen ones.
    async with db.execute("SELECT COUNT(*) FROM items WHERE seen_at IS NOT NULL") as cur:
        row = await cur.fetchone()
    seen_count: int = row[0]

    to_delete_seen = min(excess, seen_count)
    logger.debug(f"Pruning {to_delete_seen} seen items to reduce total to {settings.keep_items}")

    if to_delete_seen > 0:
        await db.execute(
            "DELETE FROM items WHERE id IN "
            "(SELECT id FROM items WHERE seen_at IS NOT NULL "
            " ORDER BY pub_date ASC NULLS LAST LIMIT ?)",
            (to_delete_seen,),
        )
        excess -= to_delete_seen

    # Last resort: delete the oldest unseen items.
    if excess > 0:
        logger.debug(f"Pruning {excess} unseen items to reduce total to {settings.keep_items}")
        await db.execute(
            "DELETE FROM items WHERE id IN "
            "(SELECT id FROM items WHERE seen_at IS NULL "
            " ORDER BY pub_date ASC NULLS LAST LIMIT ?)",
            (excess,),
        )

    await db.commit()
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        row = await cur.fetchone()
    logger.debug(f"Items remaining after pruning: {row[0]}")


async def refresh_all_feeds(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Refresh every feed in the database, prune old items, and evict stale cache."""
    logger.debug("Refreshing all feeds")
    async with db.execute("SELECT id, url FROM feeds") as cur:
        feeds = await cur.fetchall()
    for feed in feeds:
        try:
            await _refresh_feed(db, feed["id"], feed["url"], client)
        except Exception as exc:
            logger.warning("Feed refresh failed for %s: %s", feed["url"], exc)
    # Prune and evict always run regardless of individual feed failures so
    # keep_items and cache size limits are reliably enforced.
    await prune_items(db)
    await evict()
