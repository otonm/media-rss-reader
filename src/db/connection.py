from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite

from src.config import settings


async def open_db(path: str | None = None) -> aiosqlite.Connection:
    path_str = path or settings.db_path
    Path(path_str).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(path_str)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    db = await open_db()
    try:
        yield db
    finally:
        await db.close()
