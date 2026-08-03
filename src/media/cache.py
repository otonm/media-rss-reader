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
import contextlib
import hashlib
import logging
import os
import tempfile
import time
from collections.abc import AsyncIterable, AsyncIterator, Iterator
from pathlib import Path

from src.config import settings

logger = logging.getLogger(__name__)

# URLs currently being downloaded, by number of concurrent downloaders. The
# browser's proxy GET for an item routinely overlaps the background _warm task
# for the same URL, and prefetch_ahead re-queues overlapping windows on every
# scroll event, so the same URL would otherwise be pulled from the origin
# several times at once.
_inflight: dict[str, int] = {}


def _cache_path(url: str) -> Path:
    """Return the filesystem path for a cached URL (does not check existence)."""
    return Path(settings.cache_dir) / hashlib.sha256(url.encode()).hexdigest()


def _meta_path(url: str) -> Path:
    """Return the sidecar path holding the cached URL's Content-Type."""
    return _cache_path(url).with_suffix(".meta")


def _write_meta(meta_path: Path, content_type: str) -> None:
    meta_path.write_text(content_type, encoding="ascii")


@contextlib.contextmanager
def download_claim(url: str) -> Iterator[bool]:
    """Mark `url` as being downloaded for the duration of the block.

    Yields True to the first caller in, False while another download of the
    same URL is still running. Background prefetch skips a False claim; the
    proxy proceeds regardless, because a user is waiting for those bytes.
    """
    first = url not in _inflight
    _inflight[url] = _inflight.get(url, 0) + 1
    try:
        yield first
    finally:
        remaining = _inflight[url] - 1
        if remaining:
            _inflight[url] = remaining
        else:
            del _inflight[url]


async def cache_stream_tee(
    url: str, chunks: AsyncIterable[bytes], content_type: str = "application/octet-stream"
) -> AsyncIterator[bytes]:
    """Write an async byte iterator to the cache file, yielding each chunk onward.

    This is the primitive: the proxy needs the same bytes going to the browser
    and to disk in one pass, so the write loop yields rather than returning at
    the end. Nothing is buffered in memory beyond one chunk.

    Writes to a private temp file first, then renames atomically so a partial
    download never leaves a corrupt cache entry. The temp name is unique per
    writer: two writers racing on the same URL each fill their own file and
    both rename onto the same destination, which is atomic and last-one-wins.
    A shared temp name would instead let the second writer's open() truncate
    the first's in-flight file, and leave the loser deleting the winner's
    sidecar on its way out.

    The sidecar is written *before* the data rename, so a file visible to
    cache_read always has its Content-Type. Without it the proxy falls back to
    text/plain (the cache filename is a bare sha256 with no extension, so
    mimetypes cannot guess), which no browser will decode as video.

    Cleanup catches BaseException, not Exception: a browser that scrolls past
    mid-download cancels the consumer, which throws GeneratorExit in here, and
    that partial file still has to go.
    """
    path = _cache_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            async for chunk in chunks:
                fh.write(chunk)
                yield chunk
        # mkstemp creates 0600; the cache volume is routinely shared with other
        # readers, and the previous plain open() produced umask-default perms.
        await asyncio.to_thread(tmp.chmod, 0o644)
        await asyncio.to_thread(_write_meta, _meta_path(url), content_type)
        await asyncio.to_thread(tmp.replace, path)
        logger.debug(f"cache_stream_tee: cached {url} (type={content_type})")
    except BaseException:
        tmp.unlink(missing_ok=True)  # noqa: ASYNC240 — one metadata op on an error path
        raise


async def cache_stream_write(
    url: str, chunks: AsyncIterable[bytes], content_type: str = "application/octet-stream"
) -> tuple[Path, str]:
    """Drain cache_stream_tee into the cache for callers that don't want the bytes.

    Returns (path, sha256) — every media byte passes through here, so the
    content digest is accumulated for free and used by src.media.dedup to
    collapse the same picture arriving under two different URLs.
    """
    digest = hashlib.sha256()
    async for chunk in cache_stream_tee(url, chunks, content_type):
        digest.update(chunk)
    return _cache_path(url), digest.hexdigest()


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
    proxy would actually serve. .tmp entries are skipped too — they are
    in-flight downloads, not cache entries, and unlinking one would break
    the writer that owns it.
    """
    if not cache_dir.exists():
        return
    now = time.time()
    files = sorted(cache_dir.iterdir(), key=lambda p: p.stat().st_mtime)
    surviving: list[Path] = []
    for f in files:
        if f.suffix in (".meta", ".tmp"):
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
