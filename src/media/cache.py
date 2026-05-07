"""Filesystem media cache.

Files are stored as {CACHE_DIR}/{sha256(url)} — flat directory, no extension.
The sha256 filename makes lookup O(1) and handles any characters in the URL.

evict() is called after every feed refresh cycle. It removes files that are
too old first, then trims by count from the oldest end if the directory is
still over the limit.
"""

import asyncio
import hashlib
import logging
import time
from pathlib import Path

from src.config import settings

logger = logging.getLogger(__name__)


def _cache_path(url: str) -> Path:
    """Return the filesystem path for a cached URL (does not check existence)."""
    return Path(settings.cache_dir) / hashlib.sha256(url.encode()).hexdigest()


async def cache_write(url: str, data: bytes) -> Path:
    """Write media bytes to the cache and return the path.

    The write is performed off the event loop via asyncio.to_thread to
    avoid blocking the async executor on large file writes.
    """
    path = _cache_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, data)
    return path


def cache_read(url: str) -> Path | None:
    """Return the cached path for a URL, or None on a cache miss."""
    path = _cache_path(url)
    return path if path.exists() else None


def _evict_sync(cache_dir: Path, max_age_secs: float, max_items: int) -> None:
    """Blocking eviction logic — run via asyncio.to_thread to keep the event loop free."""
    now = time.time()
    files = sorted(cache_dir.iterdir(), key=lambda p: p.stat().st_mtime)
    surviving: list[Path] = []
    for f in files:
        if now - f.stat().st_mtime > max_age_secs:
            logger.debug(f"Evicting cache file {f} due to age")
            f.unlink(missing_ok=True)
        else:
            surviving.append(f)
    while len(surviving) > max_items:
        logger.debug(f"Evicting cache file {surviving[0]} due to count limit")
        surviving.pop(0).unlink(missing_ok=True)


async def evict() -> None:
    """Evict stale or excess cache entries without blocking the event loop.

    Step 1: delete files older than CACHE_MAX_AGE_HOURS.
    Step 2: if the surviving count still exceeds CACHE_MAX_ITEMS,
            delete the oldest files (by mtime) until under the limit.
    """
    cache_dir = Path(settings.cache_dir)
    if not cache_dir.exists():
        return
    await asyncio.to_thread(
        _evict_sync,
        cache_dir,
        settings.cache_max_age_hours * 3600,
        settings.cache_max_items,
    )
