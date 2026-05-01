import datetime
import hashlib
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import httpx
import pytest
import respx

from src.feeds.sync import opml_sync, prune_items, refresh_all_feeds

_OPML = """\
<?xml version="1.0"?>
<opml version="2.0"><head/><body>
  <outline type="rss" text="Feed" xmlUrl="https://example.com/feed.xml"/>
</body></opml>"""

_RSS = """\
<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
  <item>
    <guid>g1</guid>
    <enclosure url="https://example.com/img.jpg" type="image/jpeg" length="0"/>
  </item>
</channel></rss>"""


async def test_opml_sync_inserts_new_feeds(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_OPML)
    async with httpx.AsyncClient() as client:
        await opml_sync(db, str(f), client)
    async with db.execute("SELECT COUNT(*) FROM feeds") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_opml_sync_is_idempotent(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_OPML)
    async with httpx.AsyncClient() as client:
        await opml_sync(db, str(f), client)
        await opml_sync(db, str(f), client)
    async with db.execute("SELECT COUNT(*) FROM feeds") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_opml_sync_removes_deleted_feeds(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_OPML)
    async with httpx.AsyncClient() as client:
        await opml_sync(db, str(f), client)
    f.write_text(
        '<?xml version="1.0"?><opml version="2.0"><head/><body/></opml>'
    )
    async with httpx.AsyncClient() as client:
        await opml_sync(db, str(f), client)
    async with db.execute("SELECT COUNT(*) FROM feeds") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_refresh_all_feeds_inserts_items(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_OPML)
    with respx.mock:
        respx.get("https://example.com/feed.xml").mock(
            return_value=httpx.Response(200, text=_RSS)
        )
        async with httpx.AsyncClient() as client:
            await opml_sync(db, str(f), client)
            await refresh_all_feeds(db, client)
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_refresh_all_feeds_deduplicates(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_OPML)
    with respx.mock:
        respx.get("https://example.com/feed.xml").mock(
            return_value=httpx.Response(200, text=_RSS)
        )
        async with httpx.AsyncClient() as client:
            await opml_sync(db, str(f), client)
            await refresh_all_feeds(db, client)
        respx.get("https://example.com/feed.xml").mock(
            return_value=httpx.Response(200, text=_RSS)
        )
        async with httpx.AsyncClient() as client:
            await refresh_all_feeds(db, client)
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 1


def _sqlite_dt(dt: datetime.datetime) -> str:
    """Format datetime as SQLite-compatible string (space separator, no microseconds)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def _insert_item(
    db: aiosqlite.Connection, feed_id: str, guid: str, seen: bool = False, hours_ago: int = 0
) -> str:
    item_id = hashlib.sha256((feed_id + guid).encode()).hexdigest()
    fetched = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours_ago)
    seen_at = _sqlite_dt(datetime.datetime.now(datetime.UTC)) if seen else None
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, title, media_url, media_type, pub_date, fetched_at, seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, feed_id, guid, "t", "http://x.com/a.jpg", "image",
         _sqlite_dt(fetched), _sqlite_dt(fetched), seen_at),
    )
    await db.commit()
    return item_id


@pytest.mark.asyncio
async def test_prune_deletes_old_seen_items(db: aiosqlite.Connection) -> None:
    feed_id = "feed1"
    await db.execute("INSERT INTO feeds (id, url) VALUES (?, ?)", (feed_id, "http://f1.com"))
    await db.commit()
    await _insert_item(db, feed_id, "old1", seen=True, hours_ago=200)
    await _insert_item(db, feed_id, "old2", seen=True, hours_ago=180)
    recent_id = await _insert_item(db, feed_id, "recent", seen=True, hours_ago=1)

    with patch("src.feeds.sync.settings") as mock_settings:
        mock_settings.items_max_age_hours = 168
        mock_settings.keep_items = 1000
        await prune_items(db)

    async with db.execute("SELECT id FROM items") as cur:
        rows = await cur.fetchall()
    ids = [r[0] for r in rows]
    assert recent_id in ids
    assert len(ids) == 1


@pytest.mark.asyncio
async def test_prune_seen_before_unseen_when_over_limit(db: aiosqlite.Connection) -> None:
    feed_id = "feed1"
    await db.execute("INSERT INTO feeds (id, url) VALUES (?, ?)", (feed_id, "http://f1.com"))
    await db.commit()
    seen_ids = []
    for i in range(3):
        sid = await _insert_item(db, feed_id, f"seen{i}", seen=True, hours_ago=10 - i)
        seen_ids.append(sid)
    unseen_ids = []
    for i in range(3):
        uid = await _insert_item(db, feed_id, f"unseen{i}", seen=False, hours_ago=5 - i)
        unseen_ids.append(uid)

    with patch("src.feeds.sync.settings") as mock_settings:
        mock_settings.items_max_age_hours = 9999
        mock_settings.keep_items = 4
        await prune_items(db)

    async with db.execute("SELECT id FROM items") as cur:
        rows = await cur.fetchall()
    ids = {r[0] for r in rows}
    assert len(ids) == 4
    for uid in unseen_ids:
        assert uid in ids
    assert seen_ids[2] in ids


@pytest.mark.asyncio
async def test_prune_unseen_when_over_limit_after_seen_exhausted(db: aiosqlite.Connection) -> None:
    feed_id = "feed1"
    await db.execute("INSERT INTO feeds (id, url) VALUES (?, ?)", (feed_id, "http://f1.com"))
    await db.commit()
    ids = []
    for i in range(5):
        uid = await _insert_item(db, feed_id, f"u{i}", seen=False, hours_ago=10 - i)
        ids.append(uid)

    with patch("src.feeds.sync.settings") as mock_settings:
        mock_settings.items_max_age_hours = 9999
        mock_settings.keep_items = 3
        await prune_items(db)

    async with db.execute("SELECT id FROM items") as cur:
        rows = await cur.fetchall()
    remaining = {r[0] for r in rows}
    assert len(remaining) == 3
    for uid in ids[2:]:
        assert uid in remaining
