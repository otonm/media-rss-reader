from pathlib import Path

import aiosqlite
import httpx
import respx

from src.feeds.sync import opml_sync, refresh_all_feeds

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
