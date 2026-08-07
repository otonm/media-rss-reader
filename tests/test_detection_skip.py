"""Detection and re-fetch are skipped for work already done.

Media detection is a pure function of a feed entry and entries never change,
so a restart used to redo the whole thing: every feed re-fetched, re-parsed and
every entry re-detected, with the results thrown away at INSERT OR IGNORE.
These tests pin both halves of the fix — the per-entry guid skip and the
per-feed unchanged-source skip.
"""

import os
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import feedparser
import httpx
import respx

from src.db.connection import open_db
from src.db.migrations import run_migrations
from src.db.schema import create_schema
from src.feeds.fetcher import _feed_id, entry_to_item, fetch_feed
from src.feeds.sync import local_xml_sync, refresh_all_feeds, sync_feeds
from src.media.normalize import media_key

_RSS = """\
<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
  <item>
    <guid>g1</guid>
    <enclosure url="https://cdn.example.com/a.jpg" type="image/jpeg" length="0"/>
  </item>
  <item>
    <guid>g2</guid>
    <enclosure url="https://cdn.example.com/b.jpg" type="image/jpeg" length="0"/>
  </item>
</channel></rss>"""

_OPML = """\
<?xml version="1.0"?>
<opml version="2.0"><head/><body>
  <outline type="rss" text="Feed" xmlUrl="https://example.com/feed.xml"/>
</body></opml>"""

_FEED_URL = "https://example.com/feed.xml"


def test_entry_to_item_skips_known_guid_without_detecting() -> None:
    """The guid comes from entry.id, which is known before detection runs."""
    entry = {"id": "g1", "enclosures": [{"url": "https://cdn.example.com/a.jpg"}]}
    with patch("src.feeds.fetcher.detect_all_media", side_effect=AssertionError("detector must not run")):
        assert entry_to_item("feed", entry, frozenset({"g1"})) is None


def test_entry_to_item_still_detects_unknown_guid() -> None:
    entry = {"id": "g2", "enclosures": [{"url": "https://cdn.example.com/b.jpg"}]}
    item = entry_to_item("feed", entry, frozenset({"g1"}))
    assert item is not None
    assert item["media_url"] == "https://cdn.example.com/b.jpg"


async def test_local_xml_sync_does_not_redetect_stored_items(db: aiosqlite.Connection, tmp_path: Path) -> None:
    """A changed file is re-parsed, but its already-stored entries are not re-detected."""
    path = tmp_path / "feed-one.xml"
    path.write_text(_RSS)
    await local_xml_sync(db, str(tmp_path))

    # Bump the mtime so the second run gets past the unchanged-source gate and
    # has to rely on the per-entry guid skip.
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))

    with patch("src.feeds.fetcher.detect_all_media", side_effect=AssertionError("detector must not run")):
        await local_xml_sync(db, str(tmp_path))

    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 2


async def test_local_xml_sync_skips_parse_when_mtime_unchanged(db: aiosqlite.Connection, tmp_path: Path) -> None:
    (tmp_path / "feed-one.xml").write_text(_RSS)

    with patch("src.feeds.sync.feedparser.parse", wraps=feedparser.parse) as parse:
        await local_xml_sync(db, str(tmp_path))
        assert parse.call_count == 1
        await local_xml_sync(db, str(tmp_path))
        assert parse.call_count == 1, "unchanged file must not be re-parsed"


async def test_local_xml_sync_reparses_when_file_changes(db: aiosqlite.Connection, tmp_path: Path) -> None:
    path = tmp_path / "feed-one.xml"
    path.write_text(_RSS)
    await local_xml_sync(db, str(tmp_path))

    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))

    with patch("src.feeds.sync.feedparser.parse", wraps=feedparser.parse) as parse:
        await local_xml_sync(db, str(tmp_path))
    assert parse.call_count == 1


async def test_local_xml_sync_skips_tombstoned_guid(db: aiosqlite.Connection, tmp_path: Path) -> None:
    """The unavailable_guids check moved ahead of detection; it must still bite."""
    path = tmp_path / "feed-one.xml"
    path.write_text(_RSS)
    feed_id = _feed_id("feed-one.xml")
    await db.execute("INSERT INTO feeds (id, url, title) VALUES (?, ?, ?)", (feed_id, "feed-one.xml", "Feed"))
    await db.execute(
        "INSERT INTO unavailable_guids (feed_id, guid, marked_at) VALUES (?, ?, datetime('now'))",
        (feed_id, "g1"),
    )
    await db.commit()

    await local_xml_sync(db, str(tmp_path))

    async with db.execute("SELECT guid FROM items") as cur:
        guids = {row["guid"] for row in await cur.fetchall()}
    assert guids == {"g2"}


