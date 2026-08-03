import hashlib
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import httpx
from httpx import AsyncClient as HttpxAsyncClient

from src.feeds.sync import local_xml_sync, prune_items

# sync_feeds imported below to keep existing tests importable before Task 6

_RSS_TWO_ITEMS = """\
<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Feed One</title>
  <link>https://publisher.example.com/feed-one</link>
  <item>
    <guid>g1</guid>
    <enclosure url="https://cdn.example.com/a.jpg" type="image/jpeg" length="0"/>
  </item>
  <item>
    <guid>g2</guid>
    <enclosure url="https://cdn.example.com/b.jpg" type="image/jpeg" length="0"/>
  </item>
</channel></rss>
"""


async def test_local_xml_sync_inserts_feeds_and_items(db: aiosqlite.Connection, tmp_path: Path) -> None:
    (tmp_path / "feed-one.xml").write_text(_RSS_TWO_ITEMS)

    await local_xml_sync(db, str(tmp_path))

    async with db.execute("SELECT id, url, title, site_link FROM feeds") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["url"] == "feed-one.xml"
    assert rows[0]["title"] == "Feed One"
    assert rows[0]["site_link"] == "https://publisher.example.com/feed-one"

    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 2


from src.feeds.sync import sync_feeds  # noqa: E402

_OPML_ONLY = """\
<?xml version="1.0"?>
<opml version="2.0"><head/><body>
  <outline type="rss" text="Opml Feed" xmlUrl="https://opml.example.com/feed.xml"/>
</body></opml>"""

_OPML_RSS = """\
<?xml version="1.0"?>
<rss version="2.0"><channel><title>Opml Feed</title>
  <item>
    <guid>opml-g1</guid>
    <enclosure url="https://cdn.example.com/o.jpg" type="image/jpeg" length="0"/>
  </item>
</channel></rss>"""


async def test_sync_feeds_union_folder_and_opml(
    db: aiosqlite.Connection,
    tmp_path: Path,
) -> None:
    (tmp_path / "feed-one.xml").write_text(_RSS_TWO_ITEMS)
    opml = tmp_path / "feeds.opml"
    opml.write_text(_OPML_ONLY)

    async with httpx.AsyncClient() as client:
        await sync_feeds(db, str(tmp_path), str(opml), client)

    async with db.execute("SELECT url FROM feeds ORDER BY url") as cur:
        urls = [r["url"] for r in await cur.fetchall()]
    assert urls == ["feed-one.xml", "https://opml.example.com/feed.xml"]


async def test_sync_feeds_hard_deletes_missing_folder_file(db: aiosqlite.Connection, tmp_path: Path) -> None:
    feeds_dir = tmp_path / "feeds"
    feeds_dir.mkdir()
    (feeds_dir / "keepme.xml").write_text(_RSS_TWO_ITEMS)
    # Distinct media URLs: with two feeds carrying the *same* picture, the
    # cross-feed dedup guard would store it once and this test would be
    # measuring deduplication rather than the CASCADE it means to check.
    (feeds_dir / "gone.xml").write_text(_RSS_TWO_ITEMS.replace("cdn.example.com/", "cdn.example.com/gone-"))
    await local_xml_sync(db, str(feeds_dir))

    async with db.execute("SELECT COUNT(*) FROM feeds") as cur:
        assert (await cur.fetchone())[0] == 2

    (feeds_dir / "gone.xml").unlink()
    async with httpx.AsyncClient() as client:
        await sync_feeds(db, str(feeds_dir), "", client)

    async with db.execute("SELECT url FROM feeds") as cur:
        urls = [r["url"] for r in await cur.fetchall()]
    assert urls == ["keepme.xml"]
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        items_after = (await cur.fetchone())[0]
    assert items_after == 2


async def test_sync_feeds_empty_union_keeps_feeds(db: aiosqlite.Connection, tmp_path: Path) -> None:
    """An empty FEEDS_DIR is a mount/timing problem, not "delete everything".

    The companion service restarting, or a volume that is not ready when the
    container starts, used to cascade the whole database away.
    """
    feeds_dir = tmp_path / "feeds"
    feeds_dir.mkdir()
    (feeds_dir / "keepme.xml").write_text(_RSS_TWO_ITEMS)
    async with httpx.AsyncClient() as client:
        await sync_feeds(db, str(feeds_dir), "", client)

    (feeds_dir / "keepme.xml").unlink()
    async with httpx.AsyncClient() as client:
        await sync_feeds(db, str(feeds_dir), "", client)

    async with db.execute("SELECT url FROM feeds") as cur:
        assert [r["url"] for r in await cur.fetchall()] == ["keepme.xml"]
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 2


async def test_seen_survives_prune_and_local_resync(
    db: aiosqlite.Connection, client: HttpxAsyncClient, tmp_path: Path
) -> None:
    """The reported bug: a seen local-feed item comes back unmarked.

    prune_items evicts seen rows first, then the next local_xml_sync re-inserts
    them straight out of the XML file that still lists them.
    """
    (tmp_path / "feed-one.xml").write_text(_RSS_TWO_ITEMS)
    await local_xml_sync(db, str(tmp_path))

    async with db.execute("SELECT id FROM items WHERE guid = 'g1'") as cur:
        item_id = (await cur.fetchone())["id"]
    assert (await client.post(f"/api/items/{item_id}/seen")).status_code == 200

    with patch("src.feeds.sync.settings") as mock_settings:
        mock_settings.items_max_age_hours = 9999
        mock_settings.keep_items = 1
        await prune_items(db)
    async with db.execute("SELECT guid FROM items") as cur:
        assert [r["guid"] for r in await cur.fetchall()] == ["g2"]

    # The XML file still lists g1. It must not come back.
    await local_xml_sync(db, str(tmp_path))
    async with db.execute("SELECT guid FROM items ORDER BY guid") as cur:
        assert [r["guid"] for r in await cur.fetchall()] == ["g2"]


async def test_sync_feeds_idempotent_and_feed_id_is_filename(db: aiosqlite.Connection, tmp_path: Path) -> None:
    (tmp_path / "feed-one.xml").write_text(_RSS_TWO_ITEMS)
    async with httpx.AsyncClient() as client:
        await sync_feeds(db, str(tmp_path), "", client)
        await sync_feeds(db, str(tmp_path), "", client)

    async with db.execute("SELECT COUNT(*) FROM feeds") as cur:
        assert (await cur.fetchone())[0] == 1

    expected = hashlib.sha256(b"feed-one.xml").hexdigest()
    async with db.execute("SELECT id FROM feeds") as cur:
        assert (await cur.fetchone())["id"] == expected


_OPML_DUPLICATE = """\
<?xml version="1.0"?>
<opml version="2.0"><head/><body>
  <outline type="rss" text="Grool" xmlUrl="https://reddit-feeds.example.ts.net/feeds/grool.xml"/>
</body></opml>"""


async def test_sync_feeds_folder_supersedes_opml_duplicate(db: aiosqlite.Connection, tmp_path: Path) -> None:
    """When grool.xml is in both FEEDS_DIR and OPML, only the folder row survives."""
    (tmp_path / "grool.xml").write_text(_RSS_TWO_ITEMS)
    opml = tmp_path / "feeds.opml"
    opml.write_text(_OPML_DUPLICATE)

    async with httpx.AsyncClient() as client:
        await sync_feeds(db, str(tmp_path), str(opml), client)

    async with db.execute("SELECT url FROM feeds ORDER BY url") as cur:
        urls = [r["url"] for r in await cur.fetchall()]
    assert urls == ["grool.xml"]
