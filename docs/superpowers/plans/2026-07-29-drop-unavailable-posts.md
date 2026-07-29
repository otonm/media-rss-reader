# Drop Unavailable Posts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove posts whose every media URL has returned 404 from `/api/items`, and keep them gone across feed refresh cycles.

**Architecture:** Two new SQLite tables (`dead_urls`, `unavailable_guids`). A new `src/media/availability.py` helper inspects on every proxy/prefetch 404, deletes the post row, and tombstones its `(feed_id, guid)`. `_refresh_feed` skips INSERT for tombstoned guids. Frontend passes `item_id` on the proxy URL so the helper can find the parent item.

**Tech Stack:** FastAPI (Python), aiosqlite, Vanilla JS. Tests: pytest + respx (existing infra).

---

## File Map

| File | Change |
|---|---|
| `src/db/schema.py` | Add `dead_urls` and `unavailable_guids` CREATE TABLE statements to `create_schema()`. |
| `src/db/migrations.py` | Append migrations v6 (`dead_urls`) and v7 (`unavailable_guids`). |
| `src/media/availability.py` | New module: `mark_url_dead_and_maybe_drop()`. |
| `src/api/media.py` | `proxy_media` gains `item_id` query param; on upstream non-success, call the helper. |
| `src/media/prefetch.py` | `_warm` signature gains `item_id`; calls helper on non-success. |
| `src/feeds/sync.py` | `_refresh_feed` loads `unavailable_guids` and skips tombstones. |
| `src/static/cache-queue.js` | Append `&item_id=` to the proxy URL. |
| `src/static/feed-view.js` | Same change for gallery slides. |
| `tests/test_availability.py` | New file: helper tests. |
| `tests/test_sync.py` | Add: refresh skips tombstoned guids. |
| `tests/test_api.py` | Add: proxy 404 deletes item + tombstones. |

---

## Task 1: Add the new tables to the schema

**Files:**
- Modify: `src/db/schema.py` (append new CREATE statements to `create_schema`)
- Modify: `src/db/migrations.py` (append v6, v7)

- [ ] **Step 1: Add the table statements to `src/db/schema.py`**

In `src/db/schema.py`, append these three strings after the existing `_CREATE_SEEN_GUIDS` (which ends at the `);` of the seen_guids statement, around line 49):

```python
# dead_urls records every media URL we've ever seen return 404. Used to
# answer "are all URLs of this item dead?" without re-fetching anything.
# Grows monotonically; not GC'd.
_CREATE_DEAD_URLS = """
CREATE TABLE IF NOT EXISTS dead_urls (
    url       TEXT PRIMARY KEY,
    marked_at TIMESTAMP NOT NULL DEFAULT (datetime('now'))
)
"""

# unavailable_guids tombstones (feed_id, guid) pairs whose item row has
# been deleted because every media URL was dead. _refresh_feed reads
# this to skip re-insert on the next feed poll. Cascade on feed delete.
_CREATE_UNAVAILABLE_GUIDS = """
CREATE TABLE IF NOT EXISTS unavailable_guids (
    feed_id  TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid     TEXT NOT NULL,
    marked_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (feed_id, guid)
)
"""
```

Then update `create_schema` (currently at lines 61-68). Replace:

```python
async def create_schema(db: aiosqlite.Connection) -> None:
    """Create tables and indexes if they do not already exist."""
    await db.execute(_CREATE_FEEDS)
    await db.execute(_CREATE_ITEMS)
    await db.execute(_CREATE_SEEN_GUIDS)
    for sql in _CREATE_INDEXES:
        await db.execute(sql)
    await db.commit()
```

with:

```python
async def create_schema(db: aiosqlite.Connection) -> None:
    """Create tables and indexes if they do not already exist."""
    await db.execute(_CREATE_FEEDS)
    await db.execute(_CREATE_ITEMS)
    await db.execute(_CREATE_SEEN_GUIDS)
    await db.execute(_CREATE_DEAD_URLS)
    await db.execute(_CREATE_UNAVAILABLE_GUIDS)
    for sql in _CREATE_INDEXES:
        await db.execute(sql)
    await db.commit()
```

