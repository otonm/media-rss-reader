"""Integer-versioned schema migrations.

MIGRATIONS is an ordered list of SQL statements. PRAGMA user_version stores
the count of applied migrations. On every startup, any statements from
MIGRATIONS[current_version:] are applied in sequence, with user_version
incremented after each one.

To add a migration: append one SQL string to MIGRATIONS. Never edit or
reorder existing entries — doing so would corrupt the version counter.
"""

import logging
import sqlite3

import aiosqlite

from src.media.normalize import media_key

logger = logging.getLogger(__name__)

MIGRATIONS: list[str] = [
    # v1: index on fetched_at to support age-based pruning queries
    "CREATE INDEX IF NOT EXISTS idx_items_fetched_at ON items(fetched_at)",
    # v2: seen_guids tombstone table — tracks seen state independently of pruning
    (
        "CREATE TABLE IF NOT EXISTS seen_guids ("
        "feed_id TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE, "
        "guid TEXT NOT NULL, "
        "seen_at TIMESTAMP NOT NULL, "
        "PRIMARY KEY (feed_id, guid))"
    ),
    # v3: backfill seen_guids from items that are already marked seen
    (
        "INSERT OR IGNORE INTO seen_guids (feed_id, guid, seen_at)"
        " SELECT feed_id, guid, seen_at FROM items WHERE seen_at IS NOT NULL"
    ),
    # v4: auth_config table for storing TOTP secret
    "CREATE TABLE IF NOT EXISTS auth_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    # v5: media_json stores all slides of a gallery item as a JSON array of {url, type}
    "ALTER TABLE items ADD COLUMN media_json TEXT",
    # v6: dead_urls table — tracks media URLs that have returned 404
    (
        "CREATE TABLE IF NOT EXISTS dead_urls ("
        "url TEXT PRIMARY KEY, "
        "marked_at TIMESTAMP NOT NULL DEFAULT (datetime('now')))"
    ),
    # v7: unavailable_guids tombstone — blocks re-insert of items whose
    # every media URL is in dead_urls
    (
        "CREATE TABLE IF NOT EXISTS unavailable_guids ("
        "feed_id TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE, "
        "guid TEXT NOT NULL, "
        "marked_at TIMESTAMP NOT NULL DEFAULT (datetime('now')), "
        "PRIMARY KEY (feed_id, guid))"
    ),
    # v8: site_link stores <channel><link> from RSS, populated on local-file sync
    "ALTER TABLE feeds ADD COLUMN site_link TEXT",
    # v9: index on media_url — _candidate_items looks up items by media_url inside a
    # write transaction; without this it is a full table scan holding the writer lock
    "CREATE INDEX IF NOT EXISTS idx_items_media_url ON items(media_url)",
    # v10: media_key holds the normalised media URL — the cross-feed dedup key.
    # Not backfilled: existing rows keep NULL, which never matches the insert
    # guard, and they age out within ITEMS_MAX_AGE_HOURS anyway.
    "ALTER TABLE items ADD COLUMN media_key TEXT",
    # v11: index on media_key — the insert guard probes it for every incoming item
    "CREATE INDEX IF NOT EXISTS idx_items_media_key ON items(media_key)",
    # v12: media_hashes — content digests of downloaded media, keyed by URL.
    # phash is NULL unless DEDUP_SIMILARITY is enabled.
    (
        "CREATE TABLE IF NOT EXISTS media_hashes ("
        "url TEXT PRIMARY KEY, "
        "sha256 TEXT NOT NULL, "
        "phash TEXT, "
        "hashed_at TIMESTAMP NOT NULL DEFAULT (datetime('now')))"
    ),
    # v13: index on sha256 — probed for every freshly downloaded media file
    "CREATE INDEX IF NOT EXISTS idx_media_hashes_sha256 ON media_hashes(sha256)",
    # v14: seen_media — the durable seen record, keyed on the normalised media
    # URL so a picture stays seen across feeds and across re-inserts. It
    # deliberately has NO foreign key to feeds: seen_guids was cascaded away
    # whenever sync_feeds removed a feed row. NOT NULL is explicit because
    # SQLite allows NULL in a TEXT PRIMARY KEY.
    ("CREATE TABLE IF NOT EXISTS seen_media (media_key TEXT PRIMARY KEY NOT NULL, seen_at TIMESTAMP NOT NULL)"),
    # v15/v16: HTTP validators for conditional feed fetches. A 304 skips the
    # download, the feedparser pass and the media detection for the whole feed,
    # which is the bulk of what every restart used to redo.
    "ALTER TABLE feeds ADD COLUMN etag TEXT",
    "ALTER TABLE feeds ADD COLUMN last_modified TEXT",
    # v17: mtime of the local *.xml source — same purpose for FEEDS_DIR files,
    # which have no HTTP layer to carry validators.
    "ALTER TABLE feeds ADD COLUMN source_mtime REAL",
    # v18: resolved_guids — entries that were detected and then deliberately
    # not stored, because _INSERT_ITEM's guards rejected them. Those guards are
    # keyed on media_key, which only exists after detection, while the
    # pre-detection skip set is keyed on guid; without this tombstone a rejected
    # entry never reaches items, so its guid never reaches the skip set and the
    # entry is re-detected on every poll for as long as the feed lists it.
    # CASCADE like unavailable_guids: dropping a feed drops its items too, so a
    # re-added feed should start clean.
    (
        "CREATE TABLE IF NOT EXISTS resolved_guids ("
        "feed_id TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE, "
        "guid TEXT NOT NULL, "
        "resolved_at TIMESTAMP NOT NULL DEFAULT (datetime('now')), "
        "PRIMARY KEY (feed_id, guid))"
    ),
]


