from collections.abc import AsyncGenerator

import aiosqlite
import pytest
import respx

from src.db.connection import open_db
from src.db.migrations import run_migrations
from src.db.schema import create_schema


@pytest.fixture
async def db() -> AsyncGenerator[aiosqlite.Connection]:
    conn = await open_db(":memory:")
    await create_schema(conn)
    await run_migrations(conn)
    yield conn
    await conn.close()


@pytest.fixture
def mock_http() -> respx.MockRouter:
    with respx.MockRouter() as router:
        yield router
