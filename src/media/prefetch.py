"""Background media pre-fetching.

Two entry points:

warm_startup_cache() — called once at startup; warms the cache with the most
    recent CACHE_MAX_ITEMS items, capped at 10 concurrent requests.

prefetch_ahead() — called from the /api/prefetch/hint endpoint; warms the
    next PREFETCH_AHEAD items older than the given item's pub_date. Intended
    to be fired as a background task ahead of the user's scroll position.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiosqlite
import httpx

from src.config import settings
from src.db.connection import open_db
from src.media.availability import mark_url_dead_and_maybe_drop
from src.media.cache import cache_read, cache_stream_write
from src.media.dedup import record_media_hash

logger = logging.getLogger(__name__)


async def _with_own_db(
    label: str,
    url: str,
    write: Callable[[aiosqlite.Connection], Awaitable[object]],
) -> None:
    """Run one DB write on a fresh connection, logging and swallowing failures.

    _warm runs as a fire-and-forget task that outlives its caller, so a
    borrowed connection would already be closed by the time we got here.
    """
    try:
        db = await open_db()
        try:
            await write(db)
        finally:
            await db.close()
    except Exception as exc:  # pragma: no cover
        logger.warning(f"{label} failed for {url}: {exc}")


async def _warm(item_id: str, url: str, client: httpx.AsyncClient) -> None:
    """Fetch and cache one URL if it is not already cached.

    On success, record the content digest so a duplicate arriving under a
    different URL can be collapsed. On upstream non-success, mark the URL dead
    via the availability helper so a fully-dead post can be dropped. Silent on
    errors.
    """
    if cache_read(url) is not None:
        return  # already cached — nothing to do
    try:
        async with client.stream("GET", url, follow_redirects=True, timeout=30) as response:
            if response.is_success:
                content_type = response.headers.get("content-type", "application/octet-stream")
                _, digest = await cache_stream_write(url, response.aiter_bytes(65536), content_type)
                await _with_own_db("record_media_hash", url, lambda db: record_media_hash(url, digest, db))
            else:
                await response.aread()
                await _with_own_db(
                    "mark_url_dead_and_maybe_drop",
                    url,
                    lambda db: mark_url_dead_and_maybe_drop(url, item_id, db),
                )
    except Exception as exc:  # pragma: no cover
        logger.debug(f"prefetch failed for {url}: {exc}")


async def warm_startup_cache(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Pre-warm the cache with the most recently published items.

    Runs as an asyncio background task (fire-and-forget from the lifespan hook).
    The semaphore caps in-flight requests at 10, so upstream never sees a
    thundering herd however many items are queued.
    """
    try:
        async with db.execute(
            "SELECT id, media_url FROM items ORDER BY pub_date DESC LIMIT ?",
            (settings.cache_max_items,),
        ) as cur:
            rows = await cur.fetchall()
        logger.debug(f"warm_startup_cache: {len(rows)} item(s) to pre-warm")
    except Exception as exc:
        logger.warning("warm_startup_cache: DB query failed, skipping cache warm: %s", exc)
        return

    sem = asyncio.Semaphore(10)

    async def _bounded_warm(item_id: str, url: str) -> None:
        async with sem:
            await _warm(item_id, url, client)

    for row in rows:
        asyncio.create_task(_bounded_warm(row["id"], row["media_url"]))


async def prefetch_ahead(item_id: str, db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Fire background warm tasks for the next PREFETCH_AHEAD items after item_id.

    Queries items with a pub_date strictly less than the given item's pub_date
    (i.e. items that come *after* it in reverse-chronological display order).
    Each warm task runs independently; errors are silently ignored.
    """
    async with db.execute(
        """SELECT id, media_url FROM items
           WHERE pub_date < (SELECT pub_date FROM items WHERE id = ?)
           ORDER BY pub_date DESC
           LIMIT ?""",
        (item_id, settings.prefetch_ahead),
    ) as cur:
        rows = await cur.fetchall()
    logger.debug(f"prefetch_ahead for {item_id}: {len(rows)} item(s)")
    for row in rows:
        asyncio.create_task(_warm(row["id"], row["media_url"], client))
