"""Database connection factory.

open_db() is used by the scheduler (persistent connection held for the process lifetime).
get_db() is a FastAPI dependency that opens and closes a connection per request.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite

from src.config import settings


async def open_db(path: str | None = None) -> aiosqlite.Connection:
    """Open an aiosqlite connection with WAL mode and foreign keys enabled.

    Creates the parent directory if it does not yet exist so the container
    can start cleanly even when the data volume is empty.
    """
    path_str = path or settings.db_path
    Path(path_str).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(path_str)
    # Row objects behave like dicts — access columns by name throughout the codebase.
    db.row_factory = aiosqlite.Row
    # WAL allows concurrent readers while the scheduler is writing.
    await db.execute("PRAGMA journal_mode=WAL")
    # Enforce ON DELETE CASCADE on the items → feeds foreign key.
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """FastAPI dependency: yield a short-lived connection, close on request teardown."""
    db = await open_db()
    try:
        yield db
    finally:
        await db.close()
