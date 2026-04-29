import aiosqlite
import httpx

from src.feeds.fetcher import _feed_id, fetch_feed
from src.feeds.opml import parse_opml


async def opml_sync(
    db: aiosqlite.Connection, opml_path: str, client: httpx.AsyncClient
) -> None:
    feeds = parse_opml(opml_path)
    feed_ids = []
    for feed in feeds:
        fid = _feed_id(feed["url"])
        feed_ids.append(fid)
        await db.execute(
            "INSERT OR IGNORE INTO feeds (id, url, title) VALUES (?, ?, ?)",
            (fid, feed["url"], feed["title"]),
        )
    if feed_ids:
        placeholders = ",".join("?" * len(feed_ids))
        await db.execute(
            f"DELETE FROM feeds WHERE id NOT IN ({placeholders})", feed_ids
        )
    else:
        await db.execute("DELETE FROM feeds")
    await db.commit()


async def _refresh_feed(
    db: aiosqlite.Connection,
    feed_id: str,
    url: str,
    client: httpx.AsyncClient,
) -> None:
    items = await fetch_feed(url, client)
    for item in items:
        await db.execute(
            """INSERT OR IGNORE INTO items
               (id, feed_id, guid, title, media_url, media_type, pub_date)
               VALUES (:id, :feed_id, :guid, :title, :media_url, :media_type, :pub_date)""",
            item,
        )
    await db.execute(
        "UPDATE feeds SET last_fetched_at = datetime('now') WHERE id = ?",
        (feed_id,),
    )
    await db.commit()


async def refresh_all_feeds(
    db: aiosqlite.Connection, client: httpx.AsyncClient
) -> None:
    async with db.execute("SELECT id, url FROM feeds") as cur:
        feeds = await cur.fetchall()
    for feed in feeds:
        await _refresh_feed(db, feed["id"], feed["url"], client)
