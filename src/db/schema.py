"""Initial database schema: the two base tables and their indexes.

All statements use IF NOT EXISTS so this module is safe to call on every
startup without checking whether the schema already exists.

This is the frozen v1 shape. Every later table and column belongs in
migrations.py: an existing database only ever sees what migrations.py adds,
and run_migrations() runs right after create_schema() on every startup, so a
fresh database gets both.
"""

import logging

import aiosqlite

logger = logging.getLogger(__name__)

# feeds stores one row per RSS feed, from either the OPML file or an *.xml file
# in FEEDS_DIR (whose filename stands in for the url).
# id is sha256(url) so it is stable across restarts without a sequence counter.
_CREATE_FEEDS = """
CREATE TABLE IF NOT EXISTS feeds (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL UNIQUE,
    title           TEXT,
    last_fetched_at TIMESTAMP,
    created_at      TIMESTAMP DEFAULT (datetime('now'))
)
"""

# items stores every media entry extracted from feed content. Dropping a feed
# takes its items with it, which the tombstone tables in migrations.py are
# written around. UNIQUE(feed_id, guid) is the key sync.py's INSERT OR IGNORE
# relies on to skip entries it has already stored.
_CREATE_ITEMS = """
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,
    feed_id     TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid        TEXT NOT NULL,
    title       TEXT,
    media_url   TEXT NOT NULL,
    media_type  TEXT NOT NULL,              -- 'image' | 'gif' | 'video'
    pub_date    TIMESTAMP,
    fetched_at  TIMESTAMP DEFAULT (datetime('now')),
    seen_at     TIMESTAMP,                  -- NULL = unseen
    UNIQUE(feed_id, guid)
)
"""

# The first three serve one query each: filter by feed, and sync.py's two prune
# passes, which order by pub_date and select on seen_at.
#
# idx_items_feed_pub matches RANKED_ITEMS_CTE's window exactly (PARTITION BY
# feed_id ORDER BY pub_date, id), so ROW_NUMBER reads it in order instead of
# sorting the whole table. /api/items materialises that CTE twice per page —
# once to resolve the cursor anchor, once for the page itself — and it is the
# endpoint every scroll hits.
_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_items_feed_id  ON items(feed_id)",
    "CREATE INDEX IF NOT EXISTS idx_items_pub_date ON items(pub_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_items_seen_at  ON items(seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_items_feed_pub ON items(feed_id, pub_date, id)",
]


async def create_schema(db: aiosqlite.Connection) -> None:
    """Create tables and indexes if they do not already exist."""
    logger.debug("create_schema creating tables and indexes")
    await db.execute(_CREATE_FEEDS)
    await db.execute(_CREATE_ITEMS)
    for sql in _CREATE_INDEXES:
        await db.execute(sql)
    await db.commit()
    logger.debug("create_schema done")
