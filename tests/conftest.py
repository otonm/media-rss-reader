import os

# Auth env vars must be set before src.config is imported (it instantiates Settings() at module level).
os.environ.setdefault("AUTH_USERNAME", "testuser")
os.environ.setdefault("AUTH_PASSWORD", "testpassword")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-minimum-32-chars!!")

from collections.abc import AsyncGenerator, AsyncIterator

import aiosqlite
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport
from httpx import AsyncClient as HttpxAsyncClient

from src.api import feeds as feeds_router
from src.api import items as items_router
from src.api import media as media_router
from src.db.connection import get_db, open_db
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


@pytest.fixture
async def client(db: aiosqlite.Connection) -> AsyncGenerator[HttpxAsyncClient]:
    test_app = FastAPI()
    test_app.include_router(feeds_router.router, prefix="/api")
    test_app.include_router(items_router.router, prefix="/api")
    test_app.include_router(media_router.router, prefix="/api")

    async def _override_db() -> AsyncIterator[aiosqlite.Connection]:
        yield db

    test_app.dependency_overrides[get_db] = _override_db

    async with HttpxAsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as c:
        yield c
