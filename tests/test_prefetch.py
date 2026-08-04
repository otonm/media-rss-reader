import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
import respx

from src.media import cache as cache_mod
from src.media.prefetch import _warm, prefetch_ahead


async def test_warm_on_cache_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_warm fetches and caches when URL is not in cache."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/img.jpg"

    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(200, content=b"imgbytes", headers={"content-type": "image/jpeg"})
        )
        async with httpx.AsyncClient() as client:
            await _warm("item1", url, client)

    path = cache_mod.cache_read(url)
    assert path is not None
    assert path.read_bytes() == b"imgbytes"


async def test_warm_skips_if_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_warm skips the HTTP request when URL is already cached."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/cached.jpg"

    # Pre-populate cache
    async def _data() -> AsyncGenerator[bytes]:
        yield b"cached"

    await cache_mod.cache_stream_write(url, _data())

    with respx.mock:
        # If _warm makes any request, respx will raise NoMatchFound
        async with httpx.AsyncClient() as client:
            await _warm("item1", url, client)  # should not make a request

    # Cache is still intact
    path = cache_mod.cache_read(url)
    assert path is not None
    assert path.read_bytes() == b"cached"


async def test_warm_non_success_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_warm does not cache on non-2xx responses, and records the URL as dead.

    _warm runs as a fire-and-forget task that outlives its caller, so it must open
    its own connection rather than borrow one — a borrowed request-scoped connection
    is already closed by then ("no active connection").
    """
    from src.db import connection as conn_mod
    from src.db.connection import open_db
    from src.db.migrations import run_migrations
    from src.db.schema import create_schema

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    # _warm opens its own connection from settings.db_path — point it at a file DB
    # so the marking is visible from this test's connection.
    monkeypatch.setattr(conn_mod.settings, "db_path", str(tmp_path / "test.db"))
    url = "http://example.com/missing.jpg"

    conn = await open_db()
    await create_schema(conn)
    await run_migrations(conn)

    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        async with httpx.AsyncClient() as client:
            await _warm("item1", url, client)

    assert cache_mod.cache_read(url) is None
    async with conn.execute("SELECT url FROM dead_urls") as cur:
        assert [row["url"] for row in await cur.fetchall()] == [url]
    await conn.close()


async def test_prefetch_ahead_fires_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """prefetch_ahead queues cache-warming tasks for upcoming items."""
    from src.db.connection import open_db
    from src.db.migrations import run_migrations
    from src.db.schema import create_schema

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    conn = await open_db(":memory:")
    await create_schema(conn)
    await run_migrations(conn)

    await conn.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/f', 'F')")
    for i in range(3):
        await conn.execute(
            "INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date) "
            "VALUES (?, 'f1', ?, 'T', 'http://example.com/img.jpg', 'image', datetime('now', ?))",
            (f"item{i}", f"guid{i}", f"-{i} seconds"),
        )
    await conn.commit()

    with respx.mock:
        respx.get("http://example.com/img.jpg").mock(return_value=httpx.Response(200, content=b"data"))
        async with httpx.AsyncClient() as client:
            await prefetch_ahead("item0", conn, client)
            # Allow tasks to run
            await asyncio.sleep(0.1)

    await conn.close()


async def test_background_tasks_are_tracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A created warm task must be retained until it completes (F8)."""
    from src.media import prefetch as pf

    async def slow() -> None:
        await asyncio.sleep(0.05)

    t = asyncio.create_task(slow())
    pf._track(t)
    assert t in pf._bg_tasks
    await t
    assert t not in pf._bg_tasks


async def test_prefetch_ahead_warms_items_ahead_not_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F2: under ASC interleave, 'ahead' = greater (rn, feed_id, id), not
    smaller pub_date. The old query warmed items behind the cursor."""
    from src.db.connection import open_db
    from src.db.migrations import run_migrations
    from src.db.schema import create_schema
    from src.media.prefetch import prefetch_ahead

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    conn = await open_db(":memory:")
    await create_schema(conn)
    await run_migrations(conn)
    await conn.execute("INSERT INTO feeds(id,url,title) VALUES ('f1','http://x','F')")
    for i in range(4):
        await conn.execute(
            "INSERT INTO items(id,feed_id,guid,title,media_url,media_type,pub_date)"
            " VALUES (?, 'f1', ?, 't', ?, 'image', datetime('now', ?))",
            (f"i{i}", f"g{i}", f"http://example.com/{i}.jpg", f"-{3 - i} seconds"),
        )
    await conn.commit()

    with respx.mock:
        respx.get("http://example.com/1.jpg").mock(return_value=httpx.Response(200, content=b"d"))
        respx.get("http://example.com/2.jpg").mock(return_value=httpx.Response(200, content=b"d"))
        async with httpx.AsyncClient() as client:
            await prefetch_ahead("i0", conn, client)
            await asyncio.sleep(0.1)

    # i0 is the oldest (pub_date oldest). Ahead = i1, i2 (next in ASC order),
    # NOT items with smaller pub_date (there are none older than i0).
    assert cache_mod.cache_read("http://example.com/1.jpg") is not None
    assert cache_mod.cache_read("http://example.com/2.jpg") is not None
    await conn.close()
