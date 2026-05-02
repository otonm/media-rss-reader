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
    scheduler: AsyncIOScheduler | None = None
    client: httpx.AsyncClient | None = None
    last_opml_sync: datetime.datetime | None = None


_state = _State()


def get_http_client() -> httpx.AsyncClient:
    if _state.client is None:
        raise RuntimeError("HTTP client not initialised — call start_scheduler first")
    return _state.client


def get_last_opml_sync() -> datetime.datetime | None:
    return _state.last_opml_sync


async def _opml_sync_job(db: aiosqlite.Connection, opml_path: str, client: httpx.AsyncClient) -> None:
    await opml_sync(db, opml_path, client)
    _state.last_opml_sync = datetime.datetime.now(datetime.UTC)


async def start_scheduler(db: aiosqlite.Connection) -> None:
    _state.client = httpx.AsyncClient()
    _state.scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop())

    _state.scheduler.add_job(
        _opml_sync_job,
        "interval",
        seconds=settings.opml_sync_interval,
        args=[db, settings.opml_path, _state.client],
        id="opml_sync",
    )
    _state.scheduler.add_job(
        refresh_all_feeds,
        "interval",
        seconds=settings.feed_refresh_interval,
        args=[db, _state.client],
        id="refresh_feeds",
    )
    _state.scheduler.start()
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


async def stop_scheduler() -> None:
    if _state.scheduler and _state.scheduler.running:
        _state.scheduler.shutdown(wait=False)
        _state.scheduler = None
    if _state.client:
        await _state.client.aclose()
        _state.client = None
