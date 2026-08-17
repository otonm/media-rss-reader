"""Feed synchronisation: OPML sync and per-feed item refresh.

sync_feeds()        — reconcile the feeds table against FEEDS_DIR + OPML
refresh_all_feeds() — fetch new items for every known feed, then prune
prune_items()       — enforce KEEP_ITEMS and ITEMS_MAX_AGE_HOURS limits
"""

import asyncio
import json
import logging
from pathlib import Path

import aiosqlite
import feedparser
import httpx

from src.config import settings
from src.feeds.fetcher import _feed_id, entry_to_item, fetch_feed
from src.feeds.opml import parse_opml
from src.logging_utils import loggable
from src.media.cache import evict

logger = logging.getLogger(__name__)

# INSERT ... SELECT ... WHERE NOT EXISTS rather than plain VALUES. Two guards:
#   items      — rejects a picture already present in any feed.
#   seen_media — rejects a picture the user has already seen. Without this,
#                prune_items evicts the seen row and the next sync re-inserts
#                it straight out of a feed that still lists it, unseen.
# Both paths (local_xml_sync and _refresh_feed) share this statement.
# OR IGNORE covers the (feed_id, guid) UNIQUE constraint for re-polls.
_INSERT_ITEM = """INSERT OR IGNORE INTO items
   (id, feed_id, guid, title, media_url, media_key, media_type, media_json, pub_date)
   SELECT :id, :feed_id, :guid, :title, :media_url, :media_key, :media_type, :media_json, :pub_date
   WHERE NOT EXISTS (SELECT 1 FROM items WHERE media_key = :media_key)
     AND NOT EXISTS (SELECT 1 FROM seen_media WHERE media_key = :media_key)"""


async def _skip_guids(db: aiosqlite.Connection, feed_id: str) -> frozenset[str]:
    """GUIDs of this feed that need no media detection.

    Two sources, both meaning "already resolved": rows already in items, and
    GUIDs in resolved_guids that either _INSERT_ITEM's guards rejected or that
    were dropped after every media URL went dead. The guards key on media_key,
    which only exists after detection, so a rejected entry leaves no trace in
    items and would be re-detected on every poll without this tombstone.

    Loaded once per feed rather than per entry to avoid repeated queries.
    Bounded by KEEP_ITEMS; idx_items_feed_id and the tombstone primary key
    cover the lookups.
    """
    async with db.execute(
        """SELECT guid FROM items WHERE feed_id = ?
           UNION SELECT guid FROM resolved_guids WHERE feed_id = ?""",
        (feed_id, feed_id),
    ) as cur:
        return frozenset(row["guid"] for row in await cur.fetchall())


async def _insert_item(db: aiosqlite.Connection, item: dict) -> int:
    """INSERT one item, tombstoning its guid when the guard rejects it.

    Returns the number of rows inserted (0 or 1). Both ingest paths use this
    to ensure tombstones are written consistently.

    rowcount == 0 means a guard rejection — an entry whose (feed_id, guid) is
    already in items never reaches this point, because entry_to_item skips it
    before detection.
    """
    cursor = await db.execute(_INSERT_ITEM, item)
    if cursor.rowcount == 0:
        await db.execute(
            "INSERT OR IGNORE INTO resolved_guids (feed_id, guid) VALUES (?, ?)",
            (item["feed_id"], item["guid"]),
        )
        logger.debug(f"Item guid={item['guid']} rejected by the insert guard; tombstoned as resolved")
        return 0

    logger.debug(
        f"Storing item {loggable(item['title'])} with media URL {loggable(item['media_url'])} and ID {item['id']}"
    )

    # Every media URL of the item, indexed, so the known-URL gate is a point
    # lookup instead of a LIKE scan over media_json.
    slides = json.loads(item["media_json"]) if item.get("media_json") else []
    urls = {slide["url"] for slide in slides} | {item["media_url"]}
    await db.executemany(
        "INSERT OR IGNORE INTO media_urls (url, item_id) VALUES (?, ?)",
        [(url, item["id"]) for url in urls],
    )
    return cursor.rowcount


