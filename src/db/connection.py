"""Database connection factory.

open_db() is used by the scheduler (persistent connection held for the process lifetime).
get_db() is a FastAPI dependency that opens and closes a connection per request.
run_with_own_db() is for work that outlives the request that started it.
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import aiosqlite

from src.config import settings

logger = logging.getLogger(__name__)


async def open_db(path: str | None = None) -> aiosqlite.Connection:
    """Open an aiosqlite connection with WAL mode and foreign keys enabled.

    Creates the parent directory if it does not yet exist so the container
    can start cleanly even when the data volume is empty.
    """
    path_str = path or settings.db_path
    logger.debug(f"open_db opening {path_str}")
    Path(path_str).parent.mkdir(parents=True, exist_ok=True)
    # timeout is SQLite's busy timeout: how long a writer waits for the lock
    # before raising "database is locked". The 5s default is too tight when a
    # whole feed 404s at once and every request wants the writer lock.
    db = await aiosqlite.connect(path_str, timeout=30)
    # Row objects behave like dicts — access columns by name throughout the codebase.
    db.row_factory = aiosqlite.Row
    # WAL allows concurrent readers while the scheduler is writing.
    await db.execute("PRAGMA journal_mode=WAL")
    # Enforce ON DELETE CASCADE on the items → feeds foreign key.
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """FastAPI dependency: yield a short-lived connection, close on request teardown."""
    logger.debug("get_db opening request-scoped connection")
    db = await open_db()
    try:
        yield db
    finally:
        logger.debug("get_db closing request-scoped connection")
        await db.close()


async def run_with_own_db(
    label: str,
    write: Callable[[aiosqlite.Connection], Awaitable[object]],
) -> None:
    """Run one DB write on a fresh connection, logging and swallowing failures.

    For work that outlives the request that started it: fire-and-forget warm
    tasks, and streaming-response bodies, which run after the route function
    has returned and its request-scoped connection has already been closed.
    Borrowing that connection raises "no active connection" instead.
    """
    try:
        db = await open_db()
        try:
            await write(db)
        finally:
            await db.close()
    except Exception as exc:  # pragma: no cover
        logger.warning(f"{label} failed: {exc}")
