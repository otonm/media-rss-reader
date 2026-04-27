import aiosqlite

from src.db.migrations import MIGRATIONS, run_migrations
from src.db.schema import create_schema


async def test_schema_creates_feeds_table() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='feeds'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    await db.close()


async def test_schema_creates_items_table() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='items'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    await db.close()


async def test_schema_creates_indexes() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    async with db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name LIKE 'idx_items_%'"
    ) as cur:
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
