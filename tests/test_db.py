import os
import tempfile

import aiosqlite

from src.db.connection import get_db, open_db
from src.db.migrations import MIGRATIONS, run_migrations
from src.db.schema import create_schema


async def test_schema_creates_feeds_table() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='feeds'") as cur:
        row = await cur.fetchone()
    assert row is not None
    await db.close()


async def test_schema_creates_items_table() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='items'") as cur:
        row = await cur.fetchone()
    assert row is not None
    await db.close()


async def test_schema_creates_indexes() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    async with db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name LIKE 'idx_items_%'") as cur:
        row = await cur.fetchone()
    assert row[0] == 3
    await db.close()


async def test_schema_is_idempotent() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    await create_schema(db)  # must not raise
    await db.close()


async def test_db_fixture_has_row_factory(db: aiosqlite.Connection) -> None:
    await db.execute("INSERT INTO feeds (id, url) VALUES ('x', 'https://example.com')")
    await db.commit()
    async with db.execute("SELECT id FROM feeds") as cur:
        row = await cur.fetchone()
    assert row["id"] == "x"


async def test_migrations_sets_user_version() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    await run_migrations(db)
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    assert row[0] == len(MIGRATIONS)
    await db.close()


async def test_migrations_are_idempotent() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    await run_migrations(db)
    await run_migrations(db)  # second call must not re-apply
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    assert row[0] == len(MIGRATIONS)
    await db.close()


async def test_open_db_sets_wal_and_fk() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = await open_db(path)
        async with db.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        assert row[0] == "wal"
        async with db.execute("PRAGMA foreign_keys") as cur:
            row = await cur.fetchone()
        assert row[0] == 1
        await db.close()
    finally:
        os.unlink(path)


async def test_open_db_sets_row_factory() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = await open_db(path)
        await db.execute("CREATE TABLE t (x INTEGER)")
        await db.execute("INSERT INTO t VALUES (42)")
        async with db.execute("SELECT x FROM t") as cur:
            row = await cur.fetchone()
        assert row["x"] == 42
        await db.close()
    finally:
        os.unlink(path)


async def test_get_db_yields_connection(tmp_path: object) -> None:
    import pathlib

    import src.db.connection as conn_mod

    db_path = pathlib.Path(str(tmp_path)) / "test.db"
    original = conn_mod.settings.db_path
    conn_mod.settings.db_path = str(db_path)  # type: ignore[assignment]
    try:
        db_gen = get_db()
        conn = await db_gen.__anext__()
        assert isinstance(conn, aiosqlite.Connection)
        await conn.close()
    finally:
        conn_mod.settings.db_path = original  # type: ignore[assignment]
