import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncGenerator
from pathlib import Path

import aiosqlite
import httpx
import pytest
import respx
from httpx import AsyncClient


def _pinned(url: str) -> str:
    """The url as open_upstream now sends it: host replaced by the stubbed IP."""
    from src.media import fetch as fetch_mod

    return fetch_mod._pinned_url(url, "93.184.216.34")


async def test_feeds_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/feeds")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_feeds_returns_feed_with_counts(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await db.execute("INSERT INTO feeds (id, url, title) VALUES ('f1', 'http://x.com', 'X')")
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type)"
        " VALUES ('i1', 'f1', 'g1', 'http://img.jpg', 'image')"
    )
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type, seen_at)"
        " VALUES ('i2', 'f1', 'g2', 'http://img2.jpg', 'image', datetime('now'))"
    )
    await db.commit()
    resp = await client.get("/api/feeds")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "f1"
    assert data[0]["item_count"] == 2
    assert data[0]["unseen_count"] == 1


async def test_feeds_ordered_by_title(client: AsyncClient, db: aiosqlite.Connection) -> None:
    for fid, title in (("f3", "Zeta"), ("fC", "Alpha"), ("f2", "Mid")):
        await db.execute("INSERT INTO feeds(id, url, title) VALUES (?, ?, ?)", (fid, fid, title))
    await db.commit()
    resp = await client.get("/api/feeds")
    assert [f["title"] for f in resp.json()] == ["Alpha", "Mid", "Zeta"]


