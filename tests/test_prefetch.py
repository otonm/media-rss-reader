import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import aiosqlite
import httpx
import pytest
import respx

from src.media import cache as cache_mod
from src.media import fetch as fetch_mod
from src.media import prefetch as prefetch_mod
from src.media.prefetch import _warm, prefetch_ahead


def _pinned(url: str) -> str:
    """The url as open_upstream now sends it: host replaced by the stubbed IP."""
    return fetch_mod._pinned_url(url, "93.184.216.34")


async def test_warm_on_cache_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_warm fetches and caches when URL is not in cache."""
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/img.jpg"

    with respx.mock:
        respx.get(_pinned(url)).mock(
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
        respx.get(_pinned(url)).mock(return_value=httpx.Response(404))
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
        respx.get(_pinned("http://example.com/img.jpg")).mock(return_value=httpx.Response(200, content=b"data"))
        async with httpx.AsyncClient() as client:
            await prefetch_ahead("item0", conn, client, unseen=True)
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
        respx.get(_pinned("http://example.com/1.jpg")).mock(return_value=httpx.Response(200, content=b"d"))
        respx.get(_pinned("http://example.com/2.jpg")).mock(return_value=httpx.Response(200, content=b"d"))
        async with httpx.AsyncClient() as client:
            await prefetch_ahead("i0", conn, client, unseen=True)
            await asyncio.sleep(0.1)

    # i0 is the oldest (pub_date oldest). Ahead = i1, i2 (next in ASC order),
    # NOT items with smaller pub_date (there are none older than i0).
    assert cache_mod.cache_read("http://example.com/1.jpg") is not None
    assert cache_mod.cache_read("http://example.com/2.jpg") is not None
    await conn.close()


async def test_prefetch_ahead_warms_seen_items_when_unseen_false(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R12: with the show-seen toggle on, the page asks for unseen=false while
    the hint warmed only unseen items — so the items the user is about to view
    were never warmed."""
    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/feed', 'F')")
    for n in (1, 2):
        await db.execute(
            """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date, seen_at)
               VALUES (?, 'f1', ?, 'T', ?, 'image', ?, '2026-01-01T00:00:00')""",
            (f"i{n}", f"g{n}", f"http://x.com/{n}.jpg", f"2026-01-0{n}T00:00:00"),
        )
    await db.commit()

    warmed: list[str] = []

    async def _fake_warm(item_id: str, url: str, client: object, request_id: str | None = None) -> None:
        warmed.append(url)

    monkeypatch.setattr("src.media.prefetch._warm", _fake_warm)

    async with httpx.AsyncClient() as client:
        await prefetch_ahead("i1", db, client, unseen=False)
        await asyncio.gather(*list(prefetch_mod._bg_tasks))

    assert warmed == ["http://x.com/2.jpg"]


async def test_prefetch_ahead_returns_none_for_an_unknown_item(
    db: aiosqlite.Connection, mock_http: respx.MockRouter
) -> None:
    """The hint endpoint ran its own SELECT to produce F16's 404, duplicating
    the lookup prefetch_ahead already opens with — and timing only that
    duplicate, while the two ROW_NUMBER scans that are the endpoint's actual
    cost were excluded from the number the log called db=."""
    import httpx

    from src.media.prefetch import prefetch_ahead

    async with httpx.AsyncClient() as client:
        assert await prefetch_ahead("nonexistent", db, client, unseen=True) is None


