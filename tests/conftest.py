from collections.abc import AsyncGenerator

import aiosqlite
import pytest

from src.db.schema import create_schema


@pytest.fixture
async def db() -> AsyncGenerator[aiosqlite.Connection]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await create_schema(conn)
    yield conn
    await conn.close()
