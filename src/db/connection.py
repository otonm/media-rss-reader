from collections.abc import AsyncGenerator

import aiosqlite

from src.config import settings


async def open_db(path: str | None = None) -> aiosqlite.Connection:
    db = await aiosqlite.connect(path or settings.db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def get_db() -> AsyncGenerator[aiosqlite.Connection]:
    db = await open_db()
    try:
        yield db
    finally:
        await db.close()
