"""Integer-versioned schema migrations.

MIGRATIONS is an ordered list of SQL statements and callables taking the
connection. PRAGMA user_version stores the count of applied migrations. On
every startup, any entries from MIGRATIONS[current_version:] are applied in
sequence, with user_version incremented after each one.

To add a migration: append one entry to MIGRATIONS. Never edit or reorder
existing entries — doing so would corrupt the version counter.

Each entry must also be idempotent. SQLite runs DDL outside any transaction,
so a statement takes effect before the version bump that records it, and a
crash in between replays it on the next startup. That is what the IF NOT EXISTS
clauses and run_migrations' duplicate-column handling are there for.
"""

import logging
import sqlite3
from collections.abc import Awaitable, Callable

import aiosqlite

from src.media.normalize import media_key

logger = logging.getLogger(__name__)

MigrationStep = str | Callable[[aiosqlite.Connection], Awaitable[None]]


async def _backfill_seen_media(db: aiosqlite.Connection) -> None:
    """v14's data step — see the v19 comment in MIGRATIONS."""
    rows: list[aiosqlite.Row] = []
    # Rows still carrying their own seen mark.
    async with db.execute("SELECT media_url, seen_at FROM items WHERE seen_at IS NOT NULL") as cur:
        rows.extend(await cur.fetchall())
    # Rows whose mark survives only in seen_guids, because pruning removed the
    # item and a later fetch re-inserted it unseen.
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
    # Any item still marked unseen whose media is now known seen was re-inserted
    # after a prune; seen_media is the authority, so drop it.
    await db.execute("DELETE FROM items WHERE seen_at IS NULL AND media_key IN (SELECT media_key FROM seen_media)")
    await db.commit()
    logger.debug(f"_backfill_seen_media reconciled {len(rows)} pre-v14 seen record(s)")


async def _merge_unavailable_guids(db: aiosqlite.Connection) -> None:
    """v20's data step — see the v20 comment in MIGRATIONS.

    Guarded on the source table's existence: a replay of the whole pending
    batch from an early checkpoint (test_replay_from_v19_hits_merge_unavailable_guids_guard
    exercises exactly this) can reach this step after v21 already dropped
    unavailable_guids in an earlier pass. Without the guard that is a hard
    OperationalError instead of the no-op idempotency requires.
    """
    async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='unavailable_guids'") as cur:
        if await cur.fetchone() is None:
            return
    await db.execute(
        "INSERT OR IGNORE INTO resolved_guids (feed_id, guid, resolved_at)"
        " SELECT feed_id, guid, marked_at FROM unavailable_guids"
    )


# v24's statement, named so tests that insert items with raw SQL can mirror them
# into media_urls with the exact statement the schema uses.
BACKFILL_MEDIA_URLS = (
    "INSERT OR IGNORE INTO media_urls (url, item_id) "
    "SELECT COALESCE(json_extract(s.value, '$.url'), i.media_url), i.id "
    "FROM items i LEFT JOIN json_each(i.media_json) s "
    "WHERE json_valid(COALESCE(i.media_json, 'null')) "
    "UNION "
    "SELECT i.media_url, i.id FROM items i WHERE i.media_url IS NOT NULL"
)

