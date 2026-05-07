"""Integer-versioned schema migrations.

MIGRATIONS is an ordered list of SQL statements. PRAGMA user_version stores
the count of applied migrations. On every startup, any statements from
MIGRATIONS[current_version:] are applied in sequence, with user_version
incremented after each one.

To add a migration: append one SQL string to MIGRATIONS. Never edit or
reorder existing entries — doing so would corrupt the version counter.
"""

import aiosqlite

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
    "INSERT OR IGNORE INTO seen_guids (feed_id, guid, seen_at) SELECT feed_id, guid, seen_at FROM items WHERE seen_at IS NOT NULL",
]


async def run_migrations(db: aiosqlite.Connection) -> None:
    """Apply any pending migrations and advance the version counter."""
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    current_version: int = row[0]

    pending = MIGRATIONS[current_version:]
    if not pending:
        return

    for i, sql in enumerate(pending, start=current_version + 1):
        await db.execute(sql)
        # Commit version update immediately so a crash mid-migration leaves a
        # consistent state — partially applied migrations are not retried.
        await db.execute(f"PRAGMA user_version = {i}")
        await db.commit()
