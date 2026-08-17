import contextlib
import json
import logging

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
    dropped = await mark_url_dead_and_maybe_drop("http://x.com/a.jpg", item_id="i1", db=db)
    assert dropped == ["i1"]
    async with db.execute("SELECT id FROM items") as cur:
        rows = await cur.fetchall()
    assert rows == []
    async with db.execute("SELECT guid FROM resolved_guids WHERE feed_id = ?", ("f1",)) as cur:
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
    await _insert_item(db, "i1", "f1", "g1", "http://x.com/a.jpg", media_json=media_json)
    dropped = await mark_url_dead_and_maybe_drop("http://x.com/a.jpg", item_id="i1", db=db)
    assert dropped == []
    async with db.execute("SELECT id FROM items") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["i1"]
    async with db.execute("SELECT COUNT(*) FROM resolved_guids") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_gallery_all_404_drops_item(db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    media_json = json.dumps(
        [
            {"url": "http://x.com/a.jpg", "type": "image"},
            {"url": "http://x.com/b.jpg", "type": "image"},
        ]
    )
    await _insert_item(db, "i1", "f1", "g1", "http://x.com/a.jpg", media_json=media_json)
    await mark_url_dead_and_maybe_drop("http://x.com/a.jpg", item_id="i1", db=db)
    dropped = await mark_url_dead_and_maybe_drop("http://x.com/b.jpg", item_id="i1", db=db)
    assert dropped == ["i1"]
    async with db.execute("SELECT id FROM items") as cur:
        rows = await cur.fetchall()
    assert rows == []
    async with db.execute("SELECT guid FROM resolved_guids WHERE feed_id = ?", ("f1",)) as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["g1"]


async def test_url_shared_by_two_items_drops_both(db: aiosqlite.Connection) -> None:
    await _insert_feed(db, "f1")
    await _insert_feed(db, "f2")
    await _insert_item(db, "i1", "f1", "g1", "http://x.com/shared.jpg")
    await _insert_item(db, "i2", "f2", "g2", "http://x.com/shared.jpg")
    dropped = await mark_url_dead_and_maybe_drop("http://x.com/shared.jpg", item_id="i1", db=db)
    assert sorted(dropped) == ["i1", "i2"]
    async with db.execute("SELECT id FROM items ORDER BY id") as cur:
        rows = await cur.fetchall()
    assert rows == []
    async with db.execute("SELECT feed_id, guid FROM resolved_guids ORDER BY feed_id") as cur:
        rows = await cur.fetchall()
    assert [tuple(r) for r in rows] == [("f1", "g1"), ("f2", "g2")]


async def test_no_item_id_drops_via_media_url_lookup(db: aiosqlite.Connection) -> None:
    """Fallback path: callers without item_id scan by media_url."""
    await _insert_feed(db)
    await _insert_item(db, "i1", "f1", "g1", "http://x.com/a.jpg")
    dropped = await mark_url_dead_and_maybe_drop("http://x.com/a.jpg", item_id=None, db=db)
    assert dropped == ["i1"]
    async with db.execute("SELECT id FROM items") as cur:
        assert await cur.fetchone() is None


async def test_repeated_calls_are_idempotent(db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "i1", "f1", "g1", "http://x.com/a.jpg")
    first = await mark_url_dead_and_maybe_drop("http://x.com/a.jpg", item_id="i1", db=db)
    second = await mark_url_dead_and_maybe_drop("http://x.com/a.jpg", item_id="i1", db=db)
    assert first == ["i1"]
    assert second == []
    async with db.execute("SELECT url FROM dead_urls") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["http://x.com/a.jpg"]


async def test_mark_dead_rolls_back_on_failure(db: aiosqlite.Connection) -> None:
    """A failure part-way through must leave dead_urls unchanged.

    The callee no longer commits, so write_transaction's rollback is the only
    thing standing between a mid-flight failure and a half-applied delete.
    """
    from src.db.connection import write_transaction

    url = "https://example.com/a.jpg"
    with contextlib.suppress(RuntimeError):
        async with write_transaction(db):
            await mark_url_dead_and_maybe_drop(url, None, db)
            raise RuntimeError("boom")

    async with db.execute("SELECT COUNT(*) FROM dead_urls WHERE url = ?", (url,)) as cur:
        assert (await cur.fetchone())[0] == 0


async def test_unknown_item_id_marks_dead_only(db: aiosqlite.Connection) -> None:
    """If item_id doesn't exist, mark the URL dead but don't crash."""
    await _insert_feed(db)
    dropped = await mark_url_dead_and_maybe_drop("http://x.com/a.jpg", item_id="nonexistent", db=db)
    assert dropped == []
    async with db.execute("SELECT url FROM dead_urls") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["http://x.com/a.jpg"]


async def test_dropped_item_log_escapes_a_hostile_guid(
    db: aiosqlite.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """A feed is a trust boundary too: a guid with an embedded newline must not
    forge a second log line (minor 6)."""
    hostile_guid = "g1\nERROR fake injected line"
    await _insert_feed(db)
    await _insert_item(db, "i1", "f1", hostile_guid, "http://x.com/a.jpg")

    with caplog.at_level(logging.DEBUG, logger="src.media.availability"):
        dropped = await mark_url_dead_and_maybe_drop("http://x.com/a.jpg", item_id="i1", db=db)

    assert dropped == ["i1"]
    record = next(m for m in caplog.messages if "dropped item" in m)
    assert "\n" not in record
    assert repr(hostile_guid) in record


async def test_is_known_media_url_matches_a_non_ascii_gallery_slide(db: aiosqlite.Connection) -> None:
    """media_json is written by json.dumps with ensure_ascii=True, so `é` is
    stored as `\\u00e9`, while the LIKE prefilter was built from the raw request
    value. The two could never match, so every gallery slide with a non-ASCII
    character 404'd forever (M3)."""
    import json

    from src.media.availability import is_known_media_url

    slide = "http://example.com/café.jpg"
    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://f', 'F')")
    await db.execute(
        """INSERT INTO items(id, feed_id, guid, media_url, media_type, media_json)
           VALUES ('i1', 'f1', 'g1', 'http://example.com/first.jpg', 'image', ?)""",
        (json.dumps([{"url": "http://example.com/first.jpg", "type": "image"}, {"url": slide, "type": "image"}]),),
    )
    await db.commit()

    assert await is_known_media_url(slide, db) is True


async def test_mark_dead_ignores_item_that_lacks_the_url(db: aiosqlite.Connection) -> None:
    """R5: item_id is looked up on its own, so a caller could name any item id
    alongside any URL and have that item deleted."""
    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/feed', 'F')")
    await db.execute(
        """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date)
           VALUES ('victim', 'f1', 'g1', 'T', 'http://example.com/mine.jpg', 'image', '2026-01-01T00:00:00')"""
    )
    await db.commit()
    # Its own URL is already dead, which is what used to make the cross-item
    # deletion reachable.
    await db.execute("INSERT INTO dead_urls (url) VALUES ('http://example.com/mine.jpg')")
    await db.commit()

    dropped = await mark_url_dead_and_maybe_drop("http://example.com/other.jpg", "victim", db)

    assert dropped == []
    async with db.execute("SELECT COUNT(*) FROM items WHERE id = 'victim'") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_dropped_item_is_not_reinserted_by_next_poll(db: aiosqlite.Connection) -> None:
    """The merged tombstone must still block re-insert on the next feed poll."""
    from src.db.connection import write_transaction
    from src.feeds.sync import _skip_guids

    await db.execute("INSERT INTO feeds (id, url) VALUES ('f1', 'https://e.com/f')")
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type)"
        " VALUES ('i1', 'f1', 'g1', 'https://e.com/a.jpg', 'image')"
    )
    await db.commit()

    async with write_transaction(db):
        dropped = await mark_url_dead_and_maybe_drop("https://e.com/a.jpg", "i1", db)
    assert dropped == ["i1"]

    assert "g1" in await _skip_guids(db, "f1"), "the dropped guid must be in the skip set"