async def test_fetch_feed_304_skips_parse_and_returns_validators(mock_http: respx.MockRouter) -> None:
    route = mock_http.get(_FEED_URL).mock(return_value=httpx.Response(304))
    with patch("src.feeds.fetcher.feedparser.parse", side_effect=AssertionError("parse must not run")):
        async with httpx.AsyncClient() as client:
            items, etag, last_modified = await fetch_feed(
                _FEED_URL, client, frozenset(), '"abc"', "Mon, 01 Jan 2026 00:00:00 GMT"
            )

    assert items == []
    assert etag == '"abc"'
    assert last_modified == "Mon, 01 Jan 2026 00:00:00 GMT"
    sent = route.calls.last.request.headers
    assert sent["if-none-match"] == '"abc"'
    assert sent["if-modified-since"] == "Mon, 01 Jan 2026 00:00:00 GMT"


async def test_refresh_all_feeds_stores_and_replays_etag(db: aiosqlite.Connection, tmp_path: Path) -> None:
    opml = tmp_path / "feeds.opml"
    opml.write_text(_OPML)

    with respx.mock:
        route = respx.get(_FEED_URL).mock(return_value=httpx.Response(200, text=_RSS, headers={"ETag": '"v1"'}))
        async with httpx.AsyncClient() as client:
            await sync_feeds(db, str(tmp_path), str(opml), client)
            await refresh_all_feeds(db, client)

            async with db.execute("SELECT etag FROM feeds WHERE url = ?", (_FEED_URL,)) as cur:
                assert (await cur.fetchone())["etag"] == '"v1"'

            route.mock(return_value=httpx.Response(304))
            await refresh_all_feeds(db, client)

    assert route.calls.last.request.headers["if-none-match"] == '"v1"'
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 2


_RSS_SAME_MEDIA = """\
<?xml version="1.0"?>
<rss version="2.0"><channel><title>Other</title>
  <item>
    <guid>other-1</guid>
    <enclosure url="https://cdn.example.com/a.jpg" type="image/jpeg" length="0"/>
  </item>
  <item>
    <guid>other-2</guid>
    <enclosure url="https://cdn.example.com/b.jpg" type="image/jpeg" length="0"/>
  </item>
</channel></rss>"""


async def _sync_counting_detections(db: aiosqlite.Connection, feeds_dir: Path) -> int:
    """Re-sync with every mtime bumped, and count detector calls.

    Bumping the mtimes defeats the unchanged-source gate on purpose: the
    companion service rewrites its feed files, so in the real deployment that
    gate does not hold and the per-entry skip is what has to do the work.
    """
    for path in feeds_dir.glob("*.xml"):  # noqa: ASYNC240
        stat = path.stat()  # noqa: ASYNC240
        os.utime(path, (stat.st_atime, stat.st_mtime + 10))

    import src.feeds.fetcher as fetcher_mod

    real = fetcher_mod.detect_all_media
    with patch.object(fetcher_mod, "detect_all_media", side_effect=real) as detect:
        await local_xml_sync(db, str(feeds_dir))
    return detect.call_count


async def test_guard_rejected_entries_are_not_redetected(db: aiosqlite.Connection, tmp_path: Path) -> None:
    """Entries the insert guard rejects must still stop being re-detected.

    _INSERT_ITEM's guards key on media_key, which only exists once detection has
    run, while the skip set keys on guid. A cross-feed duplicate is therefore
    detected, rejected, and leaves nothing in items — so without a tombstone its
    guid never enters the skip set and it is re-detected on every single poll.
    """
    (tmp_path / "a.xml").write_text(_RSS)
    (tmp_path / "b.xml").write_text(_RSS_SAME_MEDIA)

    assert await _sync_counting_detections(db, tmp_path) == 4

    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 2, "b.xml's pictures duplicate a.xml's"

    assert await _sync_counting_detections(db, tmp_path) == 0, "rejected guids must not be re-detected"


async def test_seen_media_rejection_is_not_redetected(db: aiosqlite.Connection, tmp_path: Path) -> None:
    """The same hole via the seen_media leg of the guard — the one that grows
    without bound as the user keeps scrolling."""
    (tmp_path / "a.xml").write_text(_RSS)
    await db.execute(
        "INSERT INTO seen_media (media_key, seen_at) VALUES (?, datetime('now'))",
        (media_key("https://cdn.example.com/a.jpg"),),
    )
    await db.commit()

    assert await _sync_counting_detections(db, tmp_path) == 2
    async with db.execute("SELECT guid FROM items") as cur:
        assert {row["guid"] for row in await cur.fetchall()} == {"g2"}

    assert await _sync_counting_detections(db, tmp_path) == 0


async def test_migrations_add_feed_source_columns() -> None:
    conn = await open_db(":memory:")
    await create_schema(conn)
    await run_migrations(conn)
    async with conn.execute("PRAGMA table_info(feeds)") as cur:
        columns = {row["name"] for row in await cur.fetchall()}
    await conn.close()
    assert {"etag", "last_modified", "source_mtime"} <= columns
