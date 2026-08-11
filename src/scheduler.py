"""Background scheduler: OPML sync, feed refresh, and cache warmup.

Each loop sleeps for its configured interval between runs. The startup sync
fires immediately so the reader is populated on first boot without waiting.
"""

import asyncio
import logging

import aiosqlite
import httpx

from src.config import settings
from src.feeds.sync import refresh_all_feeds, sync_feeds
from src.media.prefetch import cancel_prefetch_tasks, warm_startup_cache

logger = logging.getLogger(__name__)

_bg_tasks: set[asyncio.Task] = set()
_scheduler_tasks: list[asyncio.Task] = []
_running = False


async def _opml_sync_loop(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    logger.debug(f"OPML sync loop started (interval={settings.opml_sync_interval}s)")
    while _running:
        await asyncio.sleep(settings.opml_sync_interval)
        try:
            logger.debug("Sync cycle starting")
            await sync_feeds(db, settings.feeds_dir, settings.opml_path, client)
        except Exception as exc:
            logger.warning("Sync failed (will retry on schedule): %s", exc)


async def _refresh_loop(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    logger.debug(f"Feed refresh loop started (interval={settings.feed_refresh_interval}s)")
    while _running:
        await asyncio.sleep(settings.feed_refresh_interval)
        try:
            logger.debug("Feed refresh cycle starting")
            await refresh_all_feeds(db, client)
        except Exception as exc:
            logger.warning("Feed refresh failed (will retry on schedule): %s", exc)


async def _startup_sync(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Run the initial OPML sync, feed refresh, and cache warmup as a background task."""
    logger.debug("Startup sync beginning")
    try:
        await sync_feeds(db, settings.feeds_dir, settings.opml_path, client)
    except Exception as exc:
        logger.warning("Initial sync failed (will retry on schedule): %s", exc)
    try:
        await refresh_all_feeds(db, client)
    except Exception as exc:
        logger.warning("Initial feed refresh failed (will retry on schedule): %s", exc)
    t = asyncio.create_task(warm_startup_cache(db, client))
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


async def start_scheduler(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Start the background sync loops and fire the initial sync.

    The client is the caller's (main.lifespan's app.state.http) — it must
    outlive stop_scheduler(), which cancels the tasks that use it.
    """
    global _running
    _running = True
    _scheduler_tasks[:] = [
        asyncio.create_task(_opml_sync_loop(db, client)),
        asyncio.create_task(_refresh_loop(db, client)),
        asyncio.create_task(_startup_sync(db, client)),
    ]


async def stop_scheduler() -> None:
    """Shut down background tasks. The HTTP client is not closed here — the
    caller owns it and closes it after this returns."""
    global _running
    _running = False
    for task in _scheduler_tasks:
        task.cancel()
    _scheduler_tasks.clear()
    # The startup-sync task's warm tasks are tracked here and in prefetch.py;
    # they run against the shared client, so they must be cancelled before the
    # caller closes it.
    for task in list(_bg_tasks):
        task.cancel()
    await cancel_prefetch_tasks()