- [ ] **Step 2: Append migrations v6 and v7 to `src/db/migrations.py`**

In `src/db/migrations.py`, append two new strings to the `MIGRATIONS` list (currently ending at line 33 with the v5 ALTER TABLE comment):

```python
    # v6: dead_urls table — tracks media URLs that have returned 404
    (
        "CREATE TABLE IF NOT EXISTS dead_urls ("
        "url TEXT PRIMARY KEY, "
        "marked_at TIMESTAMP NOT NULL DEFAULT (datetime('now')))"
    ),
    # v7: unavailable_guids tombstone — blocks re-insert of items whose
    # every media URL is in dead_urls
    (
        "CREATE TABLE IF NOT EXISTS unavailable_guids ("
        "feed_id TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE, "
        "guid TEXT NOT NULL, "
        "marked_at TIMESTAMP NOT NULL DEFAULT (datetime('now')), "
        "PRIMARY KEY (feed_id, guid))"
    ),
```

- [ ] **Step 3: Run the existing test suite to verify nothing regressed**

Run: `cd /home/oton/projects/media-rss-reader && uv run pytest`
Expected: PASS. The `db` fixture in `tests/conftest.py` calls both `create_schema` and `run_migrations`, so the new statements get exercised on every test.

- [ ] **Step 4: Commit**

```bash
git add src/db/schema.py src/db/migrations.py
git commit -m "feat(db): add dead_urls and unavailable_guids tables"
```

---

## Task 2: `mark_url_dead_and_maybe_drop` helper

**Files:**
- Create: `src/media/availability.py`
- Create: `tests/test_availability.py`

- [ ] **Step 1: Write the failing tests in `tests/test_availability.py`**

Create the file with this content:

