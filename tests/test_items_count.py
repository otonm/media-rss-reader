import aiosqlite
from httpx import AsyncClient


async def _insert_feed(db: aiosqlite.Connection, feed_id: str = "feed1") -> None:
    await db.execute(
        "INSERT INTO feeds(id, url, title) VALUES (?, ?, ?)",
        (feed_id, f"http://example.com/{feed_id}.xml", "Test Feed"),
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
        (item_id, feed_id, item_id, "Title", f"http://example.com/{item_id}.jpg", "image", seen_at),
    )
    await db.commit()


async def test_count_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/items/count")
    assert resp.status_code == 200
    assert resp.json() == {"count": 0}


async def test_count_unseen_default(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """unseen defaults to true, so seen items are excluded."""
    await _insert_feed(db)
    await _insert_item(db, "seen_item", "feed1", seen_at="2024-01-01T00:00:00")
    await _insert_item(db, "unseen_item_1", "feed1", seen_at=None)
    await _insert_item(db, "unseen_item_2", "feed1", seen_at=None)
    resp = await client.get("/api/items/count")
    assert resp.status_code == 200
    assert resp.json() == {"count": 2}


async def test_count_unseen_true(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "seen_item", "feed1", seen_at="2024-01-01T00:00:00")
    await _insert_item(db, "unseen_item", "feed1", seen_at=None)
    resp = await client.get("/api/items/count", params={"unseen": "true"})
    assert resp.status_code == 200
    assert resp.json() == {"count": 1}


async def test_count_unseen_false(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "seen_item", "feed1", seen_at="2024-01-01T00:00:00")
    await _insert_item(db, "unseen_item", "feed1", seen_at=None)
    resp = await client.get("/api/items/count", params={"unseen": "false"})
    assert resp.status_code == 200
    assert resp.json() == {"count": 2}


async def test_count_feed_filter(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db, feed_id="feedA")
    await _insert_feed(db, feed_id="feedB")
    await _insert_item(db, "itemA", "feedA")
    await _insert_item(db, "itemB", "feedB")
    resp = await client.get("/api/items/count", params={"feed_id": "feedA"})
    assert resp.status_code == 200
    assert resp.json() == {"count": 1}
