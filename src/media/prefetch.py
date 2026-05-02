"""Background media pre-fetching.

Two entry points:

warm_startup_cache() — called once at startup; warms the cache with the most
    recent CACHE_MAX_ITEMS items. Uses a semaphore of 10 and a 100 ms stagger
    to avoid a burst of concurrent requests against upstream servers.

prefetch_ahead() — called from the /api/prefetch/hint endpoint; warms the
    next PREFETCH_AHEAD items older than the given item's pub_date. Intended
    to be fired as a background task ahead of the user's scroll position.
"""

import asyncio
import logging

import aiosqlite
import httpx

from src.config import settings
from src.media.cache import cache_read, cache_write

logger = logging.getLogger(__name__)


async def _warm(url: str, client: httpx.AsyncClient) -> None:
    """Fetch and cache one URL if it is not already cached. Silent on errors."""
    if cache_read(url) is not None:
        return  # already cached — nothing to do
    try:
        response = await client.get(url, follow_redirects=True, timeout=30)
        if response.is_success:
            await cache_write(url, await response.aread())
    except Exception as exc:  # pragma: no cover
        logger.debug("prefetch failed for %s: %s", url, exc)


async def warm_startup_cache(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Pre-warm the cache with the most recently published items.

    Runs as an asyncio background task (fire-and-forget from the lifespan hook).
    A semaphore of 10 and a 100 ms stagger between task creation prevents a
    thundering-herd of concurrent HTTP requests at container start.
    """
    async with db.execute(
        "SELECT media_url FROM items ORDER BY pub_date DESC LIMIT ?",
        (settings.cache_max_items,),
    ) as cur:
        rows = await cur.fetchall()

    sem = asyncio.Semaphore(10)

    async def _bounded_warm(url: str) -> None:
        async with sem:
            await _warm(url, client)

    for row in rows:
        asyncio.create_task(_bounded_warm(row["media_url"]))
        # Small sleep between task creation to spread the initial burst.
        await asyncio.sleep(0.1)


async def prefetch_ahead(item_id: str, db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Fire background warm tasks for the next PREFETCH_AHEAD items after item_id.

    Queries items with a pub_date strictly less than the given item's pub_date
    (i.e. items that come *after* it in reverse-chronological display order).
    Each warm task runs independently; errors are silently ignored.
    """
    async with db.execute(
        """SELECT media_url FROM items
           WHERE pub_date < (SELECT pub_date FROM items WHERE id = ?)
           ORDER BY pub_date DESC
           LIMIT ?""",
        (item_id, settings.prefetch_ahead),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        asyncio.create_task(_warm(row["media_url"], client))