```python
import json

import aiosqlite
import pytest

from src.media.availability import mark_url_dead_and_maybe_drop


async def _insert_feed(db: aiosqlite.Connection, feed_id: str = "f1") -> None:
    await db.execute(
        "INSERT INTO feeds (id, url, title) VALUES (?, ?, ?)",
        (feed_id, f"http://{feed_id}.com", feed_id),
    )
    await db.commit()


async def _insert_item(
    db: aiosqlite.Connection,
    item_id: str,
    feed_id: str,
    guid: str,
    media_url: str,
    media_json: str | None = None,
) -> None:
    if media_json is None:
        await db.execute(
            """INSERT INTO items (id, feed_id, guid, title, media_url, media_type)
               VALUES (?, ?, ?, ?, ?, 'image')""",
            (item_id, feed_id, guid, "t", media_url),
        )
    else:
        await db.execute(
            """INSERT INTO items (id, feed_id, guid, title, media_url, media_type, media_json)
               VALUES (?, ?, ?, ?, ?, 'image', ?)""",
            (item_id, feed_id, guid, "t", media_url, media_json),
        )
    await db.commit()


async def test_single_media_url_404_drops_item(db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "i1", "f1", "g1", "http://x.com/a.jpg")
    dropped = await mark_url_dead_and_maybe_drop(
        "http://x.com/a.jpg", item_id="i1", db=db
    )
    assert dropped == ["i1"]
    async with db.execute("SELECT id FROM items") as cur:
        rows = await cur.fetchall()
    assert rows == []
    async with db.execute(
        "SELECT guid FROM unavailable_guids WHERE feed_id = ?", ("f1",)
    ) as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["g1"]
    async with db.execute("SELECT url FROM dead_urls") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["http://x.com/a.jpg"]


async def test_gallery_partial_404_keeps_item(db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    media_json = json.dumps(
        [
            {"url": "http://x.com/a.jpg", "type": "image"},
            {"url": "http://x.com/b.jpg", "type": "image"},
            {"url": "http://x.com/c.jpg", "type": "image"},
        ]
    )
    await _insert_item(
        db, "i1", "f1", "g1", "http://x.com/a.jpg", media_json=media_json
    )
    dropped = await mark_url_dead_and_maybe_drop(
        "http://x.com/a.jpg", item_id="i1", db=db
    )
    assert dropped == []
    async with db.execute("SELECT id FROM items") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["i1"]
    async with db.execute("SELECT COUNT(*) FROM unavailable_guids") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_gallery_all_404_drops_item(db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    media_json = json.dumps(
        [
            {"url": "http://x.com/a.jpg", "type": "image"},
            {"url": "http://x.com/b.jpg", "type": "image"},
        ]
    )
    await _insert_item(
        db, "i1", "f1", "g1", "http://x.com/a.jpg", media_json=media_json
    )
    await mark_url_dead_and_maybe_drop("http://x.com/a.jpg", item_id="i1", db=db)
    dropped = await mark_url_dead_and_maybe_drop(
        "http://x.com/b.jpg", item_id="i1", db=db
    )
    assert dropped == ["i1"]
    async with db.execute("SELECT id FROM items") as cur:
        rows = await cur.fetchall()
    assert rows == []
    async with db.execute(
        "SELECT guid FROM unavailable_guids WHERE feed_id = ?", ("f1",)
    ) as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["g1"]


async def test_url_shared_by_two_items_drops_both(db: aiosqlite.Connection) -> None:
    await _insert_feed(db, "f1")
    await _insert_feed(db, "f2")
    await _insert_item(db, "i1", "f1", "g1", "http://x.com/shared.jpg")
    await _insert_item(db, "i2", "f2", "g2", "http://x.com/shared.jpg")
    dropped = await mark_url_dead_and_maybe_drop(
        "http://x.com/shared.jpg", item_id="i1", db=db
    )
    assert sorted(dropped) == ["i1", "i2"]
    async with db.execute("SELECT id FROM items ORDER BY id") as cur:
        rows = await cur.fetchall()
    assert rows == []
    async with db.execute(
        "SELECT feed_id, guid FROM unavailable_guids ORDER BY feed_id"
    ) as cur:
        rows = await cur.fetchall()
    assert [tuple(r) for r in rows] == [("f1", "g1"), ("f2", "g2")]


async def test_no_item_id_drops_via_media_url_lookup(db: aiosqlite.Connection) -> None:
    """Fallback path: callers without item_id scan by media_url."""
    await _insert_feed(db)
    await _insert_item(db, "i1", "f1", "g1", "http://x.com/a.jpg")
    dropped = await mark_url_dead_and_maybe_drop(
        "http://x.com/a.jpg", item_id=None, db=db
    )
    assert dropped == ["i1"]
    async with db.execute("SELECT id FROM items") as cur:
        assert await cur.fetchone() is None


async def test_repeated_calls_are_idempotent(db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "i1", "f1", "g1", "http://x.com/a.jpg")
    first = await mark_url_dead_and_maybe_drop(
        "http://x.com/a.jpg", item_id="i1", db=db
    )
    second = await mark_url_dead_and_maybe_drop(
        "http://x.com/a.jpg", item_id="i1", db=db
    )
    assert first == ["i1"]
    assert second == []
    async with db.execute("SELECT url FROM dead_urls") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["http://x.com/a.jpg"]


async def test_unknown_item_id_marks_dead_only(db: aiosqlite.Connection) -> None:
    """If item_id doesn't exist, mark the URL dead but don't crash."""
    await _insert_feed(db)
    dropped = await mark_url_dead_and_maybe_drop(
        "http://x.com/a.jpg", item_id="nonexistent", db=db
    )
    assert dropped == []
    async with db.execute("SELECT url FROM dead_urls") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["http://x.com/a.jpg"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/oton/projects/media-rss-reader && uv run pytest tests/test_availability.py -v`
Expected: ImportError for `src.media.availability` — all 7 tests fail at collection.

- [ ] **Step 3: Create `src/media/availability.py`**