async def test_feeds_returns_a_null_title(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """feeds.title is nullable and the codebase inserts such feeds, while
    FeedOut declared title: str — a contract checked by nothing in either
    direction, since response_model=None removes the runtime check and no type
    checker is configured. SQLite sorts NULL first, so the feed also leads the
    list."""
    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f0', 'http://x', NULL)")
    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://y', 'Alpha')")
    await db.commit()
    data = (await client.get("/api/feeds")).json()
    titles = [f["title"] for f in data]
    assert None in titles, "a title-less feed must still be returned"
    assert titles[0] is None


async def test_feeds_ordered_case_insensitively(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """BINARY collation sorts every uppercase title before every lowercase one,
    and titles come from feed metadata or a filename, so mixed case is the
    norm."""
    for fid, title in (("f1", "apple"), ("f2", "Banana"), ("f3", "cherry")):
        await db.execute("INSERT INTO feeds(id, url, title) VALUES (?, ?, ?)", (fid, fid, title))
    await db.commit()
    titles = [f["title"] for f in (await client.get("/api/feeds")).json()]
    assert titles == ["apple", "Banana", "cherry"]


# ---------------------------------------------------------------------------
# Helpers for items tests
# ---------------------------------------------------------------------------


async def _insert_feed(
    db: aiosqlite.Connection, feed_id: str = "feed1", url: str = "http://example.com/feed.xml"
) -> None:
    await db.execute(
        "INSERT INTO feeds(id, url, title) VALUES (?, ?, ?)",
        (feed_id, url, "Test Feed"),
    )
    await db.commit()


async def _insert_item(
    db: aiosqlite.Connection,
    item_id: str,
    feed_id: str,
    seen_at: str | None = None,
) -> None:
    await db.execute(
        """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date, seen_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
        (item_id, feed_id, item_id, "Title", "http://example.com/img.jpg", "image", seen_at),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# GET /api/items tests
# ---------------------------------------------------------------------------


async def test_items_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/items")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_items_returns_items(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1")
    resp = await client.get("/api/items")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    item = data[0]
    assert item["id"] == "item1"
    assert item["feed_id"] == "feed1"
    assert item["title"] == "Title"
    assert item["media_url"] == "http://example.com/img.jpg"
    assert item["media_type"] == "image"
    assert "pub_date" in item
    assert "fetched_at" in item
    assert "seen_at" in item


async def test_items_unseen_filter(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "seen_item", "feed1", seen_at="2024-01-01T00:00:00")
    await _insert_item(db, "unseen_item", "feed1", seen_at=None)
    resp = await client.get("/api/items", params={"unseen": "true"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "unseen_item"


async def test_items_keyset_cursor(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1")
    await _insert_item(db, "item2", "feed1")
    await _insert_item(db, "item3", "feed1")
    # Page 1: no cursor
    resp1 = await client.get("/api/items", params={"size": 2})
    assert resp1.status_code == 200
    page1 = resp1.json()
    assert [i["id"] for i in page1] == ["item1", "item2"]
    # Page 2: cursor from page1's last item
    last = page1[-1]
    resp2 = await client.get(
        "/api/items",
        params={"size": 2, "after_id": last["id"]},
    )
    assert resp2.status_code == 200
    assert [i["id"] for i in resp2.json()] == ["item3"]


async def test_items_cursor_paginates_feed_with_undated_items(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """The blocker: ROW_NUMBER sorts NULL pub_date first and ranks those rows
    1..k, but a row-value comparison with a NULL member evaluates to NULL in
    SQLite, so the old COUNT(*) derivation dropped them and produced rn - k.
    Paginating a feed with undated items lost some and repeated others.
    """
    await _insert_feed(db)
    for n in range(3):
        await db.execute(
            """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
               VALUES (?, 'feed1', ?, 'T', 'http://example.com/img.jpg', 'image', NULL)""",
            (f"n{n}", f"gn{n}"),
        )
    for n in range(5):
        await db.execute(
            """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
               VALUES (?, 'feed1', ?, 'T', 'http://example.com/img.jpg', 'image', ?)""",
            (f"d{n}", f"gd{n}", f"2026-01-0{n + 1}T00:00:00"),
        )
    await db.commit()

    collected: list[str] = []
    params: dict[str, object] = {"size": 2}
    for _ in range(10):
        resp = await client.get("/api/items", params=params)
        assert resp.status_code == 200, resp.text
        page = resp.json()
        if not page:
            break
        collected += [i["id"] for i in page]
        params = {"size": 2, "after_id": page[-1]["id"]}

    assert collected == ["n0", "n1", "n2", "d0", "d1", "d2", "d3", "d4"], (
        f"every item exactly once, in rn order; got {collected}"
    )


async def test_items_cursor_interleaves_two_feeds(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """The feed_id tiebreak in (rn, feed_id, id) is load-bearing: with two
    feeds, rn collides across partitions on every page boundary. Every cursor
    test used to insert into feed1 only, so removing feed_id from the tuple
    kept them all green while breaking interleaved pagination.
    """
    await _insert_feed(db, feed_id="feedA", url="http://example.com/a.xml")
    await _insert_feed(db, feed_id="feedB", url="http://example.com/b.xml")
    for n in range(3):
        for fid in ("feedA", "feedB"):
            await db.execute(
                """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
                   VALUES (?, ?, ?, 'T', 'http://example.com/img.jpg', 'image', ?)""",
                (f"{fid}-{n}", fid, f"g-{fid}-{n}", f"2026-01-0{n + 1}T00:00:00"),
            )
    await db.commit()

    collected: list[str] = []
    params: dict[str, object] = {"size": 2}
    for _ in range(10):
        page = (await client.get("/api/items", params=params)).json()
        if not page:
            break
        collected += [i["id"] for i in page]
        params = {"size": 2, "after_id": page[-1]["id"]}

    assert collected == [
        "feedA-0",
        "feedB-0",
        "feedA-1",
        "feedB-1",
        "feedA-2",
        "feedB-2",
    ], f"interleaved order, no repeats, nothing skipped; got {collected}"


async def test_items_cursor_anchor_gone_is_410(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """A rank of 0 used to admit the entire table (every row has rn >= 1), so
    the client silently received page one of the global interleave, filtered it
    all out against its known set, and re-issued the same request forever.
    """
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1")
    resp = await client.get("/api/items", params={"after_id": "never-existed", "size": 5})
    assert resp.status_code == 410
    assert resp.json()["detail"] == "cursor expired"


async def test_items_cursor_anchor_gone_logs_the_id(
    client: AsyncClient, db: aiosqlite.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """'Scrolling stopped working' has to be diagnosable from a log file."""
    caplog.set_level(logging.INFO, logger="src.api.items")
    await _insert_feed(db)
    await client.get("/api/items", params={"after_id": "ghost"})
    assert any("410" in r.getMessage() and "ghost" in r.getMessage() for r in caplog.records)


async def test_items_keyset_no_skip_after_mark_seen(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """F17 regression: marking items seen must not renumber or skip later items.

    Old code computed rn over the filtered set, so marking item1 seen
    renumbered item2→1, item3→2, item4→3, and the client's offset=2 skipped
    the new position-0 item. Keyset over the full set keeps rn stable.
    """
    await _insert_feed(db)
    for n in range(1, 5):
        await _insert_item(db, f"item{n}", "feed1")

    first = await client.get("/api/items", params={"unseen": "true", "size": 2})
    assert [i["id"] for i in first.json()] == ["item1", "item2"]

    # Mark both seen; the client still holds them, so its cursor is the last
    # item it received (item2). The server must return item3 and item4, not
    # skip item3 the way the old offset=2 did.
    await client.post("/api/items/item1/seen")
    await client.post("/api/items/item2/seen")

    last = first.json()[-1]
    second = await client.get(
        "/api/items",
        params={"unseen": "true", "after_id": last["id"], "size": 2},
    )
    assert [i["id"] for i in second.json()] == ["item3", "item4"]


async def test_items_returns_media_array_from_media_json(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    media_json = (
        '[{"url": "http://example.com/a.jpg", "type": "image"}, {"url": "http://example.com/b.gif", "type": "gif"}]'
    )
    await db.execute(
        """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, media_json, pub_date)
           VALUES ('g1', 'feed1', 'g1', 'Gallery', 'http://example.com/a.jpg', 'image', ?, datetime('now'))""",
        (media_json,),
    )
    await db.commit()
    resp = await client.get("/api/items")
    assert resp.status_code == 200
    (item,) = resp.json()
    assert item["media"] == [
        {"url": "http://example.com/a.jpg", "type": "image"},
        {"url": "http://example.com/b.gif", "type": "gif"},
    ]
    assert "media_json" not in item


async def test_items_without_media_json_falls_back(client: AsyncClient, db: aiosqlite.Connection) -> None:
    # Rows predating migration v5 have media_json NULL; the API must still
    # return a 1-element media array built from media_url/media_type.
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1")
    resp = await client.get("/api/items")
    (item,) = resp.json()
    assert item["media"] == [{"url": "http://example.com/img.jpg", "type": "image"}]


# ---------------------------------------------------------------------------
# POST /api/items/{id}/seen tests
# ---------------------------------------------------------------------------


async def test_mark_seen(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1", seen_at=None)
    resp = await client.post("/api/items/item1/seen")
    assert resp.status_code == 200
    data = resp.json()
    assert "seen_at" in data
    assert data["seen_at"] is not None
    # Verify DB was updated
    async with db.execute("SELECT seen_at FROM items WHERE id = 'item1'") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] is not None


async def test_mark_seen_items_and_seen_media_share_timestamp(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F11: items.seen_at and seen_media.seen_at are bound to one `now`, so
    they cannot diverge. The timestamp has one-second resolution, so two real
    now() calls produce the same string and the assertion could not fail —
    patch the clock so every call returns a distinct second.
    """
    import datetime as real_dt

    import src.api.items as items_mod

    ticks = iter([real_dt.datetime(2026, 1, 1, 0, 0, s, tzinfo=real_dt.UTC) for s in range(1, 10)])

    class _TickingClock(real_dt.datetime):
        @classmethod
        def now(cls, tz: object = None) -> real_dt.datetime:
            return next(ticks)

    monkeypatch.setattr(items_mod.dt, "datetime", _TickingClock)

    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1", seen_at=None)
    resp = await client.post("/api/items/item1/seen")
    assert resp.status_code == 200
    async with db.execute("SELECT seen_at FROM items WHERE id = 'item1'") as cur:
        items_seen = (await cur.fetchone())[0]
    async with db.execute("SELECT seen_at FROM seen_media WHERE media_key = 'http://example.com/img.jpg'") as cur:
        media_seen = (await cur.fetchone())[0]
    assert items_seen == media_seen, f"one now() must be bound to both writes; got {items_seen} vs {media_seen}"


async def test_mark_seen_writes_seen_media(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """seen_media is the durable record — it must outlive the items row."""
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1", seen_at=None)
    assert (await client.post("/api/items/item1/seen")).status_code == 200

    async with db.execute("SELECT media_key, seen_at FROM seen_media") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["media_key"] == "http://example.com/img.jpg"
    assert rows[0]["seen_at"] is not None

    # Deleting the item (as prune_items does) must not take the record with it.
    await db.execute("DELETE FROM items WHERE id = 'item1'")
    await db.commit()
    async with db.execute("SELECT COUNT(*) FROM seen_media") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_mark_seen_rolls_back_when_seen_media_write_fails(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the INSERT OR REPLACE INTO seen_media raises, the UPDATE must not
    be left committed — otherwise items.seen_at is set with no durable seen
    record.
    """
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1", seen_at=None)
    orig_execute = db.execute
    calls = {"n": 0}

    def _flaky_execute(query: str, params: tuple[object, ...] = ()) -> object:
        calls["n"] += 1
        if "INSERT OR REPLACE INTO seen_media" in query:
            raise aiosqlite.OperationalError("simulated disk full")
        return orig_execute(query, params)

    monkeypatch.setattr(db, "execute", _flaky_execute)
    monkeypatch.setattr(client._transport, "raise_app_exceptions", False)
    resp = await client.post("/api/items/item1/seen")
    monkeypatch.undo()
    assert resp.status_code == 500
    async with db.execute("SELECT seen_at FROM items WHERE id = 'item1'") as cur:
        row = await cur.fetchone()
    assert row[0] is None, "items.seen_at must be rolled back when seen_media write fails"


async def test_mark_seen_not_found(client: AsyncClient) -> None:
    resp = await client.post("/api/items/nonexistent/seen")
    assert resp.status_code == 404


async def test_mark_seen_is_idempotent(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """The browser fires this as a discarded beacon, so a duplicate POST is
    routine. The UPDATE is unconditional, so the second call moves seen_at
    forward and rewrites the seen_media row; neither the 200-on-repeat contract
    nor the timestamp drift was pinned anywhere."""
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1", seen_at=None)
    first = await client.post("/api/items/item1/seen")
    second = await client.post("/api/items/item1/seen")
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["seen_at"] >= first.json()["seen_at"]
    async with db.execute(
        "SELECT COUNT(*) FROM seen_media WHERE media_key = ?",
        ("http://example.com/img.jpg",),
    ) as cur:
        assert (await cur.fetchone())[0] == 1, "INSERT OR REPLACE, not a second row"


# ---------------------------------------------------------------------------
# GET /api/media/proxy tests
# ---------------------------------------------------------------------------


async def _register_proxy_url(db: aiosqlite.Connection, url: str) -> None:
    """Register `url` as an item's media_url so the proxy gate accepts it."""
    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('fproxy', 'http://x', 'X')")
    await db.execute(
        "INSERT INTO items(id, feed_id, guid, media_url, media_type) VALUES ('iproxy', 'fproxy', 'g', ?, 'image')",
        (url,),
    )
    await db.commit()


async def test_proxy_cache_hit(
    client: AsyncClient, tmp_path: object, monkeypatch: object, db: aiosqlite.Connection
) -> None:
    import hashlib

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/img.jpg"
    await _register_proxy_url(db, url)
    filename = hashlib.sha256(url.encode()).hexdigest()
    (tmp_path / filename).write_bytes(b"cached")  # type: ignore[operator]

    resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 200
    assert resp.content == b"cached"


async def test_proxy_cache_hit_returns_correct_content_type(
    client: AsyncClient, tmp_path: object, monkeypatch: object, db: aiosqlite.Connection
) -> None:
    """Cache hit must serve the stored Content-Type, not octet-stream.

    The cached file is named by sha256(url) with no extension, so
    FileResponse's extension-inference would otherwise return
    application/octet-stream. That breaks the browser's ability to
    animate a cached GIF.
    """
    import hashlib

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/anim.gif"
    await _register_proxy_url(db, url)
    filename = hashlib.sha256(url.encode()).hexdigest()
    (tmp_path / filename).write_bytes(b"GIF89a")  # type: ignore[operator]
    (tmp_path / f"{filename}.meta").write_text("image/gif")  # type: ignore[operator]

    resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/gif")


async def test_proxy_cache_hit_falls_back_when_sidecar_missing(
    client: AsyncClient, tmp_path: object, monkeypatch: object, db: aiosqlite.Connection
) -> None:
    """Pre-sidecar cached files (no .meta sibling) must still be servable."""
    import hashlib

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/anim.gif"
    await _register_proxy_url(db, url)
    filename = hashlib.sha256(url.encode()).hexdigest()
    (tmp_path / filename).write_bytes(b"GIF89a")  # type: ignore[operator]
    # no .meta written — simulates a cache file from before sidecars existed

    resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 200
    assert resp.content == b"GIF89a"
    # No sidecar → must NOT be served as text/plain (Starlette's guess on a
    # bare-sha256 filename). octet-stream lets the browser sniff and render.
    assert resp.headers["content-type"].startswith("application/octet-stream")


async def test_proxy_cache_miss(
    client: AsyncClient, tmp_path: object, monkeypatch: object, db: aiosqlite.Connection
) -> None:
    import httpx
    import respx

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/photo.jpg"
    await _register_proxy_url(db, url)

    with respx.mock:
        respx.get(_pinned(url)).mock(
            return_value=httpx.Response(200, content=b"freshdata", headers={"content-type": "image/jpeg"})
        )
        resp = await client.get(f"/api/media/proxy?url={url}")

    assert resp.status_code == 200
    assert resp.content == b"freshdata"
    # Sidecar must have been written so a subsequent cache hit serves the
    # correct Content-Type.
    import hashlib

    fname = hashlib.sha256(url.encode()).hexdigest()
    assert (tmp_path / f"{fname}.meta").read_text() == "image/jpeg"  # type: ignore[operator]


async def test_proxy_still_refuses_an_unknown_url_after_the_reorder(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate moves behind the cache lookup because a hit cannot escape
    CACHE_DIR (the key is sha256(url)) and can only exist because the URL
    passed the gate earlier. This test is what stops a future reorder from
    dropping the gate entirely."""
    from src.config import settings

    monkeypatch.setattr(settings, "cache_dir", str(tmp_path))
    resp = await client.get("/api/media/proxy", params={"url": "http://evil.example/x.jpg"})
    assert resp.status_code == 404


async def test_proxy_rejects_html_upstream(
    client: AsyncClient,
    tmp_path: object,
    monkeypatch: object,
    db: aiosqlite.Connection,
) -> None:
    """An upstream serving text/html for a media URL is same-origin content
    injection (F5). Reject it as 502 instead of forwarding the content-type."""
    import httpx
    import respx

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/sneaky.jpg"
    await _register_proxy_url(db, url)
    with respx.mock:
        respx.get(_pinned(url)).mock(
            return_value=httpx.Response(200, content=b"<html/>", headers={"content-type": "text/html"})
        )
        resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 502


async def test_proxy_image_passes_with_nosniff(
    client: AsyncClient,
    tmp_path: object,
    monkeypatch: object,
    db: aiosqlite.Connection,
) -> None:
    import httpx
    import respx

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/real.jpg"
    await _register_proxy_url(db, url)
    with respx.mock:
        respx.get(_pinned(url)).mock(
            return_value=httpx.Response(200, content=b"jpgdata", headers={"content-type": "image/jpeg"})
        )
        resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")


async def test_proxy_octet_stream_upstream_passes(
    client: AsyncClient,
    tmp_path: object,
    monkeypatch: object,
    db: aiosqlite.Connection,
) -> None:
    """CDNs that don't declare a media type must not be rejected (F5)."""
    import httpx
    import respx

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/unknown.jpg"
    await _register_proxy_url(db, url)
    with respx.mock:
        respx.get(_pinned(url)).mock(
            return_value=httpx.Response(200, content=b"jpgdata", headers={"content-type": "application/octet-stream"})
        )
        resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/octet-stream")


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
        respx.get(_pinned(url)).mock(return_value=httpx.Response(404))
        resp = await client.get(f"/api/media/proxy?url={url}&item_id={item_id}")

    assert resp.status_code == 502
    async with db.execute("SELECT url FROM dead_urls") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == [url]
    # Single-media post: all (1) URLs are dead -> item should be gone.
    async with db.execute("SELECT id FROM items WHERE id = ?", (item_id,)) as cur:
        assert await cur.fetchone() is None
    async with db.execute("SELECT guid FROM unavailable_guids WHERE feed_id = ?", (feed_id,)) as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["g1"]


async def test_proxy_404_without_item_id_still_returns_502(
    client: AsyncClient,
    tmp_path: object,
    monkeypatch: object,
    db: aiosqlite.Connection,
) -> None:
    """Backwards compat: item_id is optional, missing item_id must not
    break the 502 contract -- the URL still gets marked dead."""
    import httpx
    import respx

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/broken.jpg"
    await _register_proxy_url(db, url)

    with respx.mock:
        respx.get(_pinned(url)).mock(return_value=httpx.Response(404))
        resp = await client.get(f"/api/media/proxy?url={url}")

    assert resp.status_code == 502
    async with db.execute("SELECT url FROM dead_urls") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == [url]


# ---------------------------------------------------------------------------
# POST /api/prefetch/hint tests
# ---------------------------------------------------------------------------


async def test_prefetch_hint(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/feed', 'F')")
    await db.execute(
        "INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date) "
        "VALUES ('i1', 'f1', 'g1', 'T', 'http://x.com/img.jpg', 'image', datetime('now'))"
    )
    await db.commit()

    resp = await client.post("/api/prefetch/hint", json={"item_id": "i1"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_prefetch_hint_rejects_absent_or_empty_item_id(client: AsyncClient) -> None:
    """The hand-rolled `if not item_id` guard answered
    {"detail": "item_id required"} while pydantic's 422 for the same endpoint
    gives detail as a list of error objects, so a client could not parse both
    with one code path. min_length=1 makes it one shape — and those two lines
    were the only unexecuted lines in the package."""
    for body in ({}, {"item_id": ""}):
        resp = await client.post("/api/prefetch/hint", json=body)
        assert resp.status_code == 422, body
        assert isinstance(resp.json()["detail"], list), body


async def test_prefetch_hint_unknown_item_404(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """F16: a typo'd item_id must be 404, not indistinguishable from ok."""
    resp = await client.post("/api/prefetch/hint", json={"item_id": "nonexistent"})
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        # "false" is deliberately absent: unseen is a plain bool (minor 15), so
        # /api/items' coercion applies here too and this string is valid input.
        {"item_id": "x", "unseen": None},
        {"item_id": 123},
        {"unseen": True},
    ],
)
async def test_prefetch_hint_rejects_bad_body(
    client: AsyncClient, body: dict[str, object], db: aiosqlite.Connection
) -> None:
    await _insert_feed(db)
    await _insert_item(db, "x", "feed1")
    resp = await client.post("/api/prefetch/hint", json=body)
    assert resp.status_code == 422


async def test_prefetch_hint_defaults_to_the_same_filter_as_the_page(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prefetch_ahead's own default was the opposite of the model's, so the
    model default was the only thing keeping the hint aligned with /api/items —
    and flipping it kept all 303 tests green while restoring R12: with the
    show-seen toggle on, the items about to be displayed are never warmed (M9).

    The three existing tests that omit `unseen` each have one item in the DB, so
    prefetch_ahead returns 0 queued and the filter is never exercised.
    """
    warmed: list[str] = []

    async def _record(item_id: str, url: str, client_: object, request_id: str | None = None) -> None:
        warmed.append(url)

    monkeypatch.setattr("src.media.prefetch._warm", _record)

    await _insert_feed(db, "f1")
    for n, seen in ((0, None), (1, None), (2, "2026-01-01 00:00:00")):
        await db.execute(
            """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date, seen_at)
               VALUES (?, 'f1', ?, 'T', ?, 'image', ?, ?)""",
            (f"i{n}", f"g{n}", f"http://img/{n}.jpg", f"2026-01-0{n + 1}", seen),
        )
    await db.commit()

    resp = await client.post("/api/prefetch/hint", json={"item_id": "i0"})
    assert resp.status_code == 200
    await asyncio.sleep(0.05)
    assert "http://img/2.jpg" in warmed, "the default must not filter seen items out"


async def test_prefetch_hint_accepts_the_same_bool_domain_as_the_page(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    """`?unseen=1` parses on /api/items; `{"unseen": 1}` was a 422 on the hint."""
    await _insert_feed(db, "f1")
    await _insert_item(db, "i1", "f1")
    resp = await client.post("/api/prefetch/hint", json={"item_id": "i1", "unseen": 1})
    assert resp.status_code == 200


async def test_prefetch_hint_rejects_an_oversized_id_and_unknown_fields(client: AsyncClient) -> None:
    assert (await client.post("/api/prefetch/hint", json={"item_id": "x" * 129})).status_code == 422
    assert (await client.post("/api/prefetch/hint", json={"item_id": "x", "unseen_only": True})).status_code == 422


async def test_prefetch_hint_404_is_visible_at_the_default_level(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """items.py logs the equivalent vanished-anchor case at INFO, so after a
    prune the 410s are visible at the default level and the 404s from the same
    cause are not (M6)."""
    caplog.set_level(logging.INFO)
    resp = await client.post("/api/prefetch/hint", json={"item_id": "gone"})
    assert resp.status_code == 404
    assert any(r.levelno == logging.INFO and "gone" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# GET /api/reddit-feeds/status tests
# ---------------------------------------------------------------------------


async def test_reddit_feeds_status_success(
    client: AsyncClient, mock_http: respx.MockRouter, reddit_api_url: str
) -> None:
    upstream_json = {
        "feeds": [
            {
                "name": "EarthPorn",
                "last_status": "success",
                "last_fetch": "2026-07-27T14:02:00.123456+00:00",
                "last_item_count": 5,
                "total_items": 42,
            }
        ],
        "last_run": "2026-07-27T14:02:05.654321+00:00",
    }
    mock_http.get(f"{reddit_api_url}/status").mock(return_value=httpx.Response(200, json=upstream_json))
    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 200
    assert resp.json() == upstream_json


async def test_reddit_feeds_status_upstream_error(
    client: AsyncClient, mock_http: respx.MockRouter, reddit_api_url: str
) -> None:
    mock_http.get(f"{reddit_api_url}/status").mock(return_value=httpx.Response(500))
    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 502


async def test_reddit_feeds_status_redirects_become_502(
    client: AsyncClient,
    mock_http: respx.MockRouter,
    reddit_api_url: str,
) -> None:
    """Task 6: a 301 from the upstream must surface as 502 — the trusted URL
    must not be silently rewritten by an attacker-controlled Location header.
    follow_redirects=False is the contract (was True; F10/R13 fixed it then,
    Task 6 closed it for security)."""
    mock_http.get(f"{reddit_api_url}/status").mock(
        return_value=httpx.Response(301, headers={"location": f"{reddit_api_url}/v2/status"})
    )
    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 502


async def test_reddit_feeds_status_401_maps_to_502(
    client: AsyncClient,
    mock_http: respx.MockRouter,
    reddit_api_url: str,
) -> None:
    """F10: upstream 401 must not read as a failure of OUR session."""
    mock_http.get(f"{reddit_api_url}/status").mock(return_value=httpx.Response(401))
    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 502


async def test_reddit_feeds_status_non_json_body(
    client: AsyncClient,
    mock_http: respx.MockRouter,
    reddit_api_url: str,
) -> None:
    """F10: a 200 with a non-JSON body must 502, not 500 with JSONDecodeError."""
    mock_http.get(f"{reddit_api_url}/status").mock(
        return_value=httpx.Response(200, content=b"not json", headers={"content-type": "text/plain"})
    )
    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 502


async def test_items_interleaved_across_feeds(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """Items from multiple feeds should be interleaved round-robin, oldest first."""
    import datetime

    async def insert_feed(fid: str, url: str) -> None:
        await db.execute("INSERT INTO feeds (id, url) VALUES (?, ?)", (fid, url))

    async def insert_item(iid: str, feed_id: str, guid: str, pub_offset_days: int) -> None:
        pub = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=pub_offset_days)).isoformat()
        await db.execute(
            "INSERT INTO items (id, feed_id, guid, title, media_url, media_type, pub_date, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (iid, feed_id, guid, "t", "http://x.com/a.jpg", "image", pub),
        )

    fa, fb = "feedA", "feedB"
    await insert_feed(fa, "http://a.com")
    await insert_feed(fb, "http://b.com")
    # Feed A: 3 items (oldest pub_date = 3 days ago)
    await insert_item("a1", fa, "g1", pub_offset_days=3)
    await insert_item("a2", fa, "g2", pub_offset_days=2)
    await insert_item("a3", fa, "g3", pub_offset_days=1)
    # Feed B: 2 items
    await insert_item("b1", fb, "g1", pub_offset_days=4)
    await insert_item("b2", fb, "g2", pub_offset_days=1)
    await db.commit()

    resp = await client.get("/api/items?size=10")
    assert resp.status_code == 200
    data = resp.json()
    ids = [item["id"] for item in data]

    # Round 1: rn=1 for each feed — feedA (oldest=3d) and feedB (oldest=4d)
    # Round 2: rn=2 for each feed
    # Round 3: only feedA remains (rn=3)
    # Within each round, feedA < feedB alphabetically
    assert ids == ["a1", "b1", "a2", "b2", "a3"]


async def test_items_rejects_invalid_size(client: AsyncClient) -> None:
    for bad in (0, -1, 201, 100000):
        resp = await client.get("/api/items", params={"size": bad})
        assert resp.status_code == 422, f"size={bad} should be rejected"


async def test_items_report_whether_media_is_already_cached(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: object, monkeypatch: object
) -> None:
    """The browser downloads cached items first, so it has to be told which are cached.

    A cached item decodes in milliseconds; an uncached one waits on the origin.
    Without this flag the queue orders blindly and a miss at the front of the
    lookahead holds up hits behind it.
    """
    import hashlib

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    warm = "http://example.com/warm.jpg"
    cold = "http://example.com/cold.jpg"
    (tmp_path / hashlib.sha256(warm.encode()).hexdigest()).write_bytes(b"cached")  # type: ignore[operator]

    await db.execute("INSERT INTO feeds (id, url, title) VALUES ('f1', 'http://x.com', 'X')")
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type, pub_date)"
        " VALUES ('i1', 'f1', 'g1', ?, 'image', '2024-01-01')",
        (warm,),
    )
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type, pub_date)"
        " VALUES ('i2', 'f1', 'g2', ?, 'image', '2024-01-02')",
        (cold,),
    )
    await db.commit()

    resp = await client.get("/api/items")
    assert resp.status_code == 200
    cached_by_id = {i["id"]: i["cached"] for i in resp.json()}
    assert cached_by_id == {"i1": True, "i2": False}


async def test_items_cached_true_for_warm_media(
    client: AsyncClient,
    db: aiosqlite.Connection,
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    warm = "http://example.com/warm.jpg"
    (tmp_path / hashlib.sha256(warm.encode()).hexdigest()).write_bytes(b"x")
    await db.execute("INSERT INTO feeds(id,url,title) VALUES ('f1','http://x','X')")
    await db.execute(
        "INSERT INTO items(id,feed_id,guid,media_url,media_type,pub_date)"
        " VALUES ('i1','f1','g1',?,'image','2026-01-01')",
        (warm,),
    )
    await db.commit()
    resp = await client.get("/api/items")
    assert resp.json()[0]["cached"] is True


async def test_items_rank_ties_break_by_id(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """R12: rn must be deterministic when two items share a pub_date.

    The cursor (Task 2) derives rn by counting rows <= (pub_date, id), which
    only equals ROW_NUMBER if the window breaks ties by id. Inserted out of
    order so insertion order cannot pass this by accident.
    """
    await _insert_feed(db)
    for item_id in ("zz", "mm", "aa"):
        await db.execute(
            """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
               VALUES (?, 'feed1', ?, 'T', 'http://example.com/img.jpg', 'image', '2026-01-01T00:00:00')""",
            (item_id, item_id),
        )
    await db.commit()

    resp = await client.get("/api/items")
    assert resp.status_code == 200
    data = resp.json()
    assert [i["id"] for i in data] == ["aa", "mm", "zz"]


async def test_items_cursor_survives_prune_beneath_it(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """R3: pruning rows below an outstanding cursor must not skip items.

    rn is recomputed per request, so deleting the two lowest rows used to shift
    every later rn down by two — a client holding the old rn started two items
    too far ahead and never rendered them. prune_items does exactly this on
    every refresh cycle.
    """
    await _insert_feed(db)
    for n in range(1, 9):
        await db.execute(
            """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
               VALUES (?, 'feed1', ?, 'T', 'http://example.com/img.jpg', 'image', ?)""",
            (f"item{n}", f"g{n}", f"2026-01-0{n}T00:00:00"),
        )
    await db.commit()

    page1 = await client.get("/api/items", params={"size": 4})
    assert [i["id"] for i in page1.json()] == ["item1", "item2", "item3", "item4"]
    last = page1.json()[-1]

    # A refresh cycle prunes the two oldest rows while the client holds its cursor.
    await db.execute("DELETE FROM items WHERE id IN ('item1', 'item2')")
    await db.commit()

    page2 = await client.get(
        "/api/items",
        params={"after_id": last["id"], "size": 4},
    )
    assert [i["id"] for i in page2.json()] == ["item5", "item6", "item7", "item8"]


async def test_items_cursor_with_after_rn_survives_prune_beneath_it(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    """R3, with the client sending after_rn (Task 8's frontend).

    bound_rn = min(after_rn, resolved) must take the *resolved* rank when a
    prune has moved it below what was issued. A mutation that trusts after_rn
    unconditionally — dropping the resolved value entirely — would still page
    from the client's stale, too-high rank and skip the same rows R3 already
    covers, but only for a client that sends after_rn. The after_id-only test
    above never exercises this: min(2, 3) picked the issued side by
    coincidence there, so a mutation that always trusts after_rn passes it too.
    """
    await _insert_feed(db)
    for n in range(1, 9):
        await db.execute(
            """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
               VALUES (?, 'feed1', ?, 'T', 'http://example.com/img.jpg', 'image', ?)""",
            (f"item{n}", f"g{n}", f"2026-01-0{n}T00:00:00"),
        )
    await db.commit()

    page1 = await client.get("/api/items", params={"size": 4})
    assert [i["id"] for i in page1.json()] == ["item1", "item2", "item3", "item4"]
    last = page1.json()[-1]
    assert "rn" in last

    # A refresh cycle prunes the two oldest rows while the client holds its cursor.
    await db.execute("DELETE FROM items WHERE id IN ('item1', 'item2')")
    await db.commit()

    page2 = await client.get(
        "/api/items",
        params={"after_id": last["id"], "after_rn": last["rn"], "size": 4},
    )
    assert [i["id"] for i in page2.json()] == ["item5", "item6", "item7", "item8"]


async def test_items_cursor_does_not_skip_when_a_feed_gains_an_older_row(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    """rn is ROW_NUMBER recomputed per request. A row inserted into a feed that
    sorts before the client's anchor raises the anchor's rn, so the next page
    asks for a window past rows the client never received. The trigger is
    routine: _parse_pub_date returns None for an undated entry and ROW_NUMBER
    sorts NULLs first, renumbering that feed's whole partition (M2)."""
    await _insert_feed(db, "fa", "http://a.example/feed.xml")
    await _insert_feed(db, "fb", "http://b.example/feed.xml")
    for feed in ("fa", "fb"):
        for n in range(4):
            await db.execute(
                """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
                   VALUES (?, ?, ?, 'T', ?, 'image', ?)""",
                (f"{feed}{n}", feed, f"{feed}{n}", f"http://img/{feed}{n}.jpg", f"2026-01-0{n + 1}"),
            )
    await db.commit()

    first = await client.get("/api/items?size=3")
    assert first.status_code == 200
    page1 = first.json()
    assert len(page1) == 3
    last = page1[-1]
    assert "rn" in last, "the client cannot send back a rank it was never given"

    # An undated entry arrives in feed fa and renumbers its whole partition.
    await db.execute(
        """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
           VALUES ('fa_new', 'fa', 'fa_new', 'T', 'http://img/fa_new.jpg', 'image', NULL)"""
    )
    await db.commit()

    second = await client.get(f"/api/items?size=8&after_id={last['id']}&after_rn={last['rn']}")
    assert second.status_code == 200

    got = {i["id"] for i in page1} | {i["id"] for i in second.json()}
    async with db.execute("SELECT id FROM items") as cur:
        every = {row["id"] for row in await cur.fetchall()}
    # fa_new itself now ranks rn=1 in feed fa — below fa0, which was already
    # delivered in page1 at the old rn=1. No forward cursor bounded by the last
    # item's own rank can reach a row that ranks before content already shown;
    # that would need a fresh page-1 load, not a page turn. What M2 promises is
    # that a row which already existed and was merely displaced (fb1, whose rn
    # the fa-partition insert never touched) is not skipped.
    missing = every - got - {"fa_new"}
    assert not missing, f"the cursor skipped {missing}"


async def test_proxy_cache_hit_evicted_before_send_is_not_a_500(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starlette's FileResponse stats by path before http.response.start and
    opens by path after it, so evict() unlinking between the two is real and
    cannot be closed from here. What it must not do is raise RuntimeError with
    the container's cache path in the log, after the handler has returned and
    outside every except clause it has (minor 1).

    The two tests this replaces both patched _cache_path to a path that never
    existed, so cache_lookup returned None and they took the ordinary miss path.
    """
    from src.config import settings
    from src.media import cache as cache_mod

    monkeypatch.setattr(settings, "cache_dir", str(tmp_path))
    url = "http://example.com/evicted.jpg"
    await _insert_feed(db, "f1")
    await db.execute(
        "INSERT INTO items(id, feed_id, guid, media_url, media_type) VALUES ('i1','f1','g1',?,'image')",
        (url,),
    )
    await db.commit()

    path = cache_mod._cache_path(url)
    path.write_bytes(b"cached")
    path.with_suffix(".meta").write_text("image/jpeg", encoding="ascii")

    real_lookup = cache_mod.cache_lookup

    def _lookup_then_evict(u: str) -> tuple[Path, str] | None:
        result = real_lookup(u)
        path.unlink(missing_ok=True)  # evict() lands here
        return result

    monkeypatch.setattr("src.api.media.cache_lookup", _lookup_then_evict)
    monkeypatch.setattr(client._transport, "raise_app_exceptions", False)

    resp = await client.get("/api/media/proxy", params={"url": url})
    assert resp.status_code == 503
    assert str(tmp_path) not in resp.text


async def test_proxy_upstream_error_logged_at_warning(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_http: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
    db: aiosqlite.Connection,
) -> None:
    """R10: the 502 is raised only after a URL was marked dead and a fully-dead
    item dropped. That was logged at DEBUG, and log_level defaults to info — so
    in a default deployment the operator saw a 502, a vanished item, and no
    explanation."""
    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/gone.jpg"
    await _register_proxy_url(db, url)
    mock_http.get(_pinned(url)).mock(return_value=httpx.Response(404))

    with caplog.at_level(logging.WARNING, logger="src.api.media"):
        resp = await client.get(f"/api/media/proxy?url={url}&item_id=i1")

    assert resp.status_code == 502
    assert any("i1" in m and url in m for m in caplog.messages)


async def test_proxy_cdn_403_logs_exactly_one_warning_across_both_loggers(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_http: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
    db: aiosqlite.Connection,
) -> None:
    """proxy_media's own `except UpstreamError` used to warn unconditionally on
    top of whatever open_upstream already logged. That was harmless while
    open_upstream's raise sites were DEBUG-only, but promoting them to WARNING
    (M6 follow-up) turned it into a double-log on the higher-traffic proxy
    path. caplog is captured unscoped (not filtered to one logger by name, the
    gap that let the double-log through review) so a second WARNING from
    either src.media.fetch or src.api.media would be visible here."""
    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/forbidden.jpg"
    await _register_proxy_url(db, url)
    mock_http.get(_pinned(url)).mock(return_value=httpx.Response(403))

    caplog.set_level(logging.DEBUG)
    resp = await client.get(f"/api/media/proxy?url={url}&item_id=i1")

    assert resp.status_code == 502
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [f"{r.name}: {r.message}" for r in warnings]


async def test_proxy_dns_failure_logs_exactly_one_warning_across_both_loggers(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    db: aiosqlite.Connection,
) -> None:
    """Same double-log risk as the 403 case above, for M6's other named
    scenario ('DNS broken'), exercised end to end through the proxy route."""
    import src.media.cache as cache_mod
    from src.media import fetch as fetch_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    def _dns_failure(host: str) -> list[str]:
        raise OSError("nodename not known")

    monkeypatch.setattr(fetch_mod, "_resolve", _dns_failure)
    url = "http://broken-dns.example.com/x.jpg"
    await _register_proxy_url(db, url)

    caplog.set_level(logging.DEBUG)
    resp = await client.get(f"/api/media/proxy?url={url}&item_id=i1")

    assert resp.status_code == 502
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [f"{r.name}: {r.message}" for r in warnings]


async def test_proxy_502_carries_the_upstream_duration(
    client: AsyncClient,
    db: aiosqlite.Connection,
    mock_http: respx.MockRouter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 502 from an instant connection refusal and one from the 30s read
    timeout produced identical lines, and duration is the field that separates
    'the origin is gone' from 'the origin is slow' (minor 9).

    proxy_media's own failure-exit logging stays at DEBUG (51f57d9: promoting
    it to WARNING would double-log alongside open_upstream's own warning on
    every failure), so this captures at DEBUG rather than WARNING.
    """
    from src.config import settings

    monkeypatch.setattr(settings, "cache_dir", str(tmp_path))
    caplog.set_level(logging.DEBUG)
    url = "http://example.com/dead.jpg"
    await _insert_feed(db, "f1")
    await db.execute(
        "INSERT INTO items(id, feed_id, guid, media_url, media_type) VALUES ('i1','f1','g1',?,'image')", (url,)
    )
    await db.commit()
    mock_http.get(_pinned(url)).mock(return_value=httpx.Response(404))

    resp = await client.get("/api/media/proxy", params={"url": url})
    assert resp.status_code == 502
    assert any("ms" in r.message for r in caplog.records if "502" in r.message)


async def test_a_logged_url_is_escaped_and_truncated(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A newline in a query parameter forges a whole log line against the
    single-line format main.py installs (minor 19).

    Scoped to the "src" logger (every app module's logger, not one leaf name —
    the gap that let a prior double-log finding through review) rather than
    root: unscoped DEBUG also captures aiosqlite's own parameter-echoing debug
    log, which is third-party and out of this task's reach.
    """
    from src.config import settings

    monkeypatch.setattr(settings, "cache_dir", str(tmp_path))
    caplog.set_level(logging.DEBUG, logger="src")
    await client.get("/api/media/proxy", params={"url": "http://x/\nFAKE LOG LINE" + "y" * 500})

    for record in caplog.records:
        assert "\nFAKE" not in record.message, "the newline must be escaped"
        assert len(record.message) < 400, "the value must be truncated"


async def test_a_logged_url_is_escaped_past_the_gate_too(
    client: AsyncClient,
    db: aiosqlite.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gate-rejection test above 404s before _check_url, open_upstream or
    cache_stream_tee ever run — it cannot prove those downstream sites are
    safe (Critical 2 on this branch's review: url logged raw three call-frames
    past the gate, at WARNING).

    Registering the malicious url as a real item's media_url lets it clear
    the gate; a non-http(s) scheme then trips _check_url's very first check,
    which raises before any network call — so this needs no upstream mock and
    still walks url through proxy_media -> open_upstream -> _check_url.
    """
    from src.config import settings

    monkeypatch.setattr(settings, "cache_dir", str(tmp_path))
    url = "ftp://evil.example/\nFAKE LOG LINE" + "y" * 500
    await _register_proxy_url(db, url)

    caplog.set_level(logging.DEBUG, logger="src")
    resp = await client.get("/api/media/proxy", params={"url": url})

    assert resp.status_code == 502
    assert any("_check_url: refusing non-http(s) URL" in r.message for r in caplog.records), (
        "the test must actually reach _check_url, not just the gate"
    )
    for record in caplog.records:
        assert "\nFAKE" not in record.message, f"{record.name}: raw newline reached a log record"
        # 600, not 400: proxy_media's own 502 line embeds the escaped url twice
        # (once directly, once inside the UpstreamError str() it also renders),
        # so two ~200-char truncated copies plus surrounding text lands here —
        # still bounded, just not by the single-occurrence budget the other
        # escaping test uses.
        assert len(record.message) < 600, f"{record.name}: unbounded value reached a log record"


async def test_proxy_serves_a_gallery_slide_with_a_non_ascii_url(
    client: AsyncClient,
    db: aiosqlite.Connection,
    mock_http: respx.MockRouter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slides 2..N of a gallery live only in media_json (M3)."""
    import json

    from src.config import settings

    monkeypatch.setattr(settings, "cache_dir", str(tmp_path))
    slide = "http://example.com/café.jpg"
    await _insert_feed(db, "f1")
    await db.execute(
        """INSERT INTO items(id, feed_id, guid, media_url, media_type, media_json)
           VALUES ('i1', 'f1', 'g1', 'http://example.com/one.jpg', 'image', ?)""",
        (json.dumps([{"url": "http://example.com/one.jpg", "type": "image"}, {"url": slide, "type": "image"}]),),
    )
    await db.commit()
    mock_http.get(_pinned(slide)).mock(
        return_value=httpx.Response(200, content=b"jpg", headers={"content-type": "image/jpeg"})
    )

    resp = await client.get("/api/media/proxy", params={"url": slide})
    assert resp.status_code == 200


async def test_prefetch_hint_logs_entry_and_queue_size(
    client: AsyncClient,
    db: aiosqlite.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R9: the endpoint logged nothing on any path, and the client discards the
    response — so 'prefetch warms nothing' and 'the browser never called' were
    indistinguishable."""
    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/feed', 'F')")
    await db.execute(
        """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
           VALUES ('i1', 'f1', 'g1', 'T', 'http://example.com/1.jpg', 'image', '2026-01-01T00:00:00')"""
    )
    await db.commit()

    with caplog.at_level(logging.DEBUG, logger="src.api.media"):
        resp = await client.post("/api/prefetch/hint", json={"item_id": "i1"})

    assert resp.status_code == 200
    assert any("i1" in m for m in caplog.messages)
    assert any("queued" in m for m in caplog.messages)


async def test_reddit_feeds_status_passes_through_json_array(
    client: AsyncClient, mock_http: respx.MockRouter, reddit_api_url: str
) -> None:
    """R4: FastAPI derives a response model from the `-> dict` annotation and
    validates after the handler returns, outside the try. A companion answering
    200 with [] became a 500 — the opposite of dadd0d6's whole purpose."""
    mock_http.get(f"{reddit_api_url}/status").mock(return_value=httpx.Response(200, json=[]))
    resp = await client.get("/api/reddit-feeds/status")

    assert resp.status_code == 200
    assert resp.json() == []


async def test_reddit_feeds_status_non_200_success_is_not_502(
    client: AsyncClient, mock_http: respx.MockRouter, reddit_api_url: str
) -> None:
    """A 202 from the companion is a success, not an error."""
    mock_http.get(f"{reddit_api_url}/status").mock(return_value=httpx.Response(202, json={"ok": True}))
    resp = await client.get("/api/reddit-feeds/status")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_reddit_feeds_status_logs_the_exception(
    client: AsyncClient,
    mock_http: respx.MockRouter,
    reddit_api_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R11: httpx timeouts frequently stringify to empty, degrading the line to
    'reddit_feeds_status unreachable:' with no exception type and no traceback."""
    mock_http.get(f"{reddit_api_url}/status").mock(side_effect=httpx.ConnectTimeout(""))

    with caplog.at_level(logging.WARNING, logger="src.api.reddit_feeds"):
        resp = await client.get("/api/reddit-feeds/status")

    assert resp.status_code == 502
    record = next(r for r in caplog.records if r.name == "src.api.reddit_feeds")
    assert "ConnectTimeout" in record.getMessage()
    assert record.exc_info is not None


async def test_reddit_feeds_status_logs_transitions_not_polls(
    client: AsyncClient,
    mock_http: respx.MockRouter,
    reddit_api_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The modal polls at 1 Hz against an optional service, so one WARNING per
    poll with a full traceback is ~600 log lines a minute for a condition the
    frontend already renders (M5)."""
    caplog.set_level(logging.DEBUG, logger="src.api.reddit_feeds")
    route = mock_http.get(f"{reddit_api_url}/status")
    route.mock(side_effect=httpx.ConnectError("refused"))

    for _ in range(3):
        assert (await client.get("/api/reddit-feeds/status")).status_code == 502

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "only the transition to unreachable warns"
    assert warnings[0].exc_info is not None, "the traceback rides the transition"
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and "unreachable" in r.message]
    assert len(debugs) == 2, "the repeats are debug"

    caplog.clear()
    route.mock(return_value=httpx.Response(200, json={"ok": True}))
    assert (await client.get("/api/reddit-feeds/status")).status_code == 200
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1, "recovery is an important status change"


async def test_proxy_cache_hit_honours_range(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db: aiosqlite.Connection
) -> None:
    """R14: the hit path uses FileResponse specifically because it is
    Range-capable — 'what makes a cached video seekable'. Nothing tested it, so
    swapping in a plain Response kept the suite green and broke seeking."""
    import hashlib

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/clip.mp4"
    await _register_proxy_url(db, url)
    filename = hashlib.sha256(url.encode()).hexdigest()
    (tmp_path / filename).write_bytes(b"0123456789")
    (tmp_path / f"{filename}.meta").write_text("video/mp4")

    resp = await client.get(f"/api/media/proxy?url={url}", headers={"Range": "bytes=2-5"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 2-5/10"
    assert resp.content == b"2345"


async def test_proxy_cache_miss_ignores_range(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_http: respx.MockRouter,
    db: aiosqlite.Connection,
) -> None:
    """The documented other half (F7): the miss path streams and deliberately
    does not honour Range, because streaming misses through is what prevents
    the black-screen stall on first paint."""
    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/fresh.mp4"
    await _register_proxy_url(db, url)
    mock_http.get(_pinned(url)).mock(
        return_value=httpx.Response(200, content=b"0123456789", headers={"content-type": "video/mp4"})
    )
    resp = await client.get(f"/api/media/proxy?url={url}", headers={"Range": "bytes=2-5"})

    assert resp.status_code == 200
    assert resp.content == b"0123456789"


async def test_is_known_media_url_primary_and_gallery(db: aiosqlite.Connection) -> None:
    from src.media.availability import is_known_media_url

    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x', 'X')")
    await db.execute(
        "INSERT INTO items(id, feed_id, guid, media_url, media_type, media_json)"
        " VALUES ('i1', 'f1', 'g1', 'http://primary.jpg', 'image',"
        ' \'[{"url":"http://slide-a.jpg","type":"image"},{"url":"http://slide-b.jpg","type":"image"}]\')'
    )
    await db.commit()
    assert await is_known_media_url("http://primary.jpg", db) is True
    assert await is_known_media_url("http://slide-b.jpg", db) is True
    assert await is_known_media_url("http://not-in-items.jpg", db) is False


async def test_proxy_rejects_unknown_url(client: AsyncClient, tmp_path: object, monkeypatch: object) -> None:
    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/not-in-db.jpg"
    resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not a known media url"


async def test_items_response_includes_rn(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """rn must reach the client so it can be sent back as after_rn (M2)."""
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1")
    data = (await client.get("/api/items")).json()
    assert data and "rn" in data[0]


async def test_list_items_logs_db_duration(
    client: AsyncClient, db: aiosqlite.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1")
    caplog.set_level(logging.DEBUG, logger="src.api.items")
    await client.get("/api/items")
    assert any(re.search(r"db=\S*ms", r.getMessage()) and "list_items" in r.getMessage() for r in caplog.records), (
        "list_items exit log must include the DB query duration"
    )


async def test_proxy_exception_uses_logger_exception(
    client: AsyncClient,
    db: aiosqlite.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import src.media.cache as cache_mod
    from src.media import fetch as fetch_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/transport.jpg"
    await db.execute("INSERT INTO feeds(id,url,title) VALUES ('f','http://x','X')")
    await db.execute("INSERT INTO items(id,feed_id,guid,media_url,media_type) VALUES ('i','f','g',?, 'image')", (url,))
    await db.commit()
    with respx.mock:
        respx.get(fetch_mod._pinned_url(url, "93.184.216.34")).mock(side_effect=httpx.ConnectError("boom"))
        caplog.set_level(logging.WARNING, logger="src.api.media")
        await client.get(f"/api/media/proxy?url={url}")
    assert any(r.levelno >= logging.WARNING and r.exc_info for r in caplog.records), (
        "the generic except must use logger.exception (exc_info set)"
    )


async def test_items_cursor_pruned_anchor_is_410(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """The anchor row is pruned between page-1 and page-2. The old COUNT(*)
    derivation guessed a position from surviving rows and, when none survived,
    guessed 0 and restarted the global ordering. An anchor that is gone is now
    reported, not guessed at; the browser walks back to an older anchor."""
    await _insert_feed(db)
    for i in range(1, 6):
        await _insert_item(db, f"item{i}", "feed1")
    page1 = (await client.get("/api/items", params={"size": 3})).json()
    assert [i["id"] for i in page1] == ["item1", "item2", "item3"]
    await db.execute("DELETE FROM items WHERE id = 'item3'")
    await db.commit()
    resp = await client.get("/api/items", params={"after_id": "item3", "size": 10})
    assert resp.status_code == 410
    # The next-best anchor still works, which is what the client falls back to.
    page2 = (await client.get("/api/items", params={"after_id": "item2", "size": 10})).json()
    assert [i["id"] for i in page2] == ["item4", "item5"]


async def test_proxy_upstream_error_detail(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    import respx

    import src.media.cache as cache_mod
    import src.media.fetch as fetch_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/missing.jpg"
    await db.execute("INSERT INTO feeds(id,url,title) VALUES ('f','http://x','X')")
    await db.execute("INSERT INTO items(id,feed_id,guid,media_url,media_type) VALUES ('i','f','g',?, 'image')", (url,))
    await db.commit()
    with respx.mock:
        respx.get(fetch_mod._pinned_url(url, "93.184.216.34")).mock(return_value=httpx.Response(404))
        resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream error"


async def test_proxy_transport_error_detail(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    import respx

    import src.media.cache as cache_mod
    import src.media.fetch as fetch_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/unreachable.jpg"
    await db.execute("INSERT INTO feeds(id,url,title) VALUES ('f','http://x','X')")
    await db.execute("INSERT INTO items(id,feed_id,guid,media_url,media_type) VALUES ('i','f','g',?, 'image')", (url,))
    await db.commit()
    with respx.mock:
        respx.get(fetch_mod._pinned_url(url, "93.184.216.34")).mock(side_effect=httpx.ConnectError("boom"))
        resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream fetch failed"


async def test_prefetch_hint_warms_cache(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import httpx
    import respx

    import src.media.cache as cache_mod
    import src.media.fetch as fetch_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    await _insert_feed(db)
    url = "http://example.com/img.jpg"
    await db.execute(
        """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date, seen_at)
           VALUES ('item1', 'feed1', 'g1', 'T', ?, 'image', ?, NULL)""",
        (url, "2026-01-01T00:00:00"),
    )
    await db.execute(
        """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date, seen_at)
           VALUES ('item2', 'feed1', 'g2', 'T', ?, 'image', ?, NULL)""",
        (url, "2026-01-02T00:00:00"),
    )
    await db.commit()
    with respx.mock:
        respx.get(fetch_mod._pinned_url(url, "93.184.216.34")).mock(
            return_value=httpx.Response(200, content=b"jpg", headers={"content-type": "image/jpeg"})
        )
        resp = await client.post("/api/prefetch/hint", json={"item_id": "item1", "unseen": True})
        assert resp.status_code == 200
        from src.media import prefetch as _pf

        bg = getattr(_pf, "_bg_tasks", None) or getattr(_pf, "_tasks", None) or getattr(_pf, "background_tasks", None)
        if bg:
            await asyncio.gather(*list(bg), return_exceptions=True)
    from src.media.cache import cache_read

    assert cache_read(url) is not None, "prefetch_hint must actually warm the cache"


async def test_feeds_zero_item_counts(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f0', 'http://x', 'Empty')")
    await db.commit()
    data = (await client.get("/api/feeds")).json()
    feed = next(f for f in data if f["id"] == "f0")
    assert feed["item_count"] == 0
    assert feed["unseen_count"] == 0


@pytest.mark.parametrize("size", [1, 200])
async def test_items_accepts_size_boundaries(client: AsyncClient, db: aiosqlite.Connection, size: int) -> None:
    await _insert_feed(db)
    for n in range(3):
        await _insert_item(db, f"item{n}", "feed1")
    resp = await client.get("/api/items", params={"size": size})
    assert resp.status_code == 200
    assert len(resp.json()) == min(size, 3), "size must actually limit the page"


async def test_proxy_non_media_content_type_is_not_an_error_log(
    client: AsyncClient,
    db: aiosqlite.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """NonMediaUpstreamError is deliberately not a subclass of UpstreamError:
    nothing is cached, nothing is marked dead, and the condition can flip back.
    media.py imported only UpstreamError, so a WAF page or a login interstitial
    fell into the catch-all and produced logger.exception — ERROR with a full
    traceback, once per affected image — while open_upstream had already logged
    the same event at WARNING with the real content type.
    """
    import httpx
    import respx

    import src.media.cache as cache_mod
    import src.media.fetch as fetch_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/challenge.jpg"
    await _register_proxy_url(db, url)
    caplog.set_level(logging.DEBUG, logger="src.api.media")

    with respx.mock:
        respx.get(fetch_mod._pinned_url(url, "93.184.216.34")).mock(
            return_value=httpx.Response(200, content=b"<html>go away</html>", headers={"content-type": "text/html"})
        )
        resp = await client.get(f"/api/media/proxy?url={url}")

    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream content type not media", (
        "the fetch did not fail — the content type was refused"
    )
    media_records = [r for r in caplog.records if r.name == "src.api.media"]
    assert not any(r.levelno >= logging.ERROR for r in media_records), (
        "an expected, reversible outcome must not log at ERROR"
    )
    assert not any(r.exc_info for r in media_records), "and must not carry a traceback"


async def test_proxy_hit_does_not_pass_stat_result_to_fileresponse(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing stat_result suppresses Starlette's own os.stat inside
    FileResponse.__call__ — the check that fails *before* any bytes go out.
    With it suppressed, an eviction between the route and the send sends
    http.response.start with a correct Content-Length and then a body that
    dies mid-flight, instead of failing cleanly."""
    import src.api.media as media_mod
    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/warm.jpg"
    await _register_proxy_url(db, url)
    (tmp_path / cache_mod.cache_name(url)).write_bytes(b"bytes")

    captured: dict[str, object] = {}
    real_file_response = media_mod.FileResponse

    def _spy(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_file_response(*args, **kwargs)

    monkeypatch.setattr(media_mod, "FileResponse", _spy)
    resp = await client.get(f"/api/media/proxy?url={url}")

    assert resp.status_code == 200
    assert "stat_result" not in captured, (
        "Starlette must re-stat at send time so a vanished file fails before headers go out"
    )


async def test_items_page_survives_one_unparseable_row(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "good", "feed1")
    await db.execute(
        """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date, media_json)
           VALUES ('bad', 'feed1', 'gbad', 'T', 'http://example.com/img.jpg', 'image',
                   datetime('now'), '[{"url": "trunc')"""
    )
    await db.commit()
    resp = await client.get("/api/items")
    assert resp.status_code == 200, "one bad row must not take the page down"
    assert {i["id"] for i in resp.json()} == {"good", "bad"}


async def test_mark_seen_404_leaves_no_open_transaction(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """sqlite3 opens an implicit transaction before any DML, including an
    UPDATE that matches nothing. Raising the 404 without closing it left the
    process-wide connection holding a RESERVED lock forever: every
    run_with_own_db write on its own connection then waited out the 30 s busy
    timeout and was swallowed, and WAL could not checkpoint. Counting rollbacks
    cannot see that — only the transaction state can.
    """
    resp = await client.post("/api/items/nonexistent/seen")
    assert resp.status_code == 404
    assert db._conn is not None
    assert db._conn.in_transaction is False, "a 404 must not strand a write transaction"


async def test_mark_seen_404_does_not_block_another_connection(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """The consequence, end to end: run_with_own_db opens its own connection to
    mark a URL dead or record a digest, and a stranded RESERVED lock turned
    every one of those into a 30 s wait and a swallowed OperationalError."""
    from src.config import settings

    assert (await client.post("/api/items/nonexistent/seen")).status_code == 404

    other = await aiosqlite.connect(settings.db_path, timeout=1)
    try:
        await other.execute("INSERT INTO feeds(id, url, title) VALUES ('other', 'http://o', 'O')")
        await other.commit()
    finally:
        await other.close()


async def test_mark_seen_logs_when_it_rolls_back(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A locked writer discarding the user's mark-as-seen left no record that
    a rollback ran — open_db sets a 30 s busy timeout precisely because
    contention happens."""
    caplog.set_level(logging.WARNING, logger="src.api.items")
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1", seen_at=None)

    orig_execute = db.execute

    def _flaky_execute(query: str, params: object = None) -> object:
        if "INSERT OR REPLACE INTO seen_media" in query:
            raise aiosqlite.OperationalError("database is locked")
        return orig_execute(query, params)

    monkeypatch.setattr(db, "execute", _flaky_execute)
    monkeypatch.setattr(client._transport, "raise_app_exceptions", False)
    resp = await client.post("/api/items/item1/seen")

    assert resp.status_code == 500
    assert any("roll" in r.getMessage().lower() and "item1" in r.getMessage() for r in caplog.records)


async def test_two_overlapping_mark_seen_requests_do_not_corrupt_each_other(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    """get_db hands both requests the same connection and sqlite3 opens one
    implicit transaction per connection, not per coroutine, so without
    serialisation one request's ROLLBACK discards the other's UPDATE and leaves
    seen_media written with items.seen_at unset (M8, F11).

    A 404 is mixed into the six real ids: with six existing items alone no
    request ever raises, so write_transaction never reaches db.rollback() and
    the counts hold at 6/6 with or without the lock. The 404 forces a genuine
    ROLLBACK on the shared connection concurrently with the other five
    requests' uncommitted UPDATEs — the only thing that can discard them.

    The lock had no test of any kind: deleting `async with _write_lock:` left
    all 303 green.
    """
    await _insert_feed(db, "f1")
    for n in range(6):
        await db.execute(
            "INSERT INTO items(id, feed_id, guid, media_url, media_type) VALUES (?,'f1',?,?,'image')",
            (f"i{n}", f"g{n}", f"http://img/{n}.jpg"),
        )
    await db.commit()

    requests = [client.post(f"/api/items/i{n}/seen") for n in range(6)]
    requests.append(client.post("/api/items/nonexistent/seen"))
    *ok_responses, missing_response = await asyncio.gather(*requests)
    assert all(r.status_code == 200 for r in ok_responses)
    assert missing_response.status_code == 404

    async with db.execute("SELECT COUNT(*) AS n FROM items WHERE seen_at IS NOT NULL") as cur:
        assert (await cur.fetchone())["n"] == 6
    async with db.execute("SELECT COUNT(*) AS n FROM seen_media") as cur:
        assert (await cur.fetchone())["n"] == 6, "every mark wrote both rows or neither"


async def test_mark_seen_reports_the_original_error_when_rollback_also_fails(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The only unexecuted lines in src/api. Rollback is I/O on a possibly
    broken connection; letting it propagate would replace the exception that
    describes what actually went wrong (minor 20)."""
    await _insert_feed(db, "f1")
    await db.execute(
        "INSERT INTO items(id, feed_id, guid, media_url, media_type) VALUES ('i1','f1','g1','http://a.jpg','image')"
    )
    await db.commit()

    real_execute = db.execute

    def _fail_on_seen_media(sql: str, params: tuple = ()):  # noqa: ANN202
        if "seen_media" in sql:
            raise RuntimeError("insert exploded")
        return real_execute(sql, params)

    async def _fail_rollback() -> None:
        raise RuntimeError("rollback exploded")

    monkeypatch.setattr(db, "execute", _fail_on_seen_media)
    monkeypatch.setattr(db, "rollback", _fail_rollback)
    monkeypatch.setattr(client._transport, "raise_app_exceptions", False)
    caplog.set_level(logging.DEBUG)

    resp = await client.post("/api/items/i1/seen")
    assert resp.status_code == 500
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "write failed" in r.getMessage()]
    assert warnings and warnings[0].exc_info is not None, "minor 8: the warning carries the exception"
    # exc_info alone is not enough: a warning with SOME traceback is logged
    # either way. The contract is which exception it carries — the original
    # write failure, not the rollback's own — so pin the exception value.
    assert str(warnings[0].exc_info[1]) == "insert exploded", (
        "minor 20: the original exception must reach the client, not the rollback failure"
    )


async def test_setup_write_is_not_discarded_by_a_concurrent_failing_write(
    auth_client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """post_setup wrote and committed on the same shared connection without
    taking the lock, so a concurrent rollback could discard the TOTP secret
    while the user was redirected to / as though setup succeeded (minor 2).

    Drives the real /setup route (auth_client mounts the auth router on this
    same db) rather than a hand-copied replica of what post_setup does — a
    replica stays green even if post_setup itself stops using
    write_transaction, which is exactly the regression minor 2 describes.

    The race needs forcing: a natural asyncio.gather of the HTTP call and a
    concurrent write resolves the HTTP call's INSERT-then-commit as one
    uninterrupted run in practice, never actually overlapping the other
    writer. db.execute is wrapped to release the concurrent writer the instant
    the real INSERT statement returns — before post_setup's route has had a
    chance to commit or (with the fix) release write_transaction's lock — so
    the two are forced to contend on the same window every run. This targets
    only the literal `'totp_secret'` INSERT, not the earlier SELECT guard
    that also mentions the key, so the writer isn't released before post_setup
    has even started writing.

    The concurrent writer does no statement of its own before rolling back —
    what it would have written is irrelevant to the corruption mechanism, only
    that rollback() runs on the shared connection while the INSERT is still
    uncommitted. An UPDATE first was tried and measurably lost the race: it
    round-trips to aiosqlite's worker thread, which reliably let post_setup's
    single scheduling tick reach commit() first even in the unfixed code,
    making the test pass whether or not the bug was present.
    """
    import pyotp
    from fastapi import HTTPException

    from src.auth.session import SETUP_COOKIE, sign_setup_cookie
    from src.config import settings
    from src.db.connection import write_transaction

    secret = pyotp.random_base32()
    auth_client.cookies.set(SETUP_COOKIE, sign_setup_cookie(secret, settings.auth_secret_key))
    code = pyotp.TOTP(secret).now()

    about_to_insert = asyncio.Event()
    real_execute = db.execute

    async def _delayed_insert(sql: str, params: tuple) -> aiosqlite.Cursor:
        result = await real_execute(sql, params)
        about_to_insert.set()
        await asyncio.sleep(0)
        return result

    def _synced_execute(sql: str, params: tuple = ()):  # noqa: ANN202
        # _load_totp_secret's SELECT also mentions 'totp_secret' and is used as
        # `async with db.execute(...)`, which a coroutine can't satisfy — only
        # the plain-awaited INSERT is wrapped; the SELECT passes through
        # untouched so it keeps aiosqlite's dual await/async-with object.
        if "INSERT OR REPLACE INTO auth_config" in sql:
            return _delayed_insert(sql, params)
        return real_execute(sql, params)

    async def failing_write() -> None:
        await about_to_insert.wait()
        with contextlib.suppress(HTTPException):
            async with write_transaction(db):
                raise HTTPException(status_code=404, detail="Not found")

    monkeypatch.setattr(db, "execute", _synced_execute)
    setup_resp, _ = await asyncio.gather(
        auth_client.post("/setup", data={"totp_code": code}),
        failing_write(),
    )
    monkeypatch.undo()

    assert setup_resp.status_code == 303
    async with db.execute("SELECT value FROM auth_config WHERE key = 'totp_secret'") as cur:
        row = await cur.fetchone()
    assert row is not None and row["value"] == secret


async def test_reddit_feeds_status_caps_the_body(
    client: AsyncClient, mock_http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole upstream body was buffered by a non-streaming client.get,
    parsed once purely as a validity check, discarded, then echoed verbatim —
    with no size cap anywhere. httpx's timeout=10 is per-operation, not a
    whole-request budget, so a trickling companion can hold the connection and
    grow the buffer."""
    from src.config import settings

    monkeypatch.setattr(settings, "reddit_feeds_api_url", "http://rf.local")
    oversized = b'{"pad": "' + b"x" * (2 * 1024 * 1024) + b'"}'
    mock_http.get("http://rf.local/status").mock(
        return_value=httpx.Response(200, content=oversized, headers={"content-type": "application/json"})
    )
    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Reddit Feeds API body too large"


async def test_reddit_feeds_status_bounds_the_whole_exchange(
    client: AsyncClient, mock_http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """httpx's timeout is the gap between reads, so a companion that trickles
    one byte at a time never trips it and never reaches MAX_STATUS_BYTES
    either — the handler runs until the client goes away (M4)."""
    import asyncio

    from src.config import settings

    monkeypatch.setattr(settings, "reddit_feeds_api_url", "http://rf.local")
    monkeypatch.setattr("src.api.reddit_feeds.STATUS_TIMEOUT_S", 0.2)

    async def _trickle() -> AsyncGenerator[bytes]:
        for _ in range(100):
            await asyncio.sleep(0.05)
            yield b"x"

    mock_http.get("http://rf.local/status").mock(return_value=httpx.Response(200, content=_trickle()))

    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 502


async def test_reddit_feeds_status_still_passes_a_normal_body_through(
    client: AsyncClient, mock_http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.config import settings

    monkeypatch.setattr(settings, "reddit_feeds_api_url", "http://rf.local")
    mock_http.get("http://rf.local/status").mock(
        return_value=httpx.Response(200, json={"feeds": []}, headers={"content-type": "application/json"})
    )
    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 200
    assert resp.json() == {"feeds": []}


async def test_reddit_feeds_status_caps_a_body_that_arrives_in_chunks(
    client: AsyncClient, mock_http: respx.MockRouter, reddit_api_url: str
) -> None:
    """The existing cap test hands respx one 2 MiB blob, so `size += len(chunk)`
    and `size = len(chunk)` are indistinguishable — the accumulation across
    chunks, which is what the trickling case needs, was never exercised."""
    from src.api.reddit_feeds import MAX_STATUS_BYTES

    chunk = b"x" * (MAX_STATUS_BYTES // 4)

    async def _chunks() -> AsyncGenerator[bytes]:
        for _ in range(6):
            yield chunk

    mock_http.get(f"{reddit_api_url}/status").mock(return_value=httpx.Response(200, content=_chunks()))
    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 502


async def test_reddit_feeds_status_allows_a_body_of_exactly_the_cap(
    client: AsyncClient, mock_http: respx.MockRouter, reddit_api_url: str
) -> None:
    """The guard is `>` not `>=`, so exactly MAX_STATUS_BYTES must pass."""
    from src.api.reddit_feeds import MAX_STATUS_BYTES

    body = b'{"pad":"' + b"x" * (MAX_STATUS_BYTES - 10) + b'"}'
    assert len(body) == MAX_STATUS_BYTES
    mock_http.get(f"{reddit_api_url}/status").mock(return_value=httpx.Response(200, content=body))
    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 200


async def test_reddit_feeds_status_reports_an_empty_body_accurately(
    client: AsyncClient, mock_http: respx.MockRouter, reddit_api_url: str
) -> None:
    """A companion answering 204, or 200 with no body, is not `non-JSON`."""
    mock_http.get(f"{reddit_api_url}/status").mock(return_value=httpx.Response(200, content=b""))
    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 502
    assert "empty" in resp.json()["detail"].lower()
