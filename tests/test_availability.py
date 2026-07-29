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