```python
"""Track media URLs that have returned 404 and drop posts whose media is gone.

mark_url_dead_and_maybe_drop(url, item_id, db) is called from the proxy and
the prefetch warmer on every upstream non-success. It records the URL in
dead_urls, then for each item that contains it, deletes the item row and
writes a tombstone to unavailable_guids when every URL of that item is now
dead. Tombstones are read by _refresh_feed to skip re-insert on the next
feed poll.
"""

from __future__ import annotations

import json
import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def _candidate_items(
    db: aiosqlite.Connection, url: str, item_id: str | None
) -> list[aiosqlite.Row]:
    """Return item rows that may contain `url`.

    If item_id is given, fetch that single row. Otherwise scan by media_url
    — the non-primary slide URLs in media_json are intentionally not searched
    here, because real callers (proxy + prefetch) always pass item_id when
    they observed a non-primary slide 404.
    """
    if item_id is not None:
        async with db.execute(
            "SELECT id, feed_id, guid, media_url, media_json "
            "FROM items WHERE id = ?",
            (item_id,),
        ) as cur:
            return list(await cur.fetchall())
    async with db.execute(
        "SELECT id, feed_id, guid, media_url, media_json "
        "FROM items WHERE media_url = ?",
        (url,),
    ) as cur:
        return list(await cur.fetchall())


def _item_urls(row: aiosqlite.Row) -> list[str]:
    """Return the full media URL list for an item row (primary + gallery)."""
    raw = row["media_json"]
    if raw:
        return [slide["url"] for slide in json.loads(raw)]
    return [row["media_url"]]


async def _all_dead(db: aiosqlite.Connection, urls: list[str]) -> bool:
    """True if every URL in `urls` is recorded in dead_urls."""
    if not urls:
        return False
    placeholders = ",".join("?" * len(urls))
    async with db.execute(
        f"SELECT url FROM dead_urls WHERE url IN ({placeholders})", urls
    ) as cur:
        dead = {row["url"] for row in await cur.fetchall()}
    return dead.issuperset(urls)


async def mark_url_dead_and_maybe_drop(
    url: str, item_id: str | None, db: aiosqlite.Connection
) -> list[str]:
    """Record `url` as dead. For every item that contains it, if every URL
    of that item is now dead, DELETE the row and tombstone it. Returns the
    IDs of items dropped by this call."""
    await db.execute(
        "INSERT OR IGNORE INTO dead_urls (url) VALUES (?)", (url,)
    )

    candidates = await _candidate_items(db, url, item_id)
    if not candidates:
        await db.commit()
        return []

    dropped: list[str] = []
    for row in candidates:
        urls = _item_urls(row)
        if not await _all_dead(db, urls):
            continue
        await db.execute("DELETE FROM items WHERE id = ?", (row["id"],))
        await db.execute(
            "INSERT OR IGNORE INTO unavailable_guids (feed_id, guid, marked_at) "
            "VALUES (?, ?, datetime('now'))",
            (row["feed_id"], row["guid"]),
        )
        dropped.append(row["id"])
        logger.debug(
            "dropped item %s (feed=%s guid=%s): all %d media URL(s) dead",
            row["id"],
            row["feed_id"],
            row["guid"],
            len(urls),
        )

    await db.commit()
    return dropped
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/oton/projects/media-rss-reader && uv run pytest tests/test_availability.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/media/availability.py tests/test_availability.py
git commit -m "feat(availability): track dead URLs and drop fully-dead items"
```

---

## Task 3: Wire helper into the proxy

**Files:**
- Modify: `src/api/media.py` (proxy gains `item_id` query param + helper call)

- [ ] **Step 1: Write the failing test in `tests/test_api.py`**

Append to `tests/test_api.py` (after the existing `test_proxy_upstream_error`, which ends around line 296):

