import logging

import aiosqlite
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.config import settings
from src.feeds.sync import opml_sync, refresh_all_feeds

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("HTTP client not initialised — call start_scheduler first")
    return _client


async def start_scheduler(db: aiosqlite.Connection) -> None:
    global _scheduler, _client  # noqa: PLW0603
    _client = httpx.AsyncClient()
    _scheduler = AsyncIOScheduler()

    _scheduler.add_job(
        opml_sync,
        "interval",
        seconds=settings.opml_sync_interval,
        args=[db, settings.opml_path, _client],
        id="opml_sync",
    )
    _scheduler.add_job(
        refresh_all_feeds,
        "interval",
        seconds=settings.feed_refresh_interval,
        args=[db, _client],
        id="refresh_feeds",
    )
    _scheduler.start()
    try:
        await opml_sync(db, settings.opml_path, _client)
    except Exception:
        logger.warning("Initial OPML sync failed (file may not exist yet)")


async def stop_scheduler() -> None:
    global _scheduler, _client  # noqa: PLW0603
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
    if _client:
        await _client.aclose()
        _client = None
