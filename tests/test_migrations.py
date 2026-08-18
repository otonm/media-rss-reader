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
    a crash in between) replays v8 as a duplicate column, which run_migrations
    must swallow, then applies v9 onward for the first time. Both entry paths
    must leave feeds identical."""

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
    # v8 is MIGRATIONS[7]. Apply v1..v7 normally, then apply v8's ALTER without
    # its version bump: DDL auto-commits ahead of the bump, so a crash right
    # there leaves user_version at 7 even though site_link already exists.
    for i, step in enumerate(mig_mod.MIGRATIONS[:7], start=1):
        await old.execute(step)
        await old.execute(f"PRAGMA user_version = {i}")
    await old.execute(mig_mod.MIGRATIONS[7])
    await old.commit()
    await mig_mod.run_migrations(old)  # replays v8 (duplicate column swallowed), then v9..latest
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


async def test_backfill_seen_media_from_items(tmp_path: Path) -> None:
    """v19 records pre-existing seen rows with the URL normalised, then the
    version gate makes a second run a no-op.

    Builds its own pre-v19 database rather than using the `db` fixture: that
    fixture runs every migration up to and including v25's DROP TABLE
    seen_guids, and rewinding PRAGMA user_version afterwards cannot undo a
    DROP that already happened on the same connection.
    """
    db = await open_db(str(tmp_path / "f.db"))
    await create_schema(db)
    target = mig_mod.MIGRATIONS.index(mig_mod._backfill_seen_media)
    for i, step in enumerate(MIGRATIONS[:target], start=1):
        if callable(step):
            await step(db)
        else:
            await db.execute(step)
        await db.execute(f"PRAGMA user_version = {i}")
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
    await db.close()


async def test_backfill_seen_media_recovers_and_drops_resurrected_rows(tmp_path: Path) -> None:
    """The state the bug left behind: a seen_guids tombstone whose item came
    back with seen_at NULL. The record is recovered and the row removed.

    Builds its own pre-v19 database — see test_backfill_seen_media_from_items
    for why the shared `db` fixture no longer works for this setup now that
    v25 drops seen_guids.
    """
    db = await open_db(str(tmp_path / "r.db"))
    await create_schema(db)
    target = mig_mod.MIGRATIONS.index(mig_mod._backfill_seen_media)
    for i, step in enumerate(MIGRATIONS[:target], start=1):
        if callable(step):
            await step(db)
        else:
            await db.execute(step)
        await db.execute(f"PRAGMA user_version = {i}")
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
    await db.close()


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


async def test_replay_from_v19_hits_merge_unavailable_guids_guard(tmp_path: Path) -> None:
    """A fully-migrated database, rewound to right before v20's replay range:
    v20 (_merge_unavailable_guids) finds unavailable_guids already dropped by
    v21 in the pass that already ran, and its existence guard must no-op
    rather than raise. v21-v25 replay harmlessly behind it (DROP ... IF
    EXISTS, CREATE ... IF NOT EXISTS, INSERT OR IGNORE). This is the shape
    that drove the guard's existence in the first place — without a test in
    this shape, the guard has no coverage."""
    db = await open_db(str(tmp_path / "v19.db"))
    await create_schema(db)
    await run_migrations(db)

    await db.execute(f"PRAGMA user_version = {mig_mod.MIGRATIONS.index(mig_mod._merge_unavailable_guids)}")
    await db.commit()

    await run_migrations(db)

    async with db.execute("PRAGMA user_version") as cur:
        assert (await cur.fetchone())[0] == len(mig_mod.MIGRATIONS)
    await db.close()


async def test_seen_guids_backfilled_then_dropped(tmp_path: Path) -> None:
    """A pre-v19 database must have its seen history moved before the table goes."""
    db = await open_db(str(tmp_path / "s.db"))
    await create_schema(db)
    # Stop before v19: seen_guids exists and holds a mark, seen_media exists
    # (v14 created it) but is empty — the backfill hasn't run yet.
    target = mig_mod.MIGRATIONS.index(mig_mod._backfill_seen_media)
    for i, step in enumerate(MIGRATIONS[:target], start=1):
        if callable(step):
            await step(db)
        else:
            await db.execute(step)
        await db.execute(f"PRAGMA user_version = {i}")
    await db.execute("INSERT INTO feeds (id, url) VALUES ('f1', 'https://e.com/f')")
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type)"
        " VALUES ('i1', 'f1', 'g1', 'https://example.com/a.jpg', 'image')"
    )
    await db.execute("INSERT INTO seen_guids (feed_id, guid, seen_at) VALUES ('f1', 'g1', '2026-01-01 00:00:00')")
    await db.commit()

    await run_migrations(db)

    async with db.execute("SELECT COUNT(*) FROM seen_media") as cur:
        assert (await cur.fetchone())[0] == 1, "the seen mark must survive the backfill"
    async with db.execute("SELECT name FROM sqlite_master WHERE name='seen_guids'") as cur:
        assert await cur.fetchone() is None, "the drained table must be gone"
    await db.close()


async def test_v24_backfills_every_slide_url(tmp_path: Path) -> None:
    """The backfill must reproduce every URL item_slides yields, including
    non-ASCII, and must not raise on the pathological media_json shapes
    json_extract() cannot parse directly: an object at top level, an array of
    bare strings, unparseable text, and NULL. Each row must still contribute
    at least its media_url."""
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
    pathological = [
        ("obj", '{"url":"https://example.com/obj.jpg"}'),
        ("arrstr", '["https://example.com/str.jpg"]'),
        ("unparse", "not json"),
        ("nullmedia", None),
    ]
    for item_id, media_json in pathological:
        await db.execute(
            "INSERT INTO items (id, feed_id, guid, media_url, media_type, media_json)"
            " VALUES (?, 'f1', ?, ?, 'image', ?)",
            (item_id, item_id, f"https://example.com/{item_id}.jpg", media_json),
        )
    await db.commit()

    await run_migrations(db)  # must not raise

    async with db.execute("SELECT url FROM media_urls WHERE item_id = 'i1' ORDER BY url") as cur:
        urls = {row["url"] for row in await cur.fetchall()}
    assert urls == {"https://example.com/a.jpg", slide}

    for item_id, _ in pathological:
        async with db.execute("SELECT url FROM media_urls WHERE item_id = ?", (item_id,)) as cur:
            urls = {row["url"] for row in await cur.fetchall()}
        assert f"https://example.com/{item_id}.jpg" in urls
    await db.close()
