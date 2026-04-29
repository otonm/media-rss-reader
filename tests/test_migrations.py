import src.db.migrations as mig_mod
from src.db.connection import open_db


async def test_migration_applies() -> None:
    """Test that a pending migration is applied and user_version bumped."""
    conn = await open_db(":memory:")
    original = mig_mod.MIGRATIONS[:]
    mig_mod.MIGRATIONS.append("CREATE TABLE IF NOT EXISTS _test_mig (id INTEGER PRIMARY KEY)")
    try:
        await mig_mod.run_migrations(conn)

        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        assert row[0] == 1

        async with conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='_test_mig'") as cur:
            row = await cur.fetchone()
        assert row is not None

        # Run again — should be a no-op (already at version 1)
        await mig_mod.run_migrations(conn)
        async with conn.execute("PRAGMA user_version") as cur:
            row2 = await cur.fetchone()
        assert row2[0] == 1
    finally:
        mig_mod.MIGRATIONS[:] = original
        await conn.close()


async def test_multiple_migrations_apply_in_order() -> None:
    """Test that multiple pending migrations are applied sequentially."""
    conn = await open_db(":memory:")
    original = mig_mod.MIGRATIONS[:]
    mig_mod.MIGRATIONS.append("CREATE TABLE IF NOT EXISTS _mig_a (id INTEGER PRIMARY KEY)")
    mig_mod.MIGRATIONS.append("CREATE TABLE IF NOT EXISTS _mig_b (id INTEGER PRIMARY KEY)")
    try:
        await mig_mod.run_migrations(conn)

        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        assert row[0] == 2

        for table in ("_mig_a", "_mig_b"):
            async with conn.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            ) as cur:
                assert await cur.fetchone() is not None
    finally:
        mig_mod.MIGRATIONS[:] = original
        await conn.close()
