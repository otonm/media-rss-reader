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
from src.logging_utils import loggable

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


def cache_name(url: str) -> str:
    """The cache filename for `url` (sha256 hex, no extension).

    Single source of the naming contract: items.py compares this against
    `cache_names_present()`'s set to set the `cached` hint, so the two must
    not drift. Implemented as `_cache_path(url).name` so a scheme change here
    flips both sides at once.
    """
    return _cache_path(url).name


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
    safe_url = loggable(url)
    if first:
        logger.debug(f"download_claim: claimed {safe_url} ({len(_inflight)} URL(s) in flight)")
    else:
        logger.debug(f"download_claim: {safe_url} already claimed by {_inflight[url] - 1} other downloader(s)")
    try:
        yield first
    finally:
        remaining = _inflight[url] - 1
        if remaining:
            _inflight[url] = remaining
        else:
            del _inflight[url]
            logger.debug(f"download_claim: released {safe_url} ({len(_inflight)} URL(s) still in flight)")


async def cache_stream_tee(
    url: str,
    chunks: AsyncIterable[bytes],
    content_type: str = "application/octet-stream",
    request_id: str | None = None,
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
    written = 0
    safe_url = loggable(url)
    logger.debug(f"cache_stream_tee: start {safe_url} -> {tmp.name} (type={content_type}) (request_id={request_id})")
    try:
        with os.fdopen(fd, "wb") as fh:
            async for chunk in chunks:
                fh.write(chunk)
                written += len(chunk)
                yield chunk
        # mkstemp creates 0600; the cache volume is routinely shared with other
        # readers, and the previous plain open() produced umask-default perms.
        await asyncio.to_thread(tmp.chmod, 0o644)
        await asyncio.to_thread(_write_meta, _meta_path(url), content_type)
        await asyncio.to_thread(tmp.replace, path)
        logger.debug(
            f"cache_stream_tee: cached {safe_url} ({written} bytes, type={content_type}) as {path.name} "
            f"(request_id={request_id})"
        )
    except BaseException as exc:
        tmp.unlink(missing_ok=True)  # noqa: ASYNC240 — one metadata op on an error path
        logger.debug(
            f"cache_stream_tee: discarded partial {safe_url} after {written} bytes "
            f"({type(exc).__name__}); temp file {tmp.name} removed (request_id={request_id})"
        )
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


def cache_lookup(url: str) -> tuple[Path, str] | None:
    """Return (path, content_type) for a cached URL, or None on a miss.

    One blocking call for the proxy's hit path instead of three. The stat is
    what makes a miss unambiguous — cache_read only checked existence, so a file
    evicted between the check and the stat was a separate race the caller had to
    handle — but its result is deliberately not returned: forwarding it to
    FileResponse suppresses Starlette's own os.stat, which is the check that
    fails before any bytes go out (R2).
    """
    path = _cache_path(url)
    try:
        path.stat()
    except FileNotFoundError:
        return None
    return path, cache_read_meta(url) or "application/octet-stream"


def cache_names_present(names: set[str]) -> set[str]:
    """Which of `names` are on disk. One thread hop, len(names) stats.

    The previous shape iterated the whole cache directory: Path.iterdir yields
    Paths with no cached stat, so is_file() issued one stat per entry — data
    files and .meta sidecars both, ~1000 at the default cache_max_items — to
    answer at most `size` questions, where the frontend's first page is 10.
    Batching per-row checks into one to_thread keeps the single event-loop hop
    (F18) and makes the cost scale with the page rather than the cache.
    """
    cache_dir = Path(settings.cache_dir)
    try:
        return {name for name in names if (cache_dir / name).is_file()}
    except FileNotFoundError:
        return set()


def _evict_sync(cache_dir: Path, max_age_secs: float, max_items: int, max_bytes: int) -> None:
    """Blocking eviction logic — run via asyncio.to_thread to keep the event loop free.

    Each data file and its .meta sidecar share the same mtime (written
    together), so evicting one takes the other along. Counting is by data
    file only; .meta entries are skipped so the count matches what the
    proxy would actually serve. .tmp entries are skipped too — they are
    in-flight downloads, not cache entries, and unlinking one would break
    the writer that owns it. A third pass trims by total bytes: counting
    files cannot bound a directory of multi-gigabyte videos.
    """
    if not cache_dir.exists():
        logger.debug(f"_evict_sync: cache dir {cache_dir} does not exist, nothing to do")
        return
    now = time.time()
    files = sorted(cache_dir.iterdir(), key=lambda p: p.stat().st_mtime)
    inflight = sum(1 for f in files if f.suffix == ".tmp")
    surviving: list[Path] = []
    by_age = 0
    for f in files:
        if f.suffix in (".meta", ".tmp"):
            continue
        if now - f.stat().st_mtime > max_age_secs:
            logger.debug(f"Evicting cache file {f} due to age")
            f.unlink(missing_ok=True)
            f.with_suffix(".meta").unlink(missing_ok=True)
            by_age += 1
        else:
            surviving.append(f)
    by_count = 0
    while len(surviving) > max_items:
        logger.debug(f"Evicting cache file {surviving[0]} due to count limit")
        head = surviving.pop(0)
        head.unlink(missing_ok=True)
        head.with_suffix(".meta").unlink(missing_ok=True)
        by_count += 1
    by_bytes = 0
    if max_bytes:
        total = sum(f.stat().st_size for f in surviving)
        while surviving and total > max_bytes:
            head = surviving.pop(0)
            total -= head.stat().st_size
            logger.debug(f"Evicting cache file {head} due to byte budget")
            head.unlink(missing_ok=True)
            head.with_suffix(".meta").unlink(missing_ok=True)
            by_bytes += 1
    logger.debug(
        f"_evict_sync: {len(surviving)} entries remain (limit {max_items}); "
        f"evicted {by_age} by age, {by_count} by count, {by_bytes} by bytes; "
        f"skipped {inflight} in-flight .tmp"
    )


async def evict() -> None:
    """Evict stale or excess cache entries without blocking the event loop.

    Step 1: delete files older than CACHE_MAX_AGE_HOURS.
    Step 2: if the surviving count still exceeds CACHE_MAX_ITEMS,
            delete the oldest files (by mtime) until under the limit.
    Step 3: if the surviving total still exceeds CACHE_MAX_BYTES, delete the
            oldest files until under the budget.
    """
    cache_dir = Path(settings.cache_dir)
    await asyncio.to_thread(
        _evict_sync,
        cache_dir,
        settings.cache_max_age_hours * 3600,
        settings.cache_max_items,
        settings.cache_max_bytes,
    )
