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

import aiosqlite
import httpx

from src.config import settings
from src.media.cache import cache_read
from src.media.fetch import fetch_to_cache

logger = logging.getLogger(__name__)


async def _warm(item_id: str, url: str, client: httpx.AsyncClient) -> None:
    """Fetch and cache one URL if it is not already cached.

    Caching, digest recording and dead-URL marking all live in src.media.fetch,
    shared with the proxy, so both paths cannot drift apart.
    """
    if cache_read(url) is not None:
        return  # already cached — nothing to do
    await fetch_to_cache(url, item_id, client)


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
