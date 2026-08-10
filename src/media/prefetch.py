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

# One cap for both entry points. warm_startup_cache had its own Semaphore(10);
# the request-driven path had none at all, so a fast scroll (a hint per scroll
# snap) accumulated unbounded tasks and outbound connections — the semaphore
# capped concurrency (in-flight requests), not existence. A task waiting on it
# is still a live task strongly referenced in _bg_tasks, so the backlog still
# grew monotonically and drained against windows scrolled past minutes ago
# (R6/minor 12).
_sem = asyncio.Semaphore(10)

# _bg_tasks holds every warm task from BOTH producers, so the event loop's weak
# ref doesn't GC either kind (F8) and cancel_prefetch_tasks can stop both on
# shutdown. MAX_BACKLOG must not be checked against that shared set: the two
# producers have unrelated budgets, so a startup warm still draining would spend
# the hint path's cap on the reader's behalf. That once dropped every hint for
# the whole cold-cache window after every restart — precisely the window
# prefetching exists for — back when the startup warm queued CACHE_MAX_ITEMS
# (500) tasks, ten times this cap. It is bounded far lower now, but sharing the
# counter would still be wrong for the same reason. _hint_tasks tracks only the
# request-driven path a second time, so the cap measures what it is meant to: a
# fast scroll growing that backlog without bound (_sem caps concurrency, not
# existence, and the hint endpoint applies no coalescing) — a newer scroll
# position supersedes an older one, so dropping the new hint once that backlog
# is full is the right direction.
_hint_tasks: set[asyncio.Task] = set()

MAX_BACKLOG = 50


def _track(task: asyncio.Task) -> None:
    """Keep a strong ref so the event loop's weak ref doesn't GC the task (F8)."""
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _track_hint(task: asyncio.Task) -> None:
    """Like _track, but also counted against MAX_BACKLOG (hint path only)."""
    _track(task)
    _hint_tasks.add(task)
    task.add_done_callback(_hint_tasks.discard)


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
            logger.debug(f"_warm: {loggable(url)} already on disk, skipping")
            return  # already cached — nothing to do
        await fetch_to_cache(url, item_id, client, request_id=request_id)


async def warm_startup_cache(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Pre-warm the cache with the items the reader will see first.

    The order has to be the order /api/items serves, not "most recent": the feed
    interleaves feeds and runs OLDEST-first within each, filtered to unseen by
    default. Warming by pub_date DESC filled the opposite end of the library, so
    the first page was always a cold miss (see UNSEEN_FIRST_ORDER_BY).

    The bound covers exactly the cold-start gap — the first page the browser
    asks for, plus the window prefetch_ahead would have warmed had a scroll
    already happened. It used to be CACHE_MAX_ITEMS (500), which made a cold
    start fire up to 500 upstream fetches before the reader had opened anything;
    every permanently-gone URL among them deletes its item (open_upstream ->
    _mark_dead), so the boot sweep was also an unattended mass-deletion window.
    CACHE_MAX_ITEMS is the eviction budget, not a warm-queue depth. Past this
    window the hint path drives the cache on demand, which is the whole point of
    prefetch_ahead.

    Warming an item already on disk costs nothing: _warm returns on a cache_read
    hit without opening a connection, so a restart with a warm cache issues no
    upstream requests at all.

    Runs as an asyncio background task (fire-and-forget from the lifespan hook).
    The shared semaphore caps in-flight requests at 10 across the startup warm
    and the request-driven hint both, so upstream never sees a thundering herd
    however many items are queued.
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

    'After' means strictly greater in the (rn, feed_id, id) interleave key that
    /api/items uses — i.e. items the client will request next as it scrolls
    forward. Previously this queried pub_date < cursor, which under the current
    ASC display order warmed items the user had already scrolled past (F2).

    `unseen` mirrors the filter the page itself used and has no default of its
    own — the caller must state the filter it paged with. It used to be
    hardcoded to "seen_at IS NULL", so with the show-seen toggle on the client
    requested unseen=false while the hint fired from the same scroll warmed
    only unseen items — the items about to be displayed were never warmed
    (R12).

    Returns the number of warm tasks queued, which the hint endpoint logs (R9),
    or None when item_id names no row — the hint turns that into F16's 404
    rather than running a second lookup of its own.

    request_id ties the warm tasks back to the hint that queued them. The tasks
    outlive the request, so the contextvar is already reset by the time they
    log — it has to be passed explicitly, exactly as open_upstream and
    tee_to_cache already accept it.
    """
    # Interpolated SQL fragments are source-controlled; request values remain bound.
    async with db.execute(ANCHOR_LOOKUP, (item_id,)) as cur:
        cursor = await cur.fetchone()
    if cursor is None:
        logger.debug(f"prefetch_ahead: item {loggable(item_id)} not found, warming nothing")
        return None
    if len(_hint_tasks) >= MAX_BACKLOG:
        logger.info(f"prefetch_ahead for {loggable(item_id)}: hint backlog at {len(_hint_tasks)}, dropping the hint")
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