```python
async def test_proxy_404_marks_item_unavailable(
    client: AsyncClient,
    tmp_path: object,
    monkeypatch: object,
    db: aiosqlite.Connection,
) -> None:
    """When the upstream returns 404, the proxy must mark the item's URL
    dead so the post can be dropped once every URL of its gallery is dead."""
    import httpx
    import respx

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    feed_id = "f1"
    item_id = "i1"
    url = "http://example.com/broken.jpg"
    await db.execute(
        "INSERT INTO feeds (id, url, title) VALUES (?, ?, ?)",
        (feed_id, "http://feed.example.com", "F"),
    )
    await db.execute(
        """INSERT INTO items (id, feed_id, guid, title, media_url, media_type)
           VALUES (?, ?, ?, ?, ?, 'image')""",
        (item_id, feed_id, "g1", "T", url),
    )
    await db.commit()

    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
        resp = await client.get(f"/api/media/proxy?url={url}&item_id={item_id}")
        await real_client.aclose()

    assert resp.status_code == 502
    async with db.execute("SELECT url FROM dead_urls") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == [url]
    # Single-media post: all (1) URLs are dead → item should be gone.
    async with db.execute("SELECT id FROM items WHERE id = ?", (item_id,)) as cur:
        assert await cur.fetchone() is None
    async with db.execute(
        "SELECT guid FROM unavailable_guids WHERE feed_id = ?", (feed_id,)
    ) as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["g1"]


async def test_proxy_404_without_item_id_still_returns_502(
    client: AsyncClient,
    tmp_path: object,
    monkeypatch: object,
    db: aiosqlite.Connection,
) -> None:
    """Backwards compat: item_id is optional, missing item_id must not
    break the 502 contract — the URL still gets marked dead."""
    import httpx
    import respx

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/broken.jpg"

    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
        resp = await client.get(f"/api/media/proxy?url={url}")
        await real_client.aclose()

    assert resp.status_code == 502
    async with db.execute("SELECT url FROM dead_urls") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == [url]
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `cd /home/oton/projects/media-rss-reader && uv run pytest tests/test_api.py::test_proxy_404_marks_item_unavailable tests/test_api.py::test_proxy_404_without_item_id_still_returns_502 -v`
Expected: FAIL — proxy currently never marks the URL dead (the helper isn't called yet, and item_id isn't accepted).

- [ ] **Step 3: Modify `src/api/media.py`**

In `src/api/media.py`, replace the entire `proxy_media` function (lines 23-53) with:

```python
@router.get("/media/proxy", response_model=None)
async def proxy_media(
    url: str = Query(...),
    item_id: str | None = Query(None),
    db: _DbDep = None,  # type: ignore[assignment]
) -> FileResponse:
    """Cache-through proxy for media files.

    On a cache hit: serve the file directly via FileResponse (zero-copy sendfile).
    On a cache miss: stream from upstream to the cache file (no in-memory buffer),
    then serve the cached file. This keeps memory usage O(chunk_size) regardless
    of the media file size.

    On upstream non-success, mark `url` as dead and (if every URL of `item_id`
    is now dead) drop the item from the DB. Errors from the helper are logged
    and swallowed — they must not mask the 502 the client deserves.
    """
    path = cache_read(url)
    if path is not None:
        # Cached file is named by sha256(url) with no extension, so FileResponse
        # would otherwise infer application/octet-stream and the browser would
        # refuse to e.g. animate a cached GIF. The sidecar written at cache
        # time holds the upstream Content-Type.
        media_type = cache_read_meta(url)
        return FileResponse(str(path), media_type=media_type)

    client = get_http_client()
    content_type = "application/octet-stream"
    try:
        async with client.stream("GET", url, follow_redirects=True, timeout=30) as response:
            if not response.is_success:
                # Stream body fully before exiting the context manager so the
                # connection isn't left dangling. Body is discarded.
                await response.aread()
                await _mark_dead(url, item_id, db)
                raise HTTPException(status_code=502, detail="upstream error")
            content_type = response.headers.get("content-type", "application/octet-stream")
            path = await cache_stream_write(url, response.aiter_bytes(65536), content_type)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="upstream fetch failed") from exc

    return FileResponse(str(path), media_type=content_type)


async def _mark_dead(url: str, item_id: str | None, db: aiosqlite.Connection | None) -> None:
    """Best-effort: mark url as dead via the availability helper.

    db is injected via the FastAPI dependency and may be None if the proxy is
    called from a context where the dependency didn't fire (defensive — should
    not happen in production). Failures are logged and swallowed.
    """
    if db is None:
        return
    try:
        from src.media.availability import mark_url_dead_and_maybe_drop

        await mark_url_dead_and_maybe_drop(url, item_id, db)
    except Exception as exc:  # pragma: no cover
        logger.debug("mark_url_dead_and_maybe_drop failed for %s: %s", url, exc)
