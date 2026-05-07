import asyncio
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
            await _warm(url, client)

    path = cache_mod.cache_read(url)
    assert path is not None
    assert path.read_bytes() == b"imgbytes"


async def test_warm_skips_if_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_warm skips the HTTP request when URL is already cached."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/cached.jpg"
    # Pre-populate cache
    await cache_mod.cache_write(url, b"cached")

    with respx.mock:
        # If _warm makes any request, respx will raise NoMatchFound
        async with httpx.AsyncClient() as client:
            await _warm(url, client)  # should not make a request

    # Cache is still intact
    path = cache_mod.cache_read(url)
    assert path is not None
    assert path.read_bytes() == b"cached"


async def test_warm_non_success_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_warm does not cache on non-2xx responses."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/missing.jpg"

    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        async with httpx.AsyncClient() as client:
            await _warm(url, client)

    assert cache_mod.cache_read(url) is None


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
