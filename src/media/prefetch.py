"""Background media pre-fetching.

Two entry points:

warm_startup_cache() — called once at startup; warms the first page /api/items
    would serve plus PREFETCH_AHEAD, capped at 10 concurrent requests. Just the
    cold-start gap: from the first scroll snap onward prefetch_ahead takes over.

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
from src.db.queries import (
    ANCHOR_LOOKUP,
    INTERLEAVE_ORDER_BY,
    KEYSET_AFTER,
    RANKED_ITEMS_CTE,
    UNSEEN_FIRST_ORDER_BY,
)
from src.logging_utils import loggable
from src.media.cache import cache_read
from src.media.fetch import fetch_to_cache

logger = logging.getLogger(__name__)

_bg_tasks: set[asyncio.Task] = set()

# One cap for both entry points: at most 10 warm requests in flight, so a
# fast scroll cannot pile up unbounded outbound connections.
_sem = asyncio.Semaphore(10)

# _bg_tasks holds a strong reference to every warm task from both producers so
# the event loop's weak ref doesn't GC them, and cancel_prefetch_tasks can stop
# all of them on shutdown.
#
# _hint_backlog counts only the request-driven path, so MAX_BACKLOG measures the
# hint backlog alone. It cannot be len(_bg_tasks): the startup warm fills that
# same set from its own unrelated budget (feed_initial_count + prefetch_ahead),
# and sharing the counter would let a draining startup warm spend the hint
# path's cap. See tests/test_prefetch.py::test_prefetch_ahead_queues_despite_a_full_startup_warm_backlog.
_hint_backlog = 0

MAX_BACKLOG = 50


def _track(task: asyncio.Task) -> None:
    """Keep a strong ref so the event loop's weak ref doesn't GC the task."""
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _track_hint(task: asyncio.Task) -> None:
    """Like _track, but also counted against MAX_BACKLOG (hint path only)."""
    global _hint_backlog
    _track(task)
    _hint_backlog += 1
    task.add_done_callback(_release_hint)


def _release_hint(_task: asyncio.Task) -> None:
    global _hint_backlog
    _hint_backlog -= 1


async def cancel_prefetch_tasks() -> None:
    """Cancel in-flight warm tasks. Called from stop_scheduler.

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
            logger.debug(f"_warm: {loggable(url)} already on disk, skipping")
            return
        await fetch_to_cache(url, item_id, client, request_id=request_id)


async def warm_startup_cache(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Pre-warm the cache with the items the reader will see first.

    Uses the same order /api/items serves — feeds interleaved, oldest-first
    within each, unseen filtered by default — so the first page the browser
    asks for is exactly what was warmed. The bound covers the cold-start gap
    (first page plus the prefetch_ahead window); past it the hint path drives
    the cache on demand. Warming an item already on disk costs nothing: _warm
    returns on a cache_read hit without opening a connection, so a restart
    with a warm cache issues no upstream requests.

    Runs as an asyncio background task (fire-and-forget from the lifespan
    hook); the shared semaphore caps in-flight requests at 10 across both
    entry points.
    """
    try:
        # Interpolated SQL fragments are source-controlled; the limit is bound.
        async with db.execute(
            f"""{RANKED_ITEMS_CTE}
                SELECT id, media_url FROM ranked
                {UNSEEN_FIRST_ORDER_BY} LIMIT ?""",  # noqa: S608
            (settings.feed_initial_count + settings.prefetch_ahead,),
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
    *,
    unseen: bool,
    request_id: str | None = None,
) -> int | None:
    """Fire background warm tasks for the next PREFETCH_AHEAD items after item_id.

    'After' means strictly greater in the (rn, feed_id, id) interleave key
    that /api/items uses — i.e. items the client will request next as it
    scrolls forward.

    `unseen` mirrors the filter the page itself used and has no default of
    its own — the caller must state the filter it paged with, so the warm
    window always matches what is about to be displayed.

    Returns the number of warm tasks queued, or None when item_id names no
    row — the hint endpoint turns that into a 404 without a second lookup of
    its own.

    request_id ties the warm tasks back to the hint that queued them. The
    tasks outlive the request, so the contextvar is already reset by the time
    they log — it has to be passed explicitly, exactly as open_upstream and
    tee_to_cache already accept it.
    """
    # Interpolated SQL fragments are source-controlled; request values remain bound.
    async with db.execute(ANCHOR_LOOKUP, (item_id,)) as cur:
        cursor = await cur.fetchone()
    if cursor is None:
        logger.debug(f"prefetch_ahead: item {loggable(item_id)} not found, warming nothing")
        return None
    if _hint_backlog >= MAX_BACKLOG:
        logger.info(f"prefetch_ahead for {loggable(item_id)}: hint backlog at {_hint_backlog}, dropping the hint")
        return 0
    seen_filter = "seen_at IS NULL AND " if unseen else ""
    async with db.execute(
        f"""{RANKED_ITEMS_CTE}
            SELECT id, media_url FROM ranked
            WHERE {seen_filter}{KEYSET_AFTER}
            {INTERLEAVE_ORDER_BY} LIMIT ?""",  # noqa: S608
        (cursor["rn"], cursor["feed_id"], cursor["id"], settings.prefetch_ahead),
    ) as cur:
        rows = await cur.fetchall()
    logger.debug(f"prefetch_ahead for {loggable(item_id)}: {len(rows)} item(s) ahead (unseen={unseen})")
    for row in rows:
        t = asyncio.create_task(_warm(row["id"], row["media_url"], client, request_id=request_id))
        _track_hint(t)
    return len(rows)
