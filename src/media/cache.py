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
from collections.abc import AsyncIterable
from pathlib import Path

from src.config import settings

logger = logging.getLogger(__name__)


def _cache_path(url: str) -> Path:
    """Return the filesystem path for a cached URL (does not check existence)."""
    return Path(settings.cache_dir) / hashlib.sha256(url.encode()).hexdigest()


async def cache_write(url: str, data: bytes) -> Path:
    """Write media bytes to the cache and return the path."""
    path = _cache_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, data)
    return path


async def cache_stream_write(url: str, chunks: AsyncIterable[bytes]) -> Path:
    """Stream an async byte iterator to the cache file without buffering in memory.

    Writes to a .tmp sibling first, then renames atomically so a partial
    download never leaves a corrupt cache entry.
    """
    path = _cache_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with tmp.open("wb") as fh:
            async for chunk in chunks:
                fh.write(chunk)
        await asyncio.to_thread(tmp.rename, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
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


def _evict_if_exists(cache_dir: Path, max_age_secs: float, max_items: int) -> None:
    """Check existence and evict — runs in a thread to keep the event loop free."""
    if not cache_dir.exists():
        return
    _evict_sync(cache_dir, max_age_secs, max_items)


async def evict() -> None:
    """Evict stale or excess cache entries without blocking the event loop.

    Step 1: delete files older than CACHE_MAX_AGE_HOURS.
    Step 2: if the surviving count still exceeds CACHE_MAX_ITEMS,
            delete the oldest files (by mtime) until under the limit.
    """
    cache_dir = Path(settings.cache_dir)
    await asyncio.to_thread(
        _evict_if_exists,
        cache_dir,
        settings.cache_max_age_hours * 3600,
        settings.cache_max_items,
    )
