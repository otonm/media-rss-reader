import aiosqlite

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
