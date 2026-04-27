import aiosqlite

MIGRATIONS: list[str] = []


async def run_migrations(db: aiosqlite.Connection) -> None:
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    current_version: int = row[0]
    pending = MIGRATIONS[current_version:]
    for sql in pending:
        await db.execute(sql)
    new_version = len(MIGRATIONS)
    await db.execute(f"PRAGMA user_version = {new_version}")
    await db.commit()
