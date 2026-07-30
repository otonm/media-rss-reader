"""Filesystem media cache.

Files are stored as {CACHE_DIR}/{sha256(url)} — flat directory, no extension.
The sha256 filename makes lookup O(1) and handles any characters in the URL.

A sidecar file {CACHE_DIR}/{sha256(url)}.meta holds the upstream Content-Type
string. The proxy uses it to set a correct Content-Type header on cache
hits, which matters for the browser to e.g. animate a cached GIF. The
sidecar is written atomically alongside the data file.

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


def _meta_path(url: str) -> Path:
    """Return the sidecar path holding the cached URL's Content-Type."""
    return _cache_path(url).with_suffix(".meta")


def _write_meta(meta_path: Path, content_type: str) -> None:
    meta_path.write_text(content_type, encoding="ascii")


async def cache_stream_write(
    url: str, chunks: AsyncIterable[bytes], content_type: str = "application/octet-stream"
) -> Path:
    """Stream an async byte iterator to the cache file without buffering in memory.

    Writes to a .tmp sibling first, then renames atomically so a partial
    download never leaves a corrupt cache entry. The Content-Type sidecar
    is written only after the data file is in place, so a partial download
    leaves no sidecar that would mislead the proxy.
    """
    path = _cache_path(url)
    meta = _meta_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with tmp.open("wb") as fh:
            async for chunk in chunks:
                fh.write(chunk)
        await asyncio.to_thread(tmp.rename, path)
        await asyncio.to_thread(_write_meta, meta, content_type)
        logger.debug(f"cache_stream_write: cached {url} (type={content_type})")
    except Exception:
        tmp.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        raise
    return path


def cache_read(url: str) -> Path | None:
    """Return the cached path for a URL, or None on a cache miss."""
    path = _cache_path(url)
    return path if path.exists() else None


def cache_read_meta(url: str) -> str | None:
    """Return the cached Content-Type for a URL, or None if unknown."""
    meta = _meta_path(url)
    if not meta.exists():
        return None
    return meta.read_text(encoding="ascii").strip() or None


def _evict_sync(cache_dir: Path, max_age_secs: float, max_items: int) -> None:
    """Blocking eviction logic — run via asyncio.to_thread to keep the event loop free.

    Each data file and its .meta sidecar share the same mtime (written
    together), so evicting one takes the other along. Counting is by data
    file only; .meta entries are skipped so the count matches what the
    proxy would actually serve.
    """
    if not cache_dir.exists():
        return
    now = time.time()
    files = sorted(cache_dir.iterdir(), key=lambda p: p.stat().st_mtime)
    surviving: list[Path] = []
    for f in files:
        if f.suffix == ".meta":
            continue
        if now - f.stat().st_mtime > max_age_secs:
            logger.debug(f"Evicting cache file {f} due to age")
            f.unlink(missing_ok=True)
            f.with_suffix(".meta").unlink(missing_ok=True)
        else:
            surviving.append(f)
    while len(surviving) > max_items:
        logger.debug(f"Evicting cache file {surviving[0]} due to count limit")
        head = surviving.pop(0)
        head.unlink(missing_ok=True)
        head.with_suffix(".meta").unlink(missing_ok=True)


async def evict() -> None:
    """Evict stale or excess cache entries without blocking the event loop.

    Step 1: delete files older than CACHE_MAX_AGE_HOURS.
    Step 2: if the surviving count still exceeds CACHE_MAX_ITEMS,
            delete the oldest files (by mtime) until under the limit.
    """
    cache_dir = Path(settings.cache_dir)
    await asyncio.to_thread(
        _evict_sync,
        cache_dir,
        settings.cache_max_age_hours * 3600,
        settings.cache_max_items,
    )