```

Add this import near the top of the file (alongside the other `src.*` imports at lines 12-16):

```python
import logging
```

Then add at module level (after the imports, before `router = APIRouter()` at line 18):

```python
logger = logging.getLogger(__name__)
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `cd /home/oton/projects/media-rss-reader && uv run pytest tests/test_api.py::test_proxy_404_marks_item_unavailable tests/test_api.py::test_proxy_404_without_item_id_still_returns_502 -v`
Expected: PASS (both).

- [ ] **Step 5: Run the full proxy test set to verify no regression**

Run: `cd /home/oton/projects/media-rss-reader && uv run pytest tests/test_api.py -v -k proxy`
Expected: all proxy tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/api/media.py tests/test_api.py
git commit -m "feat(proxy): mark media URL dead on upstream non-success"
```

---

## Task 4: Wire helper into the prefetch warmer

**Files:**
- Modify: `src/media/prefetch.py` (`_warm` signature + helper call)

- [ ] **Step 1: Replace the `_warm` function in `src/media/prefetch.py`**

In `src/media/prefetch.py`, replace `_warm` (lines 26-36) with:

```python
async def _warm(item_id: str, url: str, client: httpx.AsyncClient, db: aiosqlite.Connection) -> None:
    """Fetch and cache one URL if it is not already cached. On upstream
    non-success, mark the URL dead via the availability helper so a fully-dead
    post can be dropped. Silent on errors."""
    if cache_read(url) is not None:
        return  # already cached — nothing to do
    try:
        async with client.stream("GET", url, follow_redirects=True, timeout=30) as response:
            if response.is_success:
                content_type = response.headers.get("content-type", "application/octet-stream")
                await cache_stream_write(url, response.aiter_bytes(65536), content_type)
            else:
                await response.aread()
                from src.media.availability import mark_url_dead_and_maybe_drop

                try:
                    await mark_url_dead_and_maybe_drop(url, item_id, db)
                except Exception as exc:  # pragma: no cover
                    logger.debug("mark_url_dead_and_maybe_drop failed for %s: %s", url, exc)
    except Exception as exc:  # pragma: no cover
        logger.debug("prefetch failed for %s: %s", url, exc)
