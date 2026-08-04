import logging
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
        params={
            "after_feed_id": last["feed_id"],
            "after_pub_date": last["pub_date"],
            "after_id": last["id"],
            "size": 2,
        },
    )
    assert resp2.status_code == 200
    assert [i["id"] for i in resp2.json()] == ["item3"]


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
        params={
            "unseen": "true",
            "after_feed_id": last["feed_id"],
            "after_pub_date": last["pub_date"],
            "after_id": last["id"],
            "size": 2,
        },
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


async def test_mark_seen_not_found(client: AsyncClient) -> None:
    resp = await client.post("/api/items/nonexistent/seen")
    assert resp.status_code == 404


async def test_mark_seen_after_prune_returns_clean_shape(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """F20: the old trailing SELECT could be None if the row was pruned between
    UPDATE and SELECT, raising on seen_row[0]. RETURNING reads the row in the
    same statement, so there is no second window to race."""
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1", seen_at=None)
    resp = await client.post("/api/items/item1/seen")
    assert resp.status_code == 200
    assert "seen_at" in resp.json()


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
    # R8: nosniff is on both paths now, not just the miss path.
    assert resp.headers["x-content-type-options"] == "nosniff"


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
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
        resp = await client.get(f"/api/media/proxy?url={url}")
        await real_client.aclose()

    assert resp.status_code == 200
    assert resp.content == b"freshdata"
    # Sidecar must have been written so a subsequent cache hit serves the
    # correct Content-Type.
    import hashlib

    fname = hashlib.sha256(url.encode()).hexdigest()
    assert (tmp_path / f"{fname}.meta").read_text() == "image/jpeg"  # type: ignore[operator]


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
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
        resp = await client.get(f"/api/media/proxy?url={url}")
        await real_client.aclose()
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
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
        resp = await client.get(f"/api/media/proxy?url={url}")
        await real_client.aclose()
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
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
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
        resp = await client.get(f"/api/media/proxy?url={url}")
        await real_client.aclose()
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-type"].startswith("application/octet-stream")


async def test_proxy_upstream_error(
    client: AsyncClient,
    tmp_path: object,
    monkeypatch: object,
    db: aiosqlite.Connection,
) -> None:
    import httpx
    import respx

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/broken.jpg"
    await _register_proxy_url(db, url)

    with respx.mock:
        respx.get(_pinned(url)).mock(return_value=httpx.Response(404))
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
        resp = await client.get(f"/api/media/proxy?url={url}")
        await real_client.aclose()

    assert resp.status_code == 502
    async with db.execute("SELECT url FROM dead_urls") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == [url]


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
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
        resp = await client.get(f"/api/media/proxy?url={url}&item_id={item_id}")
        await real_client.aclose()

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
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
        resp = await client.get(f"/api/media/proxy?url={url}")
        await real_client.aclose()

    assert resp.status_code == 502
    async with db.execute("SELECT url FROM dead_urls") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == [url]


# ---------------------------------------------------------------------------
# POST /api/prefetch/hint tests
# ---------------------------------------------------------------------------


async def test_prefetch_hint(client: AsyncClient, db: aiosqlite.Connection, monkeypatch: object) -> None:
    import httpx

    import src.api.media as media_mod

    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/feed', 'F')")
    await db.execute(
        "INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date) "
        "VALUES ('i1', 'f1', 'g1', 'T', 'http://x.com/img.jpg', 'image', datetime('now'))"
    )
    await db.commit()

    monkeypatch.setattr(media_mod, "get_http_client", lambda: httpx.AsyncClient())
    resp = await client.post("/api/prefetch/hint", json={"item_id": "i1"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_prefetch_hint_missing_item_id(client: AsyncClient) -> None:
    resp = await client.post("/api/prefetch/hint", json={})
    assert resp.status_code == 422


async def test_prefetch_hint_unknown_item_404(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """F16: a typo'd item_id must be 404, not indistinguishable from ok."""
    resp = await client.post("/api/prefetch/hint", json={"item_id": "nonexistent"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/reddit-feeds/status tests
# ---------------------------------------------------------------------------


async def test_reddit_feeds_status_success(
    client: AsyncClient, mock_http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
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
    mock_http.get("http://127.0.0.1:9090/status").mock(return_value=httpx.Response(200, json=upstream_json))
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
    resp = await client.get("/api/reddit-feeds/status")
    await real_client.aclose()
    assert resp.status_code == 200
    assert resp.json() == upstream_json


async def test_reddit_feeds_status_unreachable(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.api.reddit_feeds as rf_mod

    fake_client = httpx.AsyncClient()

    async def fake_get(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    fake_client.get = fake_get  # type: ignore[method-assign]
    monkeypatch.setattr(rf_mod, "get_http_client", lambda: fake_client)

    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 502


async def test_reddit_feeds_status_upstream_error(
    client: AsyncClient, mock_http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_http.get("http://127.0.0.1:9090/status").mock(return_value=httpx.Response(500))
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
    resp = await client.get("/api/reddit-feeds/status")
    await real_client.aclose()
    assert resp.status_code == 502


async def test_reddit_feeds_status_redirects_become_502(
    client: AsyncClient,
    mock_http: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 6: a 301 from the upstream must surface as 502 — the trusted URL
    must not be silently rewritten by an attacker-controlled Location header.
    follow_redirects=False is the contract (was True; F10/R13 fixed it then,
    Task 6 closed it for security)."""
    mock_http.get("http://127.0.0.1:9090/status").mock(
        return_value=httpx.Response(301, headers={"location": "http://127.0.0.1:9090/v2/status"})
    )
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
    resp = await client.get("/api/reddit-feeds/status")
    await real_client.aclose()
    assert resp.status_code == 502


async def test_reddit_feeds_status_has_nosniff(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 6: proxied JSON must always carry X-Content-Type-Options: nosniff."""
    from src.config import settings

    monkeypatch.setattr(settings, "reddit_feeds_api_url", "http://rf.local")
    with respx.mock:
        respx.get("http://rf.local/status").mock(
            return_value=httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})
        )
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
        resp = await client.get("/api/reddit-feeds/status")
        await real_client.aclose()
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_reddit_feeds_status_redirect_is_502(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 6: a 3xx from upstream must surface as 502, never be silently
    proxied through a rewritten Location header."""
    from src.config import settings

    monkeypatch.setattr(settings, "reddit_feeds_api_url", "http://rf.local")
    with respx.mock:
        respx.get("http://rf.local/status").mock(
            return_value=httpx.Response(301, headers={"location": "http://elsewhere/status"})
        )
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
        resp = await client.get("/api/reddit-feeds/status")
        await real_client.aclose()
    assert resp.status_code == 502


async def test_reddit_feeds_status_401_maps_to_502(
    client: AsyncClient,
    mock_http: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F10: upstream 401 must not read as a failure of OUR session."""
    mock_http.get("http://127.0.0.1:9090/status").mock(return_value=httpx.Response(401))
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
    resp = await client.get("/api/reddit-feeds/status")
    await real_client.aclose()
    assert resp.status_code == 502


async def test_reddit_feeds_status_non_json_body(
    client: AsyncClient,
    mock_http: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F10: a 200 with a non-JSON body must 502, not 500 with JSONDecodeError."""
    mock_http.get("http://127.0.0.1:9090/status").mock(
        return_value=httpx.Response(200, content=b"not json", headers={"content-type": "text/plain"})
    )
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
    resp = await client.get("/api/reddit-feeds/status")
    await real_client.aclose()
    assert resp.status_code == 502


async def test_reddit_feeds_status_pending_status(
    client: AsyncClient, mock_http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream_json = {
        "feeds": [
            {
                "name": "EarthPorn",
                "last_status": "pending",
                "last_fetch": "2026-07-27T14:02:00.123456+00:00",
                "last_item_count": 5,
                "total_items": 42,
            },
            {
                "name": "Python",
                "last_status": "success",
                "last_fetch": "2026-07-27T14:02:02.123456+00:00",
                "last_item_count": 3,
                "total_items": 18,
            },
        ],
        "last_run": "2026-07-27T14:02:05.654321+00:00",
    }
    mock_http.get("http://127.0.0.1:9090/status").mock(return_value=httpx.Response(200, json=upstream_json))
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
    resp = await client.get("/api/reddit-feeds/status")
    await real_client.aclose()
    assert resp.status_code == 200
    assert resp.json() == upstream_json


@pytest.mark.asyncio
async def test_items_interleaved_across_feeds(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """Items from multiple feeds should be interleaved round-robin, oldest first."""
    import datetime
    import hashlib  # noqa: F401

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
    assert [item["rn"] for item in data] == [1, 1, 2, 2, 3]


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


async def test_items_cached_excludes_meta_and_tmp(
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
    (tmp_path / "abc123.tmp").write_bytes(b"partial")  # in-flight, must not count
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
    assert [i["rn"] for i in data] == [1, 2, 3]


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
        params={
            "after_feed_id": last["feed_id"],
            "after_pub_date": last["pub_date"],
            "after_id": last["id"],
            "size": 4,
        },
    )
    assert [i["id"] for i in page2.json()] == ["item5", "item6", "item7", "item8"]


async def test_items_partial_cursor_rejected(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """R3: a partial cursor must 422, not be silently dropped back to page 1."""
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1")
    for params in (
        {"after_feed_id": "feed1"},
        {"after_id": "item1"},
        {"after_feed_id": "feed1", "after_id": "item1"},
        {"after_pub_date": "2026-01-01T00:00:00", "after_id": "item1"},
    ):
        resp = await client.get("/api/items", params=params)
        assert resp.status_code == 422, f"{params} should be rejected"


async def test_proxy_cache_hit_evicted_before_send_refetches(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_http: respx.MockRouter,
    db: aiosqlite.Connection,
) -> None:
    """R2: cache_read only stats for existence; FileResponse opens the file
    after the handler returned. evict() runs after every refresh cycle while
    the browser pulls several proxy requests, and losing that race raised
    RuntimeError -> 500 for media the miss path would have refetched."""
    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/gone.jpg"
    await _register_proxy_url(db, url)
    # cache_read says hit; the file is already gone by the time we serve it.
    monkeypatch.setattr("src.api.media.cache_read", lambda _url: tmp_path / "evicted")

    mock_http.get(_pinned(url)).mock(
        return_value=httpx.Response(200, content=b"refetched", headers={"content-type": "image/jpeg"})
    )
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
    resp = await client.get(f"/api/media/proxy?url={url}")
    await real_client.aclose()

    assert resp.status_code == 200
    assert resp.content == b"refetched"


async def test_proxy_cache_hit_sets_nosniff(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db: aiosqlite.Connection
) -> None:
    """R8: the miss path set nosniff and the hit path set no security headers,
    so the same bytes were served differently on first and second view — and
    the cached copy is the one served repeatedly."""
    import hashlib

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/img.jpg"
    await _register_proxy_url(db, url)
    filename = hashlib.sha256(url.encode()).hexdigest()
    (tmp_path / filename).write_bytes(b"cached")
    (tmp_path / f"{filename}.meta").write_text("image/jpeg")

    resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"


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
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)

    with caplog.at_level(logging.WARNING, logger="src.api.media"):
        resp = await client.get(f"/api/media/proxy?url={url}&item_id=i1")
    await real_client.aclose()

    assert resp.status_code == 502
    assert any("i1" in m and url in m for m in caplog.messages)


async def test_prefetch_hint_logs_entry_and_queue_size(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr("src.api.media.get_http_client", lambda: httpx.AsyncClient())

    with caplog.at_level(logging.DEBUG, logger="src.api.media"):
        resp = await client.post("/api/prefetch/hint", json={"item_id": "i1"})

    assert resp.status_code == 200
    assert any("i1" in m for m in caplog.messages)
    assert any("queued" in m for m in caplog.messages)


async def test_reddit_feeds_status_passes_through_json_array(
    client: AsyncClient, mock_http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: FastAPI derives a response model from the `-> dict` annotation and
    validates after the handler returns, outside the try. A companion answering
    200 with [] became a 500 — the opposite of dadd0d6's whole purpose."""
    mock_http.get("http://127.0.0.1:9090/status").mock(return_value=httpx.Response(200, json=[]))
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
    resp = await client.get("/api/reddit-feeds/status")
    await real_client.aclose()

    assert resp.status_code == 200
    assert resp.json() == []


async def test_reddit_feeds_status_non_200_success_is_not_502(
    client: AsyncClient, mock_http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 202 from the companion is a success, not an error."""
    mock_http.get("http://127.0.0.1:9090/status").mock(return_value=httpx.Response(202, json={"ok": True}))
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
    resp = await client.get("/api/reddit-feeds/status")
    await real_client.aclose()

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_reddit_feeds_status_logs_the_exception(
    client: AsyncClient,
    mock_http: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R11: httpx timeouts frequently stringify to empty, degrading the line to
    'reddit_feeds_status unreachable:' with no exception type and no traceback."""
    mock_http.get("http://127.0.0.1:9090/status").mock(side_effect=httpx.ConnectTimeout(""))
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)

    with caplog.at_level(logging.WARNING, logger="src.api.reddit_feeds"):
        resp = await client.get("/api/reddit-feeds/status")
    await real_client.aclose()

    assert resp.status_code == 502
    record = next(r for r in caplog.records if r.name == "src.api.reddit_feeds")
    assert "ConnectTimeout" in record.getMessage()
    assert record.exc_info is not None


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
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
    resp = await client.get(f"/api/media/proxy?url={url}", headers={"Range": "bytes=2-5"})
    await real_client.aclose()

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
