"""Initial database schema.

All statements use IF NOT EXISTS so this module is safe to call on every
startup without checking whether the schema already exists.
"""

import aiosqlite

# feeds stores one row per RSS feed URL found in the OPML file.
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

# items stores every media entry extracted from feed content.
# ON DELETE CASCADE means removing a feed automatically removes all its items.
# The (feed_id, guid) unique constraint is the deduplication key used by INSERT OR IGNORE.
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

# Indexes to support the common query patterns: filter by feed, sort by date,
# filter unseen, and prune by fetched_at.
_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_items_feed_id  ON items(feed_id)",
    "CREATE INDEX IF NOT EXISTS idx_items_pub_date ON items(pub_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_items_seen_at  ON items(seen_at)",
]


async def create_schema(db: aiosqlite.Connection) -> None:
    """Create tables and indexes if they do not already exist."""
    await db.execute(_CREATE_FEEDS)
    await db.execute(_CREATE_ITEMS)
    for sql in _CREATE_INDEXES:
        await db.execute(sql)
    await db.commit()
