"""Background media pre-fetching.

Two entry points:

warm_startup_cache() — called once at startup; warms the cache with the most
    recent CACHE_MAX_ITEMS items, capped at 10 concurrent requests.

prefetch_ahead() — called from the /api/prefetch/hint endpoint; warms the
    next PREFETCH_AHEAD items after the given item in interleave order.
    Intended to be fired as a background task ahead of the user's scroll
    position.
"""

import asyncio
import logging

import aiosqlite
import httpx

from src.config import settings
from src.db.queries import INTERLEAVE_ORDER_BY, RANKED_ITEMS_CTE
from src.media.cache import cache_read
from src.media.fetch import fetch_to_cache

logger = logging.getLogger(__name__)

_bg_tasks: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> None:
    """Keep a strong ref so the event loop's weak ref doesn't GC the task (F8)."""
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _warm(item_id: str, url: str, client: httpx.AsyncClient) -> None:
    """Fetch and cache one URL if it is not already cached.

    Caching, digest recording and dead-URL marking all live in src.media.fetch,
    shared with the proxy, so both paths cannot drift apart.
    """
    if cache_read(url) is not None:
        logger.debug(f"_warm: {url} already on disk, skipping")
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
        t = asyncio.create_task(_bounded_warm(row["id"], row["media_url"]))
        _track(t)


async def prefetch_ahead(item_id: str, db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Fire background warm tasks for the next PREFETCH_AHEAD items after item_id.

    'After' means strictly greater in the (rn, feed_id, id) interleave key
    that /api/items uses — i.e. items the client will request next as it
    scrolls forward. Previously this queried pub_date < cursor, which under
    the current ASC display order warmed items the user had already scrolled
    past (F2). Also applies the unseen filter so we don't warm items the
    client will never request.
    """
    async with db.execute(
        f"{RANKED_ITEMS_CTE} SELECT rn, feed_id, id FROM ranked WHERE id = ?",
        (item_id,),
    ) as cur:
        cursor = await cur.fetchone()
    if cursor is None:
        logger.debug(f"prefetch_ahead: item {item_id} not found, warming nothing")
        return
    async with db.execute(
        f"""{RANKED_ITEMS_CTE}
            SELECT id, media_url FROM ranked
            WHERE seen_at IS NULL AND (rn, feed_id, id) > (?, ?, ?)
            {INTERLEAVE_ORDER_BY} LIMIT ?""",
        (cursor["rn"], cursor["feed_id"], cursor["id"], settings.prefetch_ahead),
    ) as cur:
        rows = await cur.fetchall()
    logger.debug(f"prefetch_ahead for {item_id}: {len(rows)} item(s) ahead")
    for row in rows:
        t = asyncio.create_task(_warm(row["id"], row["media_url"], client))
        _track(t)