async def _ingest_items(db: aiosqlite.Connection, items: list[dict]) -> int:
    """Insert every item through the shared guard. Returns the number stored.

    Both ingest paths — local files and remote fetches — go through here. When
    the guard lived in only one of them, feeds loaded from the local directory
    re-surfaced their seen posts on every sync (spec.md §5.6).
    """
    inserted = 0
    for item in items:
        inserted += await _insert_item(db, item)
    return inserted


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
    if not folder.is_dir():  # noqa: ASYNC240 — one local stat on the sync loop, fine to block on
        logger.warning(f"FEEDS_DIR does not exist or is not a directory: {feeds_dir}")
        return

    xml_files = sorted(folder.glob("*.xml"))  # noqa: ASYNC240 — small local directory listing
    logger.debug(f"Local XML sync found {len(xml_files)} file(s) in {feeds_dir}")

    for path in xml_files:
        filename = path.name
        feed_id = _feed_id(filename)

        # The file is the whole input, so an unchanged mtime means an unchanged
        # feed: no read, no parse, no detection. The mtime lives on the feeds
        # row, so a wiped database or a sync_feeds hard-delete drops it together
        # with the items it stands for — it can never claim "unchanged" while
        # the items are gone.
        try:
            mtime = path.stat().st_mtime  # noqa: ASYNC240 — one local stat per feed file
        except OSError as exc:
            logger.warning(f"Skipping unreadable feed file {path}: {exc}")
            continue
        async with db.execute("SELECT source_mtime FROM feeds WHERE id = ?", (feed_id,)) as cur:
            row = await cur.fetchone()
        if row and row["source_mtime"] == mtime:
            logger.debug(f"Local XML {filename} unchanged (mtime {mtime}); skipping parse")
            continue

        try:
            text = path.read_text(encoding="utf-8")
            feed = await asyncio.to_thread(feedparser.parse, text)
        except Exception as exc:
            logger.warning(f"Skipping unreadable feed file {path}: {exc}")
            continue

        title = feed.channel.get("title") if hasattr(feed, "channel") else None
        site_link = feed.channel.get("link") if hasattr(feed, "channel") else None
        if not title:
            title = filename

        await db.execute(
            """INSERT INTO feeds (id, url, title, site_link) VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET url=excluded.url, title=excluded.title, site_link=excluded.site_link""",
            (feed_id, filename, title, site_link),
        )

        skip = await _skip_guids(db, feed_id)

        items = [entry_to_item(feed_id, entry, skip) for entry in feed.entries]
        inserted = await _ingest_items(db, [i for i in items if i is not None])
        logger.debug(f"Local XML sync {filename}: {inserted} new item(s)")

        await db.execute(
            "UPDATE feeds SET last_fetched_at = datetime('now'), source_mtime = ? WHERE id = ?",
            (mtime, feed_id),
        )

    await db.commit()


async def _refresh_feed(
    db: aiosqlite.Connection,
    feed_id: str,
    url: str,
    client: httpx.AsyncClient,
) -> None:
    """Fetch new items for one feed and write them to the database.

    INSERT OR IGNORE on (feed_id, guid) silently skips items already in the
    database. Known and tombstoned GUIDs are filtered inside entry_to_item,
    before media detection runs, so a previously-dropped dead post is never
    re-added and a post already stored is never re-detected.

    Stored ETag/Last-Modified are replayed as conditional headers; a 304
    yields no items and the validators are written back unchanged.
    """
    skip = await _skip_guids(db, feed_id)
    async with db.execute("SELECT etag, last_modified FROM feeds WHERE id = ?", (feed_id,)) as cur:
        row = await cur.fetchone()
    logger.debug(f"Feed {url} has {len(skip)} known guid(s) to skip")

    items, etag, last_modified = await fetch_feed(
        url,
        client,
        skip,
        row["etag"] if row else None,
        row["last_modified"] if row else None,
    )

    inserted = await _ingest_items(db, items)
    if items:
        logger.debug(f"Feed {url}: {inserted} new, {len(items) - inserted} already in DB")

    await db.execute(
        "UPDATE feeds SET last_fetched_at = datetime('now'), etag = ?, last_modified = ? WHERE id = ?",
        (etag, last_modified, feed_id),
    )
    await db.commit()


