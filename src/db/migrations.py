import aiosqlite

MIGRATIONS: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_items_fetched_at ON items(fetched_at)",
]


async def run_migrations(db: aiosqlite.Connection) -> None:
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    current_version: int = row[0]
    pending = MIGRATIONS[current_version:]
    if not pending:
        return
    for i, sql in enumerate(pending, start=current_version + 1):
        await db.execute(sql)
        await db.execute(f"PRAGMA user_version = {i}")
        await db.commit()
