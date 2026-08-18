"""Background media pre-fetching.

Two entry points, one shared concurrency cap: warm_startup_cache() covers the
cold-start gap at process boot, prefetch_ahead() covers everything after the
user starts scrolling. Ordering rationale for both in spec.md §7.5.
"""

import asyncio
import logging

import aiosqlite
import httpx

from src.config import settings
from src.db.queries import UNSEEN_FIRST_ORDER_BY, ranked_page, resolve_anchor
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

    Warms FEED_INITIAL_COUNT + PREFETCH_AHEAD items in the exact order
    /api/items serves them, so the first page requested is already cached.
    Ordering rationale in spec.md §7.5. Fire-and-forget background task from
    the lifespan hook.
    """
    try:
        rows = await ranked_page(
            db,
            columns="id, media_url",
            unseen=False,
            size=settings.feed_initial_count + settings.prefetch_ahead,
            order=UNSEEN_FIRST_ORDER_BY,
        )
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

    'After' is the (rn, feed_id, id) interleave key /api/items uses. `unseen`
    has no default of its own — the caller must state the filter it paged
    with, so the warm window matches what is about to be displayed. Ordering
    rationale in spec.md §7.5. Returns the number of tasks queued, or None
    when item_id names no row.
    """
    cursor = await resolve_anchor(db, item_id)
    if cursor is None:
        logger.debug(f"prefetch_ahead: item {loggable(item_id)} not found, warming nothing")
        return None
    if _hint_backlog >= MAX_BACKLOG:
        logger.info(f"prefetch_ahead for {loggable(item_id)}: hint backlog at {_hint_backlog}, dropping the hint")
        return 0
    rows = await ranked_page(
        db,
        columns="id, media_url",
        unseen=unseen,
        size=settings.prefetch_ahead,
        after=cursor,
    )
    logger.debug(f"prefetch_ahead for {loggable(item_id)}: {len(rows)} item(s) ahead (unseen={unseen})")
    for row in rows:
        t = asyncio.create_task(_warm(row["id"], row["media_url"], client, request_id=request_id))
        _track_hint(t)
    return len(rows)
