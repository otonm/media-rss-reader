"""APScheduler setup and shared HTTP client.

_State holds the scheduler and the httpx.AsyncClient as module-level
singletons. The client is shared across the scheduler jobs and API
handlers to reuse connection pools.

Both jobs fire immediately on startup (before the first scheduled
interval) so the reader is populated on first boot without waiting.
Startup failures are logged as warnings — the scheduler retries on
the next interval.
"""

import asyncio
import datetime
import logging

import aiosqlite
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.config import settings
from src.feeds.sync import opml_sync, refresh_all_feeds
from src.media.prefetch import warm_startup_cache

logger = logging.getLogger(__name__)


class _State:
    """Module-level singleton holding runtime objects that outlive a single request."""

    scheduler: AsyncIOScheduler | None = None
    client: httpx.AsyncClient | None = None
    last_opml_sync: datetime.datetime | None = None


_state = _State()


def get_http_client() -> httpx.AsyncClient:
    """Return the shared HTTP client. Raises if called before start_scheduler()."""
    if _state.client is None:
        raise RuntimeError("HTTP client not initialised — call start_scheduler first")
    return _state.client


def get_last_opml_sync() -> datetime.datetime | None:
    """Return the UTC timestamp of the most recent successful OPML sync, or None."""
    return _state.last_opml_sync


async def _opml_sync_job(db: aiosqlite.Connection, opml_path: str, client: httpx.AsyncClient) -> None:
    """Wrapper around opml_sync that updates the last-sync timestamp on success."""
    await opml_sync(db, opml_path, client)
    _state.last_opml_sync = datetime.datetime.now(datetime.UTC)


async def start_scheduler(db: aiosqlite.Connection) -> None:
    """Create the HTTP client, register scheduler jobs, and fire an initial sync.

    Job registration uses string IDs so APScheduler can de-duplicate if
    start_scheduler is somehow called twice in a test environment.
    """
    _state.client = httpx.AsyncClient()
    _state.scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop())

    # Job 1: re-read the OPML file and reconcile the feeds table.
    _state.scheduler.add_job(
        _opml_sync_job,
        "interval",
        seconds=settings.opml_sync_interval,
        args=[db, settings.opml_path, _state.client],
        id="opml_sync",
    )
    # Job 2: fetch new items for every feed and prune old ones.
    _state.scheduler.add_job(
        refresh_all_feeds,
        "interval",
        seconds=settings.feed_refresh_interval,
        args=[db, _state.client],
        id="refresh_feeds",
    )
    _state.scheduler.start()

    # Eager startup: run both jobs immediately so the reader is populated
    # on first boot without waiting for the first scheduled interval.
    try:
        await opml_sync(db, settings.opml_path, _state.client)
        _state.last_opml_sync = datetime.datetime.now(datetime.UTC)
    except Exception as exc:
        logger.warning("Initial OPML sync failed (will retry on schedule): %s", exc)
    try:
        await refresh_all_feeds(db, _state.client)
    except Exception as exc:
        logger.warning("Initial feed refresh failed (will retry on schedule): %s", exc)

    # Startup cache warmup runs as a background task — does not block the server
    # from accepting requests while it downloads media files.
    asyncio.create_task(warm_startup_cache(db, _state.client))


async def stop_scheduler() -> None:
    """Shut down the scheduler and close the HTTP client cleanly."""
    if _state.scheduler and _state.scheduler.running:
        _state.scheduler.shutdown(wait=False)
        _state.scheduler = None
    if _state.client:
        await _state.client.aclose()
        _state.client = None
