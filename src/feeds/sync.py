"""Feed synchronisation: OPML sync and per-feed item refresh.

sync_feeds()        — reconcile the feeds table against FEEDS_DIR + OPML
refresh_all_feeds() — fetch new items for every known feed, then prune
prune_items()       — enforce KEEP_ITEMS and ITEMS_MAX_AGE_HOURS limits
"""

import asyncio
import logging
from pathlib import Path

import aiosqlite
import feedparser
import httpx

from src.config import settings
from src.feeds.fetcher import _feed_id, entry_to_item, fetch_feed
from src.feeds.opml import parse_opml
from src.media.cache import evict

logger = logging.getLogger(__name__)

# INSERT ... SELECT ... WHERE NOT EXISTS rather than plain VALUES: the guard
# rejects an item whose media_key is already present in *any* feed, which is
# what stops the same picture appearing once per feed that carried it.
# OR IGNORE still covers the (feed_id, guid) UNIQUE constraint for re-polls.
_INSERT_ITEM = """INSERT OR IGNORE INTO items
   (id, feed_id, guid, title, media_url, media_key, media_type, media_json, pub_date)
   SELECT :id, :feed_id, :guid, :title, :media_url, :media_key, :media_type, :media_json, :pub_date
   WHERE NOT EXISTS (SELECT 1 FROM items WHERE media_key = :media_key)"""


async def local_xml_sync(db: aiosqlite.Connection, feeds_dir: str) -> None:
    """Scan `feeds_dir` for *.xml, parse each file, insert feeds + items.

    One row per XML file: feed_id = sha256(filename), url = filename,
    title = feed.channel.title (falls back to filename), site_link =
    feed.channel.link (NULL when missing). Items are inserted with the
    existing _item_id scheme so deduplication still works.

    Missing or unreadable files are logged and skipped — they trigger a
    hard-delete on the next call to sync_feeds(), not here.
    """
    folder = Path(feeds_dir)
    if not folder.is_dir():  # noqa: ASYNC240
        logger.warning(f"FEEDS_DIR does not exist or is not a directory: {feeds_dir}")
        return

    xml_files = sorted(folder.glob("*.xml"))  # noqa: ASYNC240
    logger.debug(f"Local XML sync found {len(xml_files)} file(s) in {feeds_dir}")

    for path in xml_files:
        filename = path.name
        try:
            text = path.read_text(encoding="utf-8")
            feed = await asyncio.to_thread(feedparser.parse, text)
        except Exception as exc:
            logger.warning(f"Skipping unreadable feed file {path}: {exc}")
            continue

        feed_id = _feed_id(filename)
        title = feed.channel.get("title") if hasattr(feed, "channel") else None
        site_link = feed.channel.get("link") if hasattr(feed, "channel") else None
        if not title:
            title = filename

        await db.execute(
            """INSERT INTO feeds (id, url, title, site_link) VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET url=excluded.url, title=excluded.title, site_link=excluded.site_link""",
            (feed_id, filename, title, site_link),
        )

        async with db.execute("SELECT guid FROM unavailable_guids WHERE feed_id = ?", (feed_id,)) as cur:
            dead_guids = {row["guid"] for row in await cur.fetchall()}

        inserted = 0
        for entry in feed.entries:
            item = entry_to_item(feed_id, entry)
            if item is None or item["guid"] in dead_guids:
                continue
            cursor = await db.execute(_INSERT_ITEM, item)
            inserted += cursor.rowcount
        logger.debug(f"Local XML sync {filename}: {inserted} new item(s)")

        await db.execute(
            "UPDATE feeds SET last_fetched_at = datetime('now') WHERE id = ?",
            (feed_id,),
        )

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
    Items whose (feed_id, guid) is in unavailable_guids are skipped before
    the INSERT so a previously-dropped dead post is never re-added.
    """
    items = await fetch_feed(url, client)

    async with db.execute("SELECT guid FROM unavailable_guids WHERE feed_id = ?", (feed_id,)) as cur:
        dead_guids = {row["guid"] for row in await cur.fetchall()}

    logger.debug(f"Feed {url} has {len(dead_guids)} tombstoned guid(s) to skip")

    inserted = 0
    for item in items:
        if item["guid"] in dead_guids:
            logger.debug(f"Skipping tombstoned item guid={item['guid']} in feed {url}")
            continue
        logger.debug(f"Storing item {item['title']} with media URL {item['media_url']} and ID {item['id']}")
        cursor = await db.execute(_INSERT_ITEM, item)
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
    async with db.execute("SELECT changes()") as cur:
        restored = (await cur.fetchone())[0]
    logger.debug(f"Feed {url}: restored seen_at on {restored} item(s)")
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
    logger.debug(f"Prune complete: kept {row[0]} items (target ≤ {settings.keep_items})")


async def refresh_all_feeds(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Refresh every feed in the database, prune old items, and evict stale cache."""
    logger.debug("Refreshing all feeds")
    async with db.execute("SELECT id, url FROM feeds") as cur:
        feeds = await cur.fetchall()
    for feed in feeds:
        if not feed["url"].startswith(("http://", "https://")):
            continue
        try:
            await _refresh_feed(db, feed["id"], feed["url"], client)
        except Exception as exc:
            logger.warning("Feed refresh failed for %s: %s", feed["url"], exc)
    # Prune and evict always run regardless of individual feed failures so
    # keep_items and cache size limits are reliably enforced.
    await prune_items(db)
    await evict()


async def sync_feeds(
    db: aiosqlite.Connection,
    feeds_dir: str,
    opml_path: str,
    client: httpx.AsyncClient,
) -> None:
    """Reconcile the feeds table against the union of FEEDS_DIR + OPML.

    Pass ``opml_path=""`` to skip the OPML pass (folder only). Always runs
    a hard-delete at the end: any feed row whose url is not in the union
    is removed (CASCADE drops items).
    """
    await local_xml_sync(db, feeds_dir)

    folder_urls: set[str] = set()
    folder_dir = Path(feeds_dir)
    if folder_dir.is_dir():  # noqa: ASYNC240
        folder_urls = {p.name for p in folder_dir.glob("*.xml")}  # noqa: ASYNC240

    opml_urls: set[str] = set()
    if opml_path:
        try:
            opml_feeds = parse_opml(opml_path)
        except FileNotFoundError:
            logger.debug(f"OPML file not present at {opml_path}; skipping OPML pass")
            opml_feeds = []
        except Exception as exc:
            logger.warning(f"OPML parse failed for {opml_path}: {exc}")
            opml_feeds = []
        for feed in opml_feeds:
            url = feed["url"]
            if Path(url).name in folder_urls:
                continue
            opml_urls.add(url)
            fid = _feed_id(url)
            await db.execute(
                "INSERT OR IGNORE INTO feeds (id, url, title) VALUES (?, ?, ?)",
                (fid, url, feed["title"]),
            )

    union = folder_urls | opml_urls

    if union:
        placeholders = ",".join("?" * len(union))
        await db.execute(f"DELETE FROM feeds WHERE url NOT IN ({placeholders})", list(union))
    else:
        await db.execute("DELETE FROM feeds")

    await db.commit()
