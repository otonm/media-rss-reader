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