```

- [ ] **Step 2: Update `warm_startup_cache` to pass `item_id` and `db`**

In the same file, replace `warm_startup_cache` (lines 39-65) with:

```python
async def warm_startup_cache(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Pre-warm the cache with the most recently published items.

    Runs as an asyncio background task (fire-and-forget from the lifespan hook).
    A semaphore of 10 and a 100 ms stagger between task creation prevents a
    thundering-herd of concurrent HTTP requests at container start.
    """
    try:
        async with db.execute(
            "SELECT id, media_url FROM items ORDER BY pub_date DESC LIMIT ?",
            (settings.cache_max_items,),
        ) as cur:
            rows = await cur.fetchall()
    except Exception as exc:
        logger.warning("warm_startup_cache: DB query failed, skipping cache warm: %s", exc)
        return

    sem = asyncio.Semaphore(10)

    async def _bounded_warm(item_id: str, url: str) -> None:
        async with sem:
            await _warm(item_id, url, client, db)

    for row in rows:
        asyncio.create_task(_bounded_warm(row["id"], row["media_url"]))
        # Small sleep between task creation to spread the initial burst.
        await asyncio.sleep(0.1)
```

- [ ] **Step 3: Update `prefetch_ahead` to pass `item_id` and `db`**

In the same file, replace `prefetch_ahead` (lines 68-84) with:

```python
async def prefetch_ahead(item_id: str, db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Fire background warm tasks for the next PREFETCH_AHEAD items after item_id.

    Queries items with a pub_date strictly less than the given item's pub_date
    (i.e. items that come *after* it in reverse-chronological display order).
    Each warm task runs independently; errors are silently ignored.
    """
    async with db.execute(
        """SELECT id, media_url FROM items
           WHERE pub_date < (SELECT pub_date FROM items WHERE id = ?)
           ORDER BY pub_date DESC
           LIMIT ?""",
        (item_id, settings.prefetch_ahead),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        asyncio.create_task(_warm(row["id"], row["media_url"], client, db))
```

- [ ] **Step 4: Run the existing prefetch test to verify no regression**

Run: `cd /home/oton/projects/media-rss-reader && uv run pytest tests/test_api.py -v -k prefetch`
Expected: PASS. The signature change to `_warm` only affects internal callers; the public API of `prefetch_ahead` is unchanged. (`prefetch_hint` calls it with `(item_id, db, client)` — same as before.)

- [ ] **Step 5: Commit**

```bash
git add src/media/prefetch.py
git commit -m "feat(prefetch): mark media URL dead on upstream non-success"
```

---

## Task 5: `_refresh_feed` skips tombstoned guids

**Files:**
- Modify: `src/feeds/sync.py:_refresh_feed`
- Modify: `tests/test_sync.py`

- [ ] **Step 1: Add the failing test to `tests/test_sync.py`**

Append to `tests/test_sync.py`:

```python
async def test_refresh_skips_unavailable_guids(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    """Items whose (feed_id, guid) is in unavailable_guids must not be
    re-inserted by a subsequent feed refresh."""
    f = tmp_path / "feeds.opml"
    f.write_text(_OPML)

    # Seed: feed + tombstone for guid g1 (no item row — simulates prior drop).
    feed_id = hashlib.sha256(b"https://example.com/feed.xml").hexdigest()
    await db.execute(
        "INSERT INTO feeds (id, url, title) VALUES (?, ?, ?)",
        (feed_id, "https://example.com/feed.xml", "Feed"),
    )
    await db.execute(
        "INSERT INTO unavailable_guids (feed_id, guid) VALUES (?, ?)",
        (feed_id, "g1"),
    )
    await db.commit()

    with respx.mock:
        respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS))
        async with httpx.AsyncClient() as client:
            await refresh_all_feeds(db, client)

    async with db.execute("SELECT id, guid FROM items") as cur:
        rows = await cur.fetchall()
    # g1 is in the RSS feed but tombstoned → not inserted.
    assert rows == []
    # Tombstone is untouched.
    async with db.execute(
        "SELECT guid FROM unavailable_guids WHERE feed_id = ?", (feed_id,)
    ) as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["g1"]
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `cd /home/oton/projects/media-rss-reader && uv run pytest tests/test_sync.py::test_refresh_skips_unavailable_guids -v`
Expected: FAIL — `_refresh_feed` currently inserts g1 (the item ends up in the table because the tombstone is ignored).

- [ ] **Step 3: Modify `_refresh_feed` in `src/feeds/sync.py`**

In `src/feeds/sync.py`, insert a tombstone-load step **and** a guard in the insert loop. Two surgical edits:

**Edit A** — add the dead_guids load immediately before the existing `for item in items:` loop (currently at line 68 of `src/feeds/sync.py`):

```python
    items = await fetch_feed(url, client)
    async with db.execute(
        "SELECT guid FROM unavailable_guids WHERE feed_id = ?", (feed_id,)
    ) as cur:
        dead_guids = {row["guid"] for row in await cur.fetchall()}
    inserted = 0
    for item in items:
        if item["guid"] in dead_guids:
            logger.debug(
                "Skipping tombstoned item guid=%s in feed %s", item["guid"], url
            )
            continue
        logger.debug(f"Storing item {item['title']} with media URL {item['media_url']} and ID {item['id']}")
```

**Edit B** — change the trailing log line at line 78 of `src/feeds/sync.py`:

```python
    if items:
        logger.debug(f"Feed {url}: {inserted} new, {len(items) - inserted} already in DB")
```

to:

```python
    if items:
        logger.debug(f"Feed {url}: {inserted} new, {len(items) - inserted} already in DB or tombstoned")
```

The rest of `_refresh_feed` (seen-at restore, last_fetched_at update, commit) is unchanged.

Update the function's docstring (currently at line 61) to mention the tombstone skip:

```python
    """Fetch new items for one feed and write them to the database.

    INSERT OR IGNORE on (feed_id, guid) silently skips items that are
    already in the database, so this function is safe to call repeatedly.
    Items whose (feed_id, guid) is in unavailable_guids are skipped before
    the INSERT so a previously-dropped dead post is never re-added.
    """
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `cd /home/oton/projects/media-rss-reader && uv run pytest tests/test_sync.py::test_refresh_skips_unavailable_guids -v`
Expected: PASS.

- [ ] **Step 5: Run the full sync test set to verify no regression**

Run: `cd /home/oton/projects/media-rss-reader && uv run pytest tests/test_sync.py -v`
Expected: all sync tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/feeds/sync.py tests/test_sync.py
git commit -m "feat(sync): skip tombstoned guids on feed refresh"
```

---

## Task 6: Frontend passes `item_id` to the proxy

**Files:**
- Modify: `src/static/cache-queue.js` (line 82)
- Modify: `src/static/feed-view.js` (line 120)

- [ ] **Step 1: Update `src/static/cache-queue.js`**

In `src/static/cache-queue.js`, replace the line 82 assignment (currently):

```javascript
el.src = `/api/media/proxy?url=${encodeURIComponent(item.media_url)}`;
```

with:

```javascript
el.src = `/api/media/proxy?url=${encodeURIComponent(item.media_url)}&item_id=${encodeURIComponent(item.id)}`;
```

- [ ] **Step 2: Update `src/static/feed-view.js`**

In `src/static/feed-view.js`, replace the line 120 assignment (currently):

```javascript
el.src = `/api/media/proxy?url=${encodeURIComponent(m.url)}`;
```

with:

```javascript
el.src = `/api/media/proxy?url=${encodeURIComponent(m.url)}&item_id=${encodeURIComponent(item.id)}`;
```

`item` is the function parameter already in scope at that point (the outer `createMediaWrap(item, …)`); verify this by reading the surrounding lines 70-100 before editing if unsure.

- [ ] **Step 3: Smoke test the frontend in the browser**

Run: `cd /home/oton/projects/media-rss-reader && uv run uvicorn src.main:app --reload --port 8080`

Open http://127.0.0.1:8080, scroll the feed. In DevTools → Network, confirm that every `/api/media/proxy?…` request has an `item_id=` parameter alongside the `url=`.

- [ ] **Step 4: Commit**

```bash
git add src/static/cache-queue.js src/static/feed-view.js
git commit -m "feat(webui): pass item_id to media proxy for dead-URL tracking"
```

---

## Task 7: Final verification

**Files:** none

- [ ] **Step 1: Run lint**

Run: `cd /home/oton/projects/media-rss-reader && uv run ruff check .`
Expected: no findings.

- [ ] **Step 2: Run the full test suite**

Run: `cd /home/oton/projects/media-rss-reader && uv run pytest`
Expected: all tests pass; coverage ≥ 90 % (enforced by `pyproject.toml`'s `addopts`).

- [ ] **Step 3: Done**

No commit — this task is verification only.

---

## Self-Review

- **Spec coverage:**
  - Schema additions (schema.py + migrations.py) → Task 1 ✓
  - `mark_url_dead_and_maybe_drop` helper → Task 2 ✓
  - Proxy integration → Task 3 ✓
  - Prefetch integration → Task 4 ✓
  - `_refresh_feed` skip tombstones → Task 5 ✓
  - Frontend `item_id` in proxy URL → Task 6 ✓
  - Tests for helper, proxy, sync → Tasks 2, 3, 5 ✓
- **No placeholders.** Every step contains the actual code. ✓
- **Type / name consistency:** `mark_url_dead_and_maybe_drop(url, item_id, db)` defined once in Task 2, used with identical signature in Tasks 3 and 4. `_warm(item_id, url, client, db)` defined once in Task 4, used by both `warm_startup_cache` and `prefetch_ahead` in Task 4. `unavailable_guids(feed_id, guid)` schema is consistent across Tasks 1, 2, and 5. ✓
- **Backwards compatibility:** `item_id` is optional in the proxy (Task 3 has the test `test_proxy_404_without_item_id_still_returns_502`); `_warm` callers in `media.py` and the lifespan hook still use the same public signatures. ✓
