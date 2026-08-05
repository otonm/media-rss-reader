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

# One cap for both entry points. warm_startup_cache had its own Semaphore(10);
# the request-driven path had none at all, so a fast scroll (a hint per scroll
# snap) accumulated unbounded tasks and outbound connections — download_claim
# only collapses duplicates of the *same* URL (R6).
_sem = asyncio.Semaphore(10)


def _track(task: asyncio.Task) -> None:
    """Keep a strong ref so the event loop's weak ref doesn't GC the task (F8)."""
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def cancel_prefetch_tasks() -> None:
    """Cancel in-flight warm tasks. Called from stop_scheduler (R6).

    Without this they outlive the shared HTTP client that stop_scheduler
    closes, and keep running against it.
    """
    tasks = list(_bg_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    logger.debug(f"cancel_prefetch_tasks: cancelled {len(tasks)} warm task(s)")


async def _warm(item_id: str, url: str, client: httpx.AsyncClient, request_id: str | None = None) -> None:
    """Fetch and cache one URL if it is not already cached.

    Caching, digest recording and dead-URL marking all live in src.media.fetch,
    shared with the proxy, so both paths cannot drift apart. The semaphore caps
    in-flight warms across both entry points.
    """
    async with _sem:
        if cache_read(url) is not None:
            logger.debug(f"_warm: {url} already on disk, skipping")
            return  # already cached — nothing to do
        await fetch_to_cache(url, item_id, client, request_id=request_id)


async def warm_startup_cache(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Pre-warm the cache with the most recently published items.

    Runs as an asyncio background task (fire-and-forget from the lifespan hook).
    The shared semaphore caps in-flight requests at 10 across the startup warm
    and the request-driven hint both, so upstream never sees a thundering herd
    however many items are queued.
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

    for row in rows:
        t = asyncio.create_task(_warm(row["id"], row["media_url"], client))
        _track(t)


async def prefetch_ahead(
    item_id: str,
    db: aiosqlite.Connection,
    client: httpx.AsyncClient,
    unseen: bool = True,
    request_id: str | None = None,
) -> int:
    """Fire background warm tasks for the next PREFETCH_AHEAD items after item_id.

    'After' means strictly greater in the (rn, feed_id, id) interleave key that
    /api/items uses — i.e. items the client will request next as it scrolls
    forward. Previously this queried pub_date < cursor, which under the current
    ASC display order warmed items the user had already scrolled past (F2).

    `unseen` mirrors the filter the page itself used. It used to be hardcoded
    to "seen_at IS NULL", so with the show-seen toggle on the client requested
    unseen=false while the hint fired from the same scroll warmed only unseen
    items — the items about to be displayed were never warmed (R12).

    Returns the number of warm tasks queued, which the hint endpoint logs (R9).

    request_id ties the warm tasks back to the hint that queued them. The tasks
    outlive the request, so the contextvar is already reset by the time they
    log — it has to be passed explicitly, exactly as open_upstream and
    tee_to_cache already accept it.
    """
    # Interpolated SQL fragments are source-controlled; request values remain bound.
    async with db.execute(
        f"{RANKED_ITEMS_CTE} SELECT rn, feed_id, id FROM ranked WHERE id = ?",  # noqa: S608
        (item_id,),
    ) as cur:
        cursor = await cur.fetchone()
    if cursor is None:
        logger.debug(f"prefetch_ahead: item {item_id} not found, warming nothing")
        return 0
    seen_filter = "seen_at IS NULL AND " if unseen else ""
    async with db.execute(
        f"""{RANKED_ITEMS_CTE}
            SELECT id, media_url FROM ranked
            WHERE {seen_filter}(rn, feed_id, id) > (?, ?, ?)
            {INTERLEAVE_ORDER_BY} LIMIT ?""",  # noqa: S608
        (cursor["rn"], cursor["feed_id"], cursor["id"], settings.prefetch_ahead),
    ) as cur:
        rows = await cur.fetchall()
    logger.debug(f"prefetch_ahead for {item_id}: {len(rows)} item(s) ahead (unseen={unseen})")
    for row in rows:
        t = asyncio.create_task(_warm(row["id"], row["media_url"], client, request_id=request_id))
        _track(t)
    return len(rows)
