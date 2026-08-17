import json
from pathlib import Path

import aiosqlite

import src.db.migrations as mig_mod
from src.db.connection import open_db
from src.db.migrations import MIGRATIONS, run_migrations
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


async def test_feeds_columns_fresh_vs_v1_rollback() -> None:
    """schema.py is frozen at the v1 shape — site_link belongs to v8, not
    CREATE TABLE. A database whose v8 version bump was lost (DDL auto-commits,
    a crash in between) replays every ALTER as a duplicate column, which
    run_migrations must swallow. Both entry paths must leave feeds identical."""

    async def feeds_columns(conn: aiosqlite.Connection) -> set[str]:
        async with conn.execute("PRAGMA table_info(feeds)") as cur:
            return {row["name"] for row in await cur.fetchall()}

    fresh = await open_db(":memory:")
    await create_schema(fresh)
    assert "site_link" not in await feeds_columns(fresh)
    await mig_mod.run_migrations(fresh)
    migrated = await feeds_columns(fresh)

    old = await open_db(":memory:")
    await create_schema(old)
    await mig_mod.run_migrations(old)
    await old.execute("PRAGMA user_version = 7")  # v8 is MIGRATIONS[7]; its bump was lost
    await old.commit()
    await mig_mod.run_migrations(old)  # replays v8..v19, duplicates swallowed
    assert await feeds_columns(old) == migrated

    await fresh.close()
    await old.close()


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
            async with conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)) as cur:
                assert await cur.fetchone() is not None
    finally:
        mig_mod.MIGRATIONS[:] = original
        await conn.close()


async def test_backfill_seen_media_from_items(db: aiosqlite.Connection) -> None:
    """v19 records pre-existing seen rows with the URL normalised, then the
    version gate makes a second run a no-op."""
    # 18: pins v19 (_backfill_seen_media) as pending. len(MIGRATIONS) - 1 would
    # retarget silently every time a later task appends another migration.
    await db.execute("PRAGMA user_version = 18")
    await db.execute("INSERT INTO feeds (id, url) VALUES ('f1', 'http://f1.com')")
    await db.execute(
        """INSERT INTO items (id, feed_id, guid, media_url, media_type, seen_at)
           VALUES ('i1', 'f1', 'g1', 'http://cdn.example.com/a.jpg?w=640', 'image', datetime('now'))"""
    )
    await db.commit()

    await mig_mod.run_migrations(db)

    async with db.execute("SELECT media_key FROM seen_media") as cur:
        assert [r["media_key"] for r in await cur.fetchall()] == ["http://cdn.example.com/a.jpg"]

    await mig_mod.run_migrations(db)
    async with db.execute("SELECT media_key FROM seen_media") as cur:
        assert [r["media_key"] for r in await cur.fetchall()] == ["http://cdn.example.com/a.jpg"]


async def test_backfill_seen_media_recovers_and_drops_resurrected_rows(db: aiosqlite.Connection) -> None:
    """The state the bug left behind: a seen_guids tombstone whose item came
    back with seen_at NULL. The record is recovered and the row removed."""
    # 18: pins v19 (_backfill_seen_media) as pending. len(MIGRATIONS) - 1 would
    # retarget silently every time a later task appends another migration.
    await db.execute("PRAGMA user_version = 18")
    await db.execute("INSERT INTO feeds (id, url) VALUES ('f1', 'http://f1.com')")
    await db.execute(
        """INSERT INTO items (id, feed_id, guid, media_url, media_key, media_type, seen_at)
           VALUES ('i1', 'f1', 'g1', 'http://cdn.example.com/a.jpg',
                   'http://cdn.example.com/a.jpg', 'image', NULL)"""
    )
    await db.execute("INSERT INTO seen_guids (feed_id, guid, seen_at) VALUES ('f1', 'g1', datetime('now'))")
    await db.commit()

    await mig_mod.run_migrations(db)

    async with db.execute("SELECT media_key FROM seen_media") as cur:
        assert [r["media_key"] for r in await cur.fetchall()] == ["http://cdn.example.com/a.jpg"]
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_v20_merges_unavailable_guids(tmp_path: Path) -> None:
    """v20 moves unavailable_guids rows into resolved_guids, then v21 drops the table."""
    db = await open_db(str(tmp_path / "m.db"))
    await create_schema(db)
    # Stop before v20 so the old table still exists.
    for i, step in enumerate(MIGRATIONS[:19], start=1):
        if callable(step):
            await step(db)
        else:
            await db.execute(step)
        await db.execute(f"PRAGMA user_version = {i}")
    await db.execute("INSERT INTO feeds (id, url) VALUES ('f1', 'https://e.com/f')")
    await db.execute(
        "INSERT INTO unavailable_guids (feed_id, guid, marked_at) VALUES ('f1', 'g1', '2026-01-01 00:00:00')"
    )
    await db.commit()

    await run_migrations(db)

    async with db.execute("SELECT resolved_at FROM resolved_guids WHERE feed_id='f1' AND guid='g1'") as cur:
        row = await cur.fetchone()
    assert row is not None, "the tombstone must survive the merge"
    assert row["resolved_at"] == "2026-01-01 00:00:00", "the original timestamp must be preserved"

    async with db.execute("SELECT name FROM sqlite_master WHERE name='unavailable_guids'") as cur:
        assert await cur.fetchone() is None, "the old table must be gone"
    await db.close()


async def test_v22_backfills_every_slide_url(tmp_path: Path) -> None:
    """The backfill must reproduce every URL item_slides yields, including non-ASCII."""
    db = await open_db(str(tmp_path / "b.db"))
    await create_schema(db)
    for i, step in enumerate(MIGRATIONS[:21], start=1):
        if callable(step):
            await step(db)
        else:
            await db.execute(step)
        await db.execute(f"PRAGMA user_version = {i}")
    slide = "https://example.com/été/photo.jpg"
    media = json.dumps(
        [
            {"url": "https://example.com/a.jpg", "type": "image"},
            {"url": slide, "type": "image"},
        ]
    )
    await db.execute("INSERT INTO feeds (id, url) VALUES ('f1', 'https://e.com/f')")
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type, media_json)"
        " VALUES ('i1', 'f1', 'g1', 'https://example.com/a.jpg', 'image', ?)",
        (media,),
    )
    await db.commit()

    await run_migrations(db)

    async with db.execute("SELECT url FROM media_urls WHERE item_id = 'i1' ORDER BY url") as cur:
        urls = {row["url"] for row in await cur.fetchall()}
    assert urls == {"https://example.com/a.jpg", slide}
    await db.close()