async def _evict_items(db: aiosqlite.Connection, where: str, params: tuple) -> int:
    """DELETE items matching `where`, tombstoning every guid it takes.

    Every eviction goes through here to ensure tombstones are written
    consistently. Without the tombstone, an evicted row is re-inserted by
    the next sync, unseen, under the same id. Since /api/items serves
    oldest-first while this function evicts oldest-first, the evicted rows
    are the ones a reader is currently viewing. Their seen beacons 404
    against the deleted row, so seen_media never records them either, and
    they return to the front of the feed on every cycle.
    """
    async with db.execute(f"DELETE FROM items WHERE {where} RETURNING feed_id, guid", params) as cur:  # noqa: S608 — where is a source-controlled fragment, params stay bound
        evicted = await cur.fetchall()
    if evicted:
        await db.executemany(
            "INSERT OR IGNORE INTO resolved_guids (feed_id, guid) VALUES (?, ?)",
            [(row["feed_id"], row["guid"]) for row in evicted],
        )
    return len(evicted)


async def prune_items(db: aiosqlite.Connection) -> None:
    """Enforce item retention limits.

    Two-phase strategy:
    1. Age-based: delete seen items older than ITEMS_MAX_AGE_HOURS; delete unseen items
       older than 4× ITEMS_MAX_AGE_HOURS (by pub_date).
    2. Count-based: if still over KEEP_ITEMS, delete oldest seen items first (by pub_date),
       then oldest unseen as a last resort.

    Everything evicted is tombstoned by _evict_items so it does not come
    straight back on the next sync.
    """
    # Phase 1: age-based eviction — seen items by fetched_at, unseen items by pub_date.
    # Unseen items are kept 4× longer since the user hasn't had a chance to see them yet.
    logger.debug(f"Pruning items older than {settings.items_max_age_hours} hours")
    await _evict_items(
        db,
        "seen_at IS NOT NULL AND fetched_at < datetime('now', ? || ' hours')",
        (f"-{settings.items_max_age_hours}",),
    )
    unseen_max_age = settings.items_max_age_hours * 4
    await _evict_items(
        db,
        "seen_at IS NULL AND pub_date < datetime('now', ? || ' hours')",
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
        await _evict_items(
            db,
            "id IN (SELECT id FROM items WHERE seen_at IS NOT NULL ORDER BY pub_date ASC NULLS LAST LIMIT ?)",
            (to_delete_seen,),
        )
        excess -= to_delete_seen

    # Last resort: delete the oldest unseen items.
    if excess > 0:
        logger.debug(f"Pruning {excess} unseen items to reduce total to {settings.keep_items}")
        await _evict_items(
            db,
            "id IN (SELECT id FROM items WHERE seen_at IS NULL ORDER BY pub_date ASC NULLS LAST LIMIT ?)",
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
    # Prune and evict always run regardless of individual feed failures
    # so keep_items and cache size limits are reliably enforced.
    await prune_items(db)
    await evict()


async def sync_feeds(
    db: aiosqlite.Connection,
    feeds_dir: str,
    opml_path: str,
    client: httpx.AsyncClient,
) -> None:
    """Reconcile the feeds table against the union of FEEDS_DIR + OPML.

    Pass ``opml_path=""`` to skip the OPML pass (folder only). Ends with a
    hard-delete: any feed row whose url is not in the union is removed
    (CASCADE drops items). The delete is skipped when the union is empty,
    since that means the sources are unreadable rather than genuinely empty.
    """
    await local_xml_sync(db, feeds_dir)

    folder_urls: set[str] = set()
    folder_dir = Path(feeds_dir)
    if folder_dir.is_dir():  # noqa: ASYNC240 — one local stat on the sync loop, fine to block on
        folder_urls = {p.name for p in folder_dir.glob("*.xml")}  # noqa: ASYNC240 — small local directory listing

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
        # Only placeholder count is interpolated; URL values remain bound.
        await db.execute(f"DELETE FROM feeds WHERE url NOT IN ({placeholders})", list(union))  # noqa: S608
    else:
        # Empty union usually means unreadable sources, not "drop all feeds".
        logger.warning(f"No feeds found in {feeds_dir} or {opml_path or '(no OPML)'}; keeping existing feeds")

    await db.commit()