async def test_prefetch_ahead_drops_the_hint_when_the_backlog_is_full(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_sem bounds how many tasks run, but a task blocked on it is still live
    and strongly referenced, so a fast scroller grew the hint backlog
    monotonically and it drained against a window scrolled past minutes ago.
    The comment claimed the semaphore fixed 'unbounded tasks and outbound
    connections'; it fixed the connections half (minor 12)."""
    # Set up DB with multiple items (so prefetch_ahead would normally queue something)
    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/feed', 'F')")
    for i in range(10):
        await db.execute(
            "INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date) "
            "VALUES (?, 'f1', ?, 'T', ?, 'image', datetime('now', ?))",
            (f"item{i}", f"guid{i}", f"http://example.com/{i}.jpg", f"-{i} seconds"),
        )
    await db.commit()

    # Mock _warm so prefetch_ahead can queue tasks without making HTTP requests
    async def _fake_warm(item_id: str, url: str, client: object, request_id: str | None = None) -> None:
        pass

    monkeypatch.setattr("src.media.prefetch._warm", _fake_warm)

    # Fill the hint backlog to MAX_BACKLOG with dummy tasks, as prefetch_ahead
    # itself would across many scroll-driven hints.
    async def _never() -> None:
        await asyncio.Event().wait()

    filler = [asyncio.create_task(_never()) for _ in range(prefetch_mod.MAX_BACKLOG)]
    for t in filler:
        prefetch_mod._track_hint(t)

    # Verify the hint backlog is full
    assert len(prefetch_mod._hint_tasks) == prefetch_mod.MAX_BACKLOG

    # Now prefetch_ahead should return 0 immediately without attempting to queue anything
    async with httpx.AsyncClient() as client:
        queued = await prefetch_mod.prefetch_ahead("item0", db, client, unseen=False)

    # When backlog is full, it returns 0 (not None, which would mean item not found)
    assert queued == 0

    # Clean up filler tasks
    for t in filler:
        t.cancel()


async def test_prefetch_ahead_queues_despite_a_full_startup_warm_backlog(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """warm_startup_cache queues one tracked task per row of its startup query
    (CACHE_MAX_ITEMS, 500 by default) — ten times MAX_BACKLOG — into the same
    _bg_tasks set _track uses for GC-safety. Before separating the hint path's
    own counter, checking len(_bg_tasks) meant every hint was dropped for the
    whole cold-cache window after every restart, until the startup warm had
    drained below the cap."""
    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/feed', 'F')")
    for i in range(2):
        await db.execute(
            "INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date) "
            "VALUES (?, 'f1', ?, 'T', ?, 'image', datetime('now', ?))",
            (f"item{i}", f"guid{i}", f"http://example.com/{i}.jpg", f"-{1 - i} seconds"),
        )
    await db.commit()

    async def _fake_warm(item_id: str, url: str, client: object, request_id: str | None = None) -> None:
        pass

    monkeypatch.setattr("src.media.prefetch._warm", _fake_warm)

    # Simulate a post-boot startup warm: many tasks in _bg_tasks, well above
    # MAX_BACKLOG, none of them hint-tracked.
    async def _never() -> None:
        await asyncio.Event().wait()

    filler = [asyncio.create_task(_never()) for _ in range(prefetch_mod.MAX_BACKLOG * 10)]
    for t in filler:
        prefetch_mod._track(t)

    assert len(prefetch_mod._bg_tasks) >= prefetch_mod.MAX_BACKLOG * 10
    assert len(prefetch_mod._hint_tasks) == 0

    async with httpx.AsyncClient() as client:
        queued = await prefetch_mod.prefetch_ahead("item0", db, client, unseen=False)

    assert queued == 1, "a full startup-warm backlog must not drop the hint"

    for t in filler:
        t.cancel()


async def test_warm_startup_cache_follows_the_feed_order(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The startup warm must fill the cache in the order /api/items serves.

    It used to warm `ORDER BY pub_date DESC LIMIT CACHE_MAX_ITEMS` — the newest
    items globally — while the feed interleaves feeds and runs OLDEST-first
    within each, filtered to unseen by default. At KEEP_ITEMS=1000 /
    CACHE_MAX_ITEMS=500 that warmed exactly the half of the library the reader
    reaches last, so page one was a guaranteed miss and the browser queue's
    cachedFirst() had nothing to prefer.
    """
    from src.api.items import list_items
    from src.media.prefetch import warm_startup_cache

    # Pinned so the assertions below depend on the seeded rows, not on whatever
    # the config defaults happen to be: the bound must cover all 8 rows.
    monkeypatch.setattr(prefetch_mod.settings, "feed_initial_count", 10)
    monkeypatch.setattr(prefetch_mod.settings, "prefetch_ahead", 5)

    for feed in ("f1", "f2"):
        await db.execute("INSERT INTO feeds(id, url, title) VALUES (?, ?, 'F')", (feed, f"http://x.com/{feed}"))
    # Interleaved pub_dates across the two feeds, plus one seen row per feed
    # that is OLDER than every unseen one — under a plain interleave those sort
    # first, so they prove the unseen-ahead-of-seen half of the ordering too.
    for feed in ("f1", "f2"):
        await db.execute(
            """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date, seen_at)
               VALUES (?, ?, ?, 'T', ?, 'image', '2026-01-01T00:00:00', '2026-02-01T00:00:00')""",
            (f"{feed}-seen", feed, f"{feed}-seen", f"http://x.com/{feed}-seen.jpg"),
        )
        for n in range(3):
            await db.execute(
                """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
                   VALUES (?, ?, ?, 'T', ?, 'image', ?)""",
                (f"{feed}-{n}", feed, f"{feed}-{n}", f"http://x.com/{feed}-{n}.jpg", f"2026-03-0{n + 1}T00:00:00"),
            )
    await db.commit()

    warmed: list[str] = []

    async def _fake_warm(item_id: str, url: str, client: object, request_id: str | None = None) -> None:
        warmed.append(url)

    monkeypatch.setattr("src.media.prefetch._warm", _fake_warm)

    async with httpx.AsyncClient() as client:
        await warm_startup_cache(db, client)
        await asyncio.gather(*list(prefetch_mod._bg_tasks))

    served = [item["media_url"] for item in await list_items(unseen=True, size=6, db=db)]
    assert warmed[: len(served)] == served, "the warm order must match the order the feed serves"
    # The seen rows are warmed, but last — they are the only ones the reader
    # sees with the show-seen toggle on, and they are behind the default view.
    assert sorted(warmed[len(served) :]) == ["http://x.com/f1-seen.jpg", "http://x.com/f2-seen.jpg"]


async def test_warm_startup_cache_stops_at_the_first_page_plus_lookahead(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The startup warm covers the cold-start gap only, and truncates the tail
    of the feed order — the items the reader reaches last.

    It used to be bounded by CACHE_MAX_ITEMS (500), so a cold start fired up to
    500 upstream fetches before the reader had opened anything. Since a
    permanently-gone URL deletes its item, that boot sweep was also an
    unattended mass-deletion window. Past this bound the hint path warms on
    demand, which is what prefetch_ahead is for.
    """
    monkeypatch.setattr(prefetch_mod.settings, "feed_initial_count", 2)
    monkeypatch.setattr(prefetch_mod.settings, "prefetch_ahead", 1)
    from src.media.prefetch import warm_startup_cache

    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/f', 'F')")
    for n in range(9):
        await db.execute(
            """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
               VALUES (?, 'f1', ?, 'T', ?, 'image', ?)""",
            (f"i{n}", f"g{n}", f"http://x.com/{n}.jpg", f"2026-03-0{n + 1}T00:00:00"),
        )
    await db.commit()

    warmed: list[str] = []

    async def _fake_warm(item_id: str, url: str, client: object, request_id: str | None = None) -> None:
        warmed.append(url)

    monkeypatch.setattr("src.media.prefetch._warm", _fake_warm)

    async with httpx.AsyncClient() as client:
        await warm_startup_cache(db, client)
        await asyncio.gather(*list(prefetch_mod._bg_tasks))

    assert warmed == ["http://x.com/0.jpg", "http://x.com/1.jpg", "http://x.com/2.jpg"]


async def test_warm_startup_cache_ignores_cache_max_items(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CACHE_MAX_ITEMS is the eviction budget, not a warm-queue depth. A large
    one must not turn startup back into a bulk upstream sweep."""
    monkeypatch.setattr(prefetch_mod.settings, "cache_max_items", 500)
    monkeypatch.setattr(prefetch_mod.settings, "feed_initial_count", 2)
    monkeypatch.setattr(prefetch_mod.settings, "prefetch_ahead", 1)
    from src.media.prefetch import warm_startup_cache

    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/f', 'F')")
    for n in range(20):
        await db.execute(
            """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
               VALUES (?, 'f1', ?, 'T', ?, 'image', datetime('now', ?))""",
            (f"i{n}", f"g{n}", f"http://x.com/{n}.jpg", f"-{20 - n} minutes"),
        )
    await db.commit()

    warmed: list[str] = []

    async def _fake_warm(item_id: str, url: str, client: object, request_id: str | None = None) -> None:
        warmed.append(url)

    monkeypatch.setattr("src.media.prefetch._warm", _fake_warm)

    async with httpx.AsyncClient() as client:
        await warm_startup_cache(db, client)
        await asyncio.gather(*list(prefetch_mod._bg_tasks))

    assert len(warmed) == 3


async def test_warm_startup_cache_makes_no_requests_for_cached_items(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart with a warm cache must issue no upstream requests at all.

    _warm returns on a cache_read hit before opening a connection, so already
    cached items are never re-checked — and so never re-tested for liveness,
    which is why a warm restart cannot delete anything.

    Asserted on route call counts, not on NoMatchFound: fetch_to_cache catches
    every exception, so an unmocked request would be swallowed and this would
    pass whether or not the skip works.
    """
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    from src.media.prefetch import warm_startup_cache

    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/f', 'F')")
    for n in range(3):
        url = f"http://example.com/{n}.jpg"
        await db.execute(
            """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
               VALUES (?, 'f1', ?, 'T', ?, 'image', ?)""",
            (f"i{n}", f"g{n}", url, f"2026-03-0{n + 1}T00:00:00"),
        )

        async def _data(n: int = n) -> AsyncGenerator[bytes]:
            yield f"cached{n}".encode()

        await cache_mod.cache_stream_write(url, _data())
    await db.commit()

    with respx.mock:
        routes = [
            respx.get(_pinned(f"http://example.com/{n}.jpg")).mock(
                return_value=httpx.Response(200, content=b"refetched", headers={"content-type": "image/jpeg"})
            )
            for n in range(3)
        ]
        async with httpx.AsyncClient() as client:
            await warm_startup_cache(db, client)
            await asyncio.gather(*list(prefetch_mod._bg_tasks))

    assert [r.call_count for r in routes] == [0, 0, 0], "a cached item must not be fetched again"
    for n in range(3):
        path = cache_mod.cache_read(f"http://example.com/{n}.jpg")
        assert path is not None
        assert path.read_bytes() == f"cached{n}".encode()
