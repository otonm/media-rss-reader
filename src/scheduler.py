"""Background scheduler: OPML sync, feed refresh, and cache warmup.

Replaces APScheduler with plain asyncio task loops. Each loop sleeps for
its configured interval between runs. The startup sync fires immediately
so the reader is populated on first boot without waiting.
"""

import asyncio
import datetime
import logging

import aiosqlite
import httpx

from src.config import settings
from src.feeds.sync import opml_sync, refresh_all_feeds
from src.media.prefetch import warm_startup_cache

logger = logging.getLogger(__name__)


class _State:
    scheduler: list[asyncio.Task] = []  # noqa: RUF012
    client: httpx.AsyncClient | None = None
    last_opml_sync: datetime.datetime | None = None
    running: bool = False


_state = _State()


def get_http_client() -> httpx.AsyncClient:
    """Return the shared HTTP client. Raises if called before start_scheduler()."""
    if _state.client is None:
        raise RuntimeError("HTTP client not initialised — call start_scheduler first")
    return _state.client


def get_last_opml_sync() -> datetime.datetime | None:
    """Return the UTC timestamp of the most recent successful OPML sync, or None."""
    return _state.last_opml_sync


async def _opml_sync_loop(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    while _state.running:
        await asyncio.sleep(settings.opml_sync_interval)
        try:
            await opml_sync(db, settings.opml_path, client)
            _state.last_opml_sync = datetime.datetime.now(datetime.UTC)
        except Exception as exc:
            logger.warning("OPML sync failed (will retry on schedule): %s", exc)


async def _refresh_loop(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    while _state.running:
        await asyncio.sleep(settings.feed_refresh_interval)
        try:
            await refresh_all_feeds(db, client)
        except Exception as exc:
            logger.warning("Feed refresh failed (will retry on schedule): %s", exc)


async def _startup_sync(db: aiosqlite.Connection) -> None:
    """Run the initial OPML sync, feed refresh, and cache warmup as a background task."""
    try:
        await opml_sync(db, settings.opml_path, _state.client)
        _state.last_opml_sync = datetime.datetime.now(datetime.UTC)
    except Exception as exc:
        logger.warning("Initial OPML sync failed (will retry on schedule): %s", exc)
    try:
        await refresh_all_feeds(db, _state.client)
    except Exception as exc:
        logger.warning("Initial feed refresh failed (will retry on schedule): %s", exc)
    asyncio.create_task(warm_startup_cache(db, _state.client))


async def start_scheduler(db: aiosqlite.Connection) -> None:
    """Create the HTTP client, start background sync loops, and fire initial sync."""
    _state.client = httpx.AsyncClient()
    _state.running = True
    _state.scheduler = [
        asyncio.create_task(_opml_sync_loop(db, _state.client)),
        asyncio.create_task(_refresh_loop(db, _state.client)),
        asyncio.create_task(_startup_sync(db)),
    ]


async def stop_scheduler() -> None:
    """Shut down background tasks and close the HTTP client cleanly."""
    _state.running = False
    for task in _state.scheduler:
        task.cancel()
    _state.scheduler = []
    if _state.client:
        await _state.client.aclose()
        _state.client = None