MIGRATIONS: list[MigrationStep] = [
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
    # URL so a picture stays seen across feeds and across re-inserts. No foreign
    # key to feeds, deliberately: a cascade would erase the seen state whenever
    # sync_feeds drops a feed row. NOT NULL is explicit because SQLite allows
    # NULL in a TEXT PRIMARY KEY.
    ("CREATE TABLE IF NOT EXISTS seen_media (media_key TEXT PRIMARY KEY NOT NULL, seen_at TIMESTAMP NOT NULL)"),
    # v15/v16: HTTP validators for conditional feed fetches. A 304 skips the
    # download, the feedparser pass and the media detection for the whole feed.
    "ALTER TABLE feeds ADD COLUMN etag TEXT",
    "ALTER TABLE feeds ADD COLUMN last_modified TEXT",
    # v17: mtime of the local *.xml source — same purpose for FEEDS_DIR files,
    # which have no HTTP layer to carry validators.
    "ALTER TABLE feeds ADD COLUMN source_mtime REAL",
    # v18: resolved_guids — entries that were detected and then deliberately not
    # stored, because _INSERT_ITEM's guards rejected them. Those guards key on
    # media_key, which only exists after detection, while the pre-detection skip
    # set keys on guid: a rejected entry never reaches items, so its guid never
    # reaches the skip set, and it is re-detected on every poll for as long as
    # the feed lists it. This tombstone is what the skip set reads instead.
    # CASCADE for the same reason as v7's tombstone table: dropping a feed
    # drops its items too, so a re-added feed should start clean.
    (
        "CREATE TABLE IF NOT EXISTS resolved_guids ("
        "feed_id TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE, "
        "guid TEXT NOT NULL, "
        "resolved_at TIMESTAMP NOT NULL DEFAULT (datetime('now')), "
        "PRIMARY KEY (feed_id, guid))"
    ),
    # v19: v14's data step, which originally escaped the version gate and ran
    # on every startup. Populate seen_media from the pre-v14 seen records
    # (items.seen_at and the seen_guids tombstone), then drop items that a
    # pre-v14 prune+re-insert left unseen. A fresh database has no rows to
    # move; INSERT OR IGNORE and the DELETE are both idempotent, so a crash
    # replay is safe. Runs once, when a database passes v18.
    _backfill_seen_media,
    # v20: unavailable_guids and resolved_guids were two tables answering one
    # question. Both are PK(feed_id, guid), both cascade from feeds, both are
    # written only by INSERT OR IGNORE, and both were read by exactly one query
    # — the skip-set UNION in feeds/sync.py. Move the rows, keeping the original
    # timestamp so age information is not lost. A callable rather than a bare
    # statement because the merge must tolerate unavailable_guids already
    # being gone (see _merge_unavailable_guids).
    _merge_unavailable_guids,
    # v21: drop the now-empty table. Separate step because db.execute takes one
    # statement, and because a crash between the two replays v20 harmlessly.
    "DROP TABLE IF EXISTS unavailable_guids",
    # v22: media_urls — every media URL of every item, one row each, indexed.
    # Replaces the two-tier known-URL gate whose second tier was an unindexed
    # `media_json LIKE '%...%'` scan of the whole items table.
    (
        "CREATE TABLE IF NOT EXISTS media_urls ("
        "url TEXT NOT NULL, "
        "item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE, "
        "PRIMARY KEY (url, item_id))"
    ),
    # v23: the cascade deletes by item_id, and _all_dead scans media_urls by it too.
    "CREATE INDEX IF NOT EXISTS idx_media_urls_item_id ON media_urls(item_id)",
    # v24: backfill from media_json. json_each is built into SQLite, so this
    # stays a SQL step. COALESCE covers rows written before media_json existed:
    # their only URL is media_url. INSERT OR IGNORE makes the replay idempotent.
    # Verified against three edge cases on SQLite 3.50: a gallery with a
    # non-ASCII slide URL, a row with media_json NULL, and a row with
    # unparseable media_json. All three yield exactly their real URLs.
    BACKFILL_MEDIA_URLS,
    # v25: seen_guids has been dead schema since v14 introduced seen_media.
    # v2 creates it, v3 populates it, v19 drains it into seen_media — this drops
    # it once that has happened. Ordering makes it correct at every starting
    # version: a fresh DB runs create -> populate(0) -> drain(0) -> drop, and an
    # old DB gets its full backfill first. The three earlier steps stay exactly
    # where they are; user_version is an index into this list, so removing them
    # would silently skip every migration after.
    # This makes v19 the first entry in the list whose read (seen_guids) a later
    # entry (this one) destroys, and unlike _merge_unavailable_guids,
    # _backfill_seen_media has no existence guard on it. Real databases stay
    # safe because user_version is monotonic and committed after every step, so
    # the pending range on any real startup always starts exactly at the step
    # that never finished — it can never re-enter v19 after v25 has already run.
    # But the list's replay-from-any-checkpoint property (MIGRATIONS[N:] is safe
    # to run for any N) is no longer true for N < 19 on an already-migrated
    # database, which only an artificial rewind (e.g. in a test) can construct.
    # Whoever appends v26: if it depends on something an even-later step could
    # destroy, it needs the same existence guard _merge_unavailable_guids uses.
    "DROP TABLE IF EXISTS seen_guids",
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
    for i, step in enumerate(pending, start=current_version + 1):
        try:
            if callable(step):
                await step(db)
            else:
                await db.execute(step)
        except sqlite3.OperationalError as exc:
            # ADD COLUMN for a column that is already there: DDL auto-commits,
            # so a crash before the version bump replays the ALTER on the next
            # startup. The column exists as the migration wanted, so count it
            # applied.
            if "duplicate column name" not in str(exc):
                raise
            logger.debug(f"run_migrations step {i} ignored duplicate column error")
        # Bump the version after each statement rather than once at the end, so
        # a failure part-way through keeps the steps that already succeeded and
        # the next startup resumes at the one that failed.
        await db.execute(f"PRAGMA user_version = {i}")
        await db.commit()
        logger.debug(f"run_migrations applied step {i}, user_version now {i}")