async def run_migrations(db: aiosqlite.Connection) -> None:
    """Apply any pending migrations and advance the version counter."""
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    current_version: int = row[0]
    logger.debug(f"run_migrations current_version={current_version}")

    pending = MIGRATIONS[current_version:]
    if not pending:
        logger.debug("run_migrations no pending migrations")
        return

    logger.debug(f"run_migrations applying {len(pending)} pending migration(s)")
    for i, sql in enumerate(pending, start=current_version + 1):
        try:
            await db.execute(sql)
        except sqlite3.OperationalError as exc:
            # Gracefully handle ALTER TABLE ADD COLUMN when the column already
            # exists — this happens when run_migrations is called after
            # create_schema (which ships the latest schema in CREATE TABLE).
            if "duplicate column name" not in str(exc):
                raise
            logger.debug(f"run_migrations step {i} ignored duplicate column error")
        # Commit version update immediately so a crash mid-migration leaves a
        # consistent state — partially applied migrations are not retried.
        await db.execute(f"PRAGMA user_version = {i}")
        await db.commit()
        logger.debug(f"run_migrations applied step {i}, user_version now {i}")


async def backfill_seen_media(db: aiosqlite.Connection) -> None:
    """Populate seen_media from the pre-v14 seen records, then clean up.

    Cannot be a plain SQL migration: media_key() is Python. Both sources are
    read — items.seen_at covers rows still marked seen, and the seen_guids
    join recovers rows that were pruned and re-inserted unseen (which is
    exactly the state that made seen posts reappear).

    Idempotent, so it is safe to run on every startup; the DELETE doubles as a
    safety net for anything the insert guard in sync.py somehow lets through.
    """
    rows: list[aiosqlite.Row] = []
    async with db.execute("SELECT media_url, seen_at FROM items WHERE seen_at IS NOT NULL") as cur:
        rows.extend(await cur.fetchall())
    async with db.execute(
        """SELECT i.media_url AS media_url, sg.seen_at AS seen_at
           FROM seen_guids sg
           JOIN items i ON i.feed_id = sg.feed_id AND i.guid = sg.guid"""
    ) as cur:
        rows.extend(await cur.fetchall())

    await db.executemany(
        "INSERT OR IGNORE INTO seen_media (media_key, seen_at) VALUES (?, ?)",
        [(media_key(row["media_url"]), row["seen_at"]) for row in rows],
    )
    await db.execute("DELETE FROM items WHERE seen_at IS NULL AND media_key IN (SELECT media_key FROM seen_media)")
    await db.commit()
    logger.debug(f"backfill_seen_media reconciled {len(rows)} pre-v14 seen record(s)")
