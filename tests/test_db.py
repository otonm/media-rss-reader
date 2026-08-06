import os
import tempfile

import aiosqlite
import pytest

from src.db.connection import open_db
from src.db.migrations import MIGRATIONS, run_migrations
from src.db.schema import create_schema


async def test_schema_creates_feeds_table() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='feeds'") as cur:
        row = await cur.fetchone()
    assert row is not None
    await db.close()


async def test_schema_creates_items_table() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='items'") as cur:
        row = await cur.fetchone()
    assert row is not None
    await db.close()


async def test_schema_creates_indexes() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    async with db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_items_%'") as cur:
        names = {row[0] for row in await cur.fetchall()}
    # Named, not counted: a count says nothing about which index went missing.
    # idx_items_feed_pub matches src/db/queries.py's window exactly, so
    # ROW_NUMBER reads it in order instead of sorting the whole table.
    assert names == {"idx_items_feed_id", "idx_items_pub_date", "idx_items_seen_at", "idx_items_feed_pub"}
    await db.close()


async def test_schema_is_idempotent() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    await create_schema(db)  # must not raise
    await db.close()


async def test_db_fixture_has_row_factory(db: aiosqlite.Connection) -> None:
    await db.execute("INSERT INTO feeds (id, url) VALUES ('x', 'https://example.com')")
    await db.commit()
    async with db.execute("SELECT id FROM feeds") as cur:
        row = await cur.fetchone()
    assert row["id"] == "x"


async def test_migrations_sets_user_version() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    await run_migrations(db)
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    assert row[0] == len(MIGRATIONS)
    await db.close()


async def test_migrations_are_idempotent() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    await run_migrations(db)
    await run_migrations(db)  # second call must not re-apply
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    assert row[0] == len(MIGRATIONS)
    await db.close()


async def test_open_db_sets_wal_and_fk() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = await open_db(path)
        async with db.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        assert row[0] == "wal"
        async with db.execute("PRAGMA foreign_keys") as cur:
            row = await cur.fetchone()
        assert row[0] == 1
        await db.close()
    finally:
        os.unlink(path)


async def test_open_db_sets_row_factory() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = await open_db(path)
        await db.execute("CREATE TABLE t (x INTEGER)")
        await db.execute("INSERT INTO t VALUES (42)")
        async with db.execute("SELECT x FROM t") as cur:
            row = await cur.fetchone()
        assert row["x"] == 42
        await db.close()
    finally:
        os.unlink(path)


async def test_get_db_returns_the_process_wide_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_db opened a fresh connection per request: a blocking mkdir, an
    aiosqlite.connect that starts an OS thread, and two PRAGMA round-trips —
    50 of each for one page of media, to answer 50 existence lookups. A
    long-lived connection already existed and was read by nothing.
    """
    from types import SimpleNamespace

    import src.db.connection as conn_mod

    async def _must_not_open(*a: object, **k: object) -> object:
        raise AssertionError("get_db must not open a connection per request")

    monkeypatch.setattr(conn_mod, "open_db", _must_not_open)

    sentinel = object()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=sentinel)))
    assert await conn_mod.get_db(request) is sentinel


def test_both_sides_of_the_interleave_share_one_keyset_predicate() -> None:
    """src/db/queries.py exists to hold these. It got the CTE and the ORDER BY;
    the anchor lookup and the keyset predicate stayed as byte-identical copies
    in items.py and prefetch.py, and the next edit to the tiebreak would have
    had to land in both or the prefetcher warms a different window than the
    page serves."""
    import inspect

    from src.api import items as items_mod
    from src.db.queries import ANCHOR_LOOKUP, KEYSET_AFTER
    from src.media import prefetch as prefetch_mod

    assert "(rn, feed_id, id) > (?, ?, ?)" in KEYSET_AFTER
    assert "FROM ranked WHERE id = ?" in ANCHOR_LOOKUP

    for module in (items_mod, prefetch_mod):
        source = inspect.getsource(module)
        assert "(rn, feed_id, id) > (?, ?, ?)" not in source, (
            f"{module.__name__} has a private copy of the keyset predicate"
        )
