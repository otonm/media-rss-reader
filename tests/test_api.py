import aiosqlite
from httpx import AsyncClient


async def test_feeds_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/feeds")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_feeds_returns_feed_with_counts(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    await db.execute(
        "INSERT INTO feeds (id, url, title) VALUES ('f1', 'http://x.com', 'X')"
    )
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


async def test_items_feed_filter(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db, feed_id="feedA", url="http://a.com/feed.xml")
    await _insert_feed(db, feed_id="feedB", url="http://b.com/feed.xml")
    await _insert_item(db, "itemA", "feedA")
    await _insert_item(db, "itemB", "feedB")
    resp = await client.get("/api/items", params={"feed_id": "feedA"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "itemA"


async def test_items_pagination(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1")
    await _insert_item(db, "item2", "feed1")
    await _insert_item(db, "item3", "feed1")
    resp_page0 = await client.get("/api/items", params={"page": 0, "size": 2})
    assert resp_page0.status_code == 200
    assert len(resp_page0.json()) == 2
    resp_page1 = await client.get("/api/items", params={"page": 1, "size": 2})
    assert resp_page1.status_code == 200
    assert len(resp_page1.json()) == 1


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


async def test_mark_seen_not_found(client: AsyncClient) -> None:
    resp = await client.post("/api/items/nonexistent/seen")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/media/proxy tests
# ---------------------------------------------------------------------------


async def test_proxy_cache_hit(
    client: AsyncClient, tmp_path: object, monkeypatch: object
) -> None:
    import hashlib

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/img.jpg"
    filename = hashlib.sha256(url.encode()).hexdigest()
    (tmp_path / filename).write_bytes(b"cached")  # type: ignore[operator]

    resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 200
    assert resp.content == b"cached"


async def test_proxy_cache_miss(
    client: AsyncClient, tmp_path: object, monkeypatch: object
) -> None:
    import httpx
    import respx

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/photo.jpg"

    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(
                200, content=b"freshdata", headers={"content-type": "image/jpeg"}
            )
        )
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
        resp = await client.get(f"/api/media/proxy?url={url}")
        await real_client.aclose()

    assert resp.status_code == 200
    assert resp.content == b"freshdata"


async def test_proxy_upstream_error(
    client: AsyncClient, tmp_path: object, monkeypatch: object
) -> None:
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


# ---------------------------------------------------------------------------
# POST /api/prefetch/hint tests
# ---------------------------------------------------------------------------


async def test_prefetch_hint(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: object
) -> None:
    import httpx

    import src.api.media as media_mod

    await db.execute(
        "INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/feed', 'F')"
    )
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


# ---------------------------------------------------------------------------
# GET /api/status tests
# ---------------------------------------------------------------------------


async def test_status(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: object, monkeypatch: object
) -> None:
    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    await db.execute(
        "INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/feed', 'F')"
    )
    await db.execute(
        "INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date) "
        "VALUES ('i1', 'f1', 'g1', 'T', 'http://x.com/img.jpg', 'image', datetime('now'))"
    )
    await db.commit()

    resp = await client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["feeds"] == 1
    assert data["items_total"] == 1
    assert data["items_unseen"] == 1
    assert "cache_size_mb" in data
    assert "last_opml_sync" in data


async def test_status_empty(client: AsyncClient, tmp_path: object, monkeypatch: object) -> None:
    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    resp = await client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["feeds"] == 0
    assert data["items_total"] == 0
    assert data["items_unseen"] == 0
    assert data["cache_size_mb"] == 0.0
