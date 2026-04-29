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
