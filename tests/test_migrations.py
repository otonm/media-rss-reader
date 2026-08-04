import aiosqlite

import src.db.migrations as mig_mod
from src.db.connection import open_db
from src.db.schema import create_schema


async def test_migration_applies() -> None:
    """Test that a pending migration is applied and user_version bumped."""
    conn = await open_db(":memory:")
    await create_schema(conn)
    original = mig_mod.MIGRATIONS[:]
    base_version = len(original)
    mig_mod.MIGRATIONS.append("CREATE TABLE IF NOT EXISTS _test_mig (id INTEGER PRIMARY KEY)")
    try:
        await mig_mod.run_migrations(conn)

        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        assert row[0] == base_version + 1

        async with conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='_test_mig'") as cur:
            row = await cur.fetchone()
        assert row is not None

        # Run again — should be a no-op (already at latest version)
        await mig_mod.run_migrations(conn)
        async with conn.execute("PRAGMA user_version") as cur:
            row2 = await cur.fetchone()
        assert row2[0] == base_version + 1
    finally:
        mig_mod.MIGRATIONS[:] = original
        await conn.close()


async def test_media_url_lookup_uses_index() -> None:
    """The availability helper looks items up by media_url inside a write
    transaction — without an index that is a full scan holding the writer lock."""
    conn = await open_db(":memory:")
    await create_schema(conn)
    await mig_mod.run_migrations(conn)

    async with conn.execute("EXPLAIN QUERY PLAN SELECT id FROM items WHERE media_url = ?", ("x",)) as cur:
        plan = " ".join(str(row["detail"]) for row in await cur.fetchall())
    assert "idx_items_media_url" in plan, plan
    await conn.close()


async def test_multiple_migrations_apply_in_order() -> None:
    """Test that multiple pending migrations are applied sequentially."""
    conn = await open_db(":memory:")
    await create_schema(conn)
    original = mig_mod.MIGRATIONS[:]
    base_version = len(original)
    mig_mod.MIGRATIONS.append("CREATE TABLE IF NOT EXISTS _mig_a (id INTEGER PRIMARY KEY)")
    mig_mod.MIGRATIONS.append("CREATE TABLE IF NOT EXISTS _mig_b (id INTEGER PRIMARY KEY)")
    try:
        await mig_mod.run_migrations(conn)

        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        assert row[0] == base_version + 2

        for table in ("_mig_a", "_mig_b"):
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ) as cur:
                assert await cur.fetchone() is not None
    finally:
        mig_mod.MIGRATIONS[:] = original
        await conn.close()


async def test_backfill_seen_media_from_items(db: aiosqlite.Connection) -> None:
    """Rows still marked seen become seen_media records, with the URL normalised."""
    await db.execute("INSERT INTO feeds (id, url) VALUES ('f1', 'http://f1.com')")
    await db.execute(
        """INSERT INTO items (id, feed_id, guid, media_url, media_type, seen_at)
           VALUES ('i1', 'f1', 'g1', 'http://cdn.example.com/a.jpg?w=640', 'image', datetime('now'))"""
    )
    await db.commit()

    await mig_mod.backfill_seen_media(db)

    async with db.execute("SELECT media_key FROM seen_media") as cur:
        assert [r["media_key"] for r in await cur.fetchall()] == ["http://cdn.example.com/a.jpg"]


async def test_backfill_seen_media_recovers_and_drops_resurrected_rows(db: aiosqlite.Connection) -> None:
    """The state the bug left behind: a seen_guids tombstone whose item came
    back with seen_at NULL. The record is recovered and the row removed."""
    await db.execute("INSERT INTO feeds (id, url) VALUES ('f1', 'http://f1.com')")
    await db.execute(
        """INSERT INTO items (id, feed_id, guid, media_url, media_key, media_type, seen_at)
           VALUES ('i1', 'f1', 'g1', 'http://cdn.example.com/a.jpg',
                   'http://cdn.example.com/a.jpg', 'image', NULL)"""
    )
    await db.execute("INSERT INTO seen_guids (feed_id, guid, seen_at) VALUES ('f1', 'g1', datetime('now'))")
    await db.commit()

    await mig_mod.backfill_seen_media(db)

    async with db.execute("SELECT media_key FROM seen_media") as cur:
        assert [r["media_key"] for r in await cur.fetchall()] == ["http://cdn.example.com/a.jpg"]
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 0
