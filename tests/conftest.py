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

    async with HttpxAsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
        yield c


import src.auth.routes as _auth_routes  # noqa: E402
from src.auth.middleware import AuthMiddleware  # noqa: E402
from src.auth.session import SESSION_COOKIE, sign_session  # noqa: E402
from src.config import settings  # noqa: E402


@pytest.fixture
def auth_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override settings attributes for auth tests. Resets after each test."""
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "auth_username", "admin")
    monkeypatch.setattr(settings, "auth_password", SecretStr("hunter2"))
    monkeypatch.setattr(settings, "auth_secret_key", SecretStr("test-secret-key-minimum-32-chars!!"))
    monkeypatch.setattr(settings, "auth_lockout_attempts", 5)
    monkeypatch.setattr(settings, "auth_lockout_minutes", 15)


@pytest.fixture
async def auth_client(
    db: aiosqlite.Connection,
    auth_settings: None,
) -> AsyncGenerator[HttpxAsyncClient]:
    """Test client with auth routes + middleware. Resets lockout state each test."""
    from src.auth.lockout import LockoutTracker

    # Re-create module-level lockout so failures from one test don't bleed into the next.
    _auth_routes._lockout = LockoutTracker(
        max_attempts=settings.auth_lockout_attempts,
        lockout_seconds=settings.auth_lockout_minutes * 60,
    )

    test_app = FastAPI()
    test_app.add_middleware(AuthMiddleware)
    test_app.include_router(_auth_routes.router)

    @test_app.get("/")
    async def _root() -> dict[str, str]:
        return {"status": "ok"}

    async def _override_db() -> AsyncIterator[aiosqlite.Connection]:
        yield db

    test_app.dependency_overrides[get_db] = _override_db

    async with HttpxAsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
        headers={"x-forwarded-proto": "https"},
        follow_redirects=False,
    ) as c:
        yield c


@pytest.fixture
async def authed_client(auth_client: HttpxAsyncClient) -> HttpxAsyncClient:
    """auth_client pre-loaded with a valid session cookie."""
    token = sign_session(settings.auth_secret_key.get_secret_value())
    auth_client.cookies.set(SESSION_COOKIE, token)
    return auth_client
