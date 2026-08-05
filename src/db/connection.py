"""Database connection factory.

open_db() is used by the scheduler (persistent connection held for the process lifetime).
get_db() is a FastAPI dependency returning the connection opened at startup.
run_with_own_db() is for work that outlives the request that started it.
"""

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import aiosqlite
from fastapi import Depends, Request

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


async def get_db(request: Request) -> aiosqlite.Connection:
    """FastAPI dependency: the process-wide connection opened at startup.

    Opening one per request cost a blocking mkdir, an aiosqlite.connect that
    starts an OS thread and two PRAGMA round-trips, on every request including
    the highest-rate route in the app. aiosqlite serialises statements on the
    connection's worker thread, so DB access queues app-wide; with WAL and
    queries this small that is cheaper than a thread per request.

    Work that outlives the request still needs run_with_own_db — a streaming
    body or a warm task running after the route returned must not borrow a
    connection whose lifetime is the request's.
    """
    return request.app.state.db


_DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]


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
