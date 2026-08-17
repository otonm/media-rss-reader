import datetime
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import httpx
import pytest
import respx
from httpx import AsyncClient as HttpxAsyncClient

from src.feeds.sync import prune_items, refresh_all_feeds, sync_feeds

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


async def test_refresh_all_feeds_inserts_items(db: aiosqlite.Connection, tmp_path: Path) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_OPML)
    with respx.mock:
        respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS))
        async with httpx.AsyncClient() as client:
            await sync_feeds(db, str(tmp_path), str(f), client)
            await refresh_all_feeds(db, client)
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_refresh_all_feeds_deduplicates(db: aiosqlite.Connection, tmp_path: Path) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_OPML)
    with respx.mock:
        respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS))
        async with httpx.AsyncClient() as client:
            await sync_feeds(db, str(tmp_path), str(f), client)
            await refresh_all_feeds(db, client)
        respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS))
        async with httpx.AsyncClient() as client:
            await refresh_all_feeds(db, client)
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 1


_GALLERY_RSS = """\
<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
  <item>
    <guid>g-gallery</guid>
    <enclosure url="https://example.com/a.jpg" type="image/jpeg" length="0"/>
    <enclosure url="https://example.com/b.gif" type="image/gif" length="0"/>
  </item>
</channel></rss>"""


async def test_refresh_all_feeds_stores_media_json(db: aiosqlite.Connection, tmp_path: Path) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_OPML)
    with respx.mock:
        respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=_GALLERY_RSS))
        async with httpx.AsyncClient() as client:
            await sync_feeds(db, str(tmp_path), str(f), client)
            await refresh_all_feeds(db, client)
    async with db.execute("SELECT media_url, media_type, media_json FROM items") as cur:
        row = await cur.fetchone()
    assert row["media_url"] == "https://example.com/a.jpg"
    assert row["media_type"] == "image"
    assert json.loads(row["media_json"]) == [
        {"url": "https://example.com/a.jpg", "type": "image"},
        {"url": "https://example.com/b.gif", "type": "gif"},
    ]


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
        (item_id, feed_id, guid, "t", "http://x.com/a.jpg", "image", _sqlite_dt(fetched), _sqlite_dt(fetched), seen_at),
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


async def test_refresh_skips_resolved_guids(db: aiosqlite.Connection, tmp_path: Path) -> None:
    """Items whose (feed_id, guid) is in resolved_guids must not be
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
        "INSERT INTO resolved_guids (feed_id, guid) VALUES (?, ?)",
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
    async with db.execute("SELECT guid FROM resolved_guids WHERE feed_id = ?", (feed_id,)) as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["g1"]


_TWO_FEED_OPML = """\
<?xml version="1.0"?>
<opml version="2.0"><head/><body>
  <outline type="rss" text="A" xmlUrl="https://a.example.com/feed.xml"/>
  <outline type="rss" text="B" xmlUrl="https://b.example.com/feed.xml"/>
</body></opml>"""

# Same picture, two feeds, different guids, and the second feed hands us the
# URL with an extra sizing query string — the shape that produced visible
# duplicates before media_key existed.
_RSS_FEED_A = """\
<?xml version="1.0"?>
<rss version="2.0"><channel><title>A</title>
  <item>
    <guid>a-1</guid>
    <enclosure url="https://cdn.example.com/shared.jpg" type="image/jpeg" length="0"/>
  </item>
</channel></rss>"""

_RSS_FEED_B = """\
<?xml version="1.0"?>
<rss version="2.0"><channel><title>B</title>
  <item>
    <guid>b-1</guid>
    <enclosure url="https://cdn.example.com/shared.jpg?width=640&amp;s=abc" type="image/jpeg" length="0"/>
  </item>
</channel></rss>"""


async def test_refresh_deduplicates_same_media_across_feeds(db: aiosqlite.Connection, tmp_path: Path) -> None:
    """The same picture carried by two different feeds is stored once."""
    f = tmp_path / "feeds.opml"
    f.write_text(_TWO_FEED_OPML)
    with respx.mock:
        respx.get("https://a.example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS_FEED_A))
        respx.get("https://b.example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS_FEED_B))
        async with httpx.AsyncClient() as client:
            await sync_feeds(db, str(tmp_path), str(f), client)
            await refresh_all_feeds(db, client)

    async with db.execute("SELECT guid, media_key FROM items") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    # Feed A is refreshed first, so its item is the one that survives.
    assert rows[0]["guid"] == "a-1"
    assert rows[0]["media_key"] == "https://cdn.example.com/shared.jpg"


async def test_seen_blocks_cross_feed_reinsert(
    db: aiosqlite.Connection, client: HttpxAsyncClient, tmp_path: Path
) -> None:
    """Once a picture is seen, the copy another feed carries must not resurrect it.

    The cross-feed guard only rejects an incoming item while a row with the
    same media_key still exists. Prune that row and feed B's copy — a different
    (feed_id, guid) — was free to insert itself as brand-new unseen.
    """
    f = tmp_path / "feeds.opml"
    f.write_text(_TWO_FEED_OPML)
    with respx.mock:
        respx.get("https://a.example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS_FEED_A))
        respx.get("https://b.example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS_FEED_B))
        async with httpx.AsyncClient() as hc:
            await sync_feeds(db, str(tmp_path), str(f), hc)
            await refresh_all_feeds(db, hc)

        async with db.execute("SELECT id, guid FROM items") as cur:
            rows = await cur.fetchall()
        assert [r["guid"] for r in rows] == ["a-1"]
        assert (await client.post(f"/api/items/{rows[0]['id']}/seen")).status_code == 200

        # State after prune_items evicted the seen row.
        await db.execute("DELETE FROM items")
        await db.commit()

        async with httpx.AsyncClient() as hc:
            await refresh_all_feeds(db, hc)

    async with db.execute("SELECT guid FROM items") as cur:
        assert [r["guid"] for r in await cur.fetchall()] == []


async def test_refresh_keeps_distinct_media_from_two_feeds(db: aiosqlite.Connection, tmp_path: Path) -> None:
    """The guard must not collapse genuinely different images."""
    f = tmp_path / "feeds.opml"
    f.write_text(_TWO_FEED_OPML)
    rss_b = _RSS_FEED_B.replace("shared.jpg", "different.jpg")
    with respx.mock:
        respx.get("https://a.example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS_FEED_A))
        respx.get("https://b.example.com/feed.xml").mock(return_value=httpx.Response(200, text=rss_b))
        async with httpx.AsyncClient() as client:
            await sync_feeds(db, str(tmp_path), str(f), client)
            await refresh_all_feeds(db, client)

    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 2
