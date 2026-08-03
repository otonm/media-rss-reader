"""Media proxy, prefetch hint, and status endpoints."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from src.config import settings
from src.db.connection import get_db
from src.media.availability import mark_url_dead_and_maybe_drop
from src.media.cache import cache_read, cache_read_meta, cache_stream_write
from src.media.dedup import record_media_hash
from src.media.prefetch import prefetch_ahead
from src.scheduler import get_http_client, get_last_opml_sync

router = APIRouter()

logger = logging.getLogger(__name__)

_DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]


@router.get("/media/proxy", response_model=None)
async def proxy_media(
    url: str = Query(...),
    item_id: str | None = Query(None),
    db: _DbDep = None,  # type: ignore[assignment]
) -> FileResponse:
    """Cache-through proxy for media files.

    On a cache hit: serve the file directly via FileResponse (zero-copy sendfile).
    On a cache miss: stream from upstream to the cache file (no in-memory buffer),
    then serve the cached file. This keeps memory usage O(chunk_size) regardless
    of the media file size.

    On upstream non-success, mark `url` as dead and (if every URL of `item_id`
    is now dead) drop the item from the DB. Errors from the helper are logged
    and swallowed -- they must not mask the 502 the client deserves.
    """
    path = cache_read(url)
    if path is not None:
        media_type = cache_read_meta(url)
        return FileResponse(str(path), media_type=media_type)

    client = get_http_client()
    try:
        async with client.stream("GET", url, follow_redirects=True, timeout=30) as response:
            if not response.is_success:
                await response.aread()
                try:
                    await mark_url_dead_and_maybe_drop(url, item_id, db)
                except Exception as exc:  # pragma: no cover
                    logger.warning(f"mark_url_dead_and_maybe_drop failed for {url}: {exc}")
                raise HTTPException(status_code=502, detail="upstream error")
            content_type = response.headers.get("content-type", "application/octet-stream")
            path, digest = await cache_stream_write(url, response.aiter_bytes(65536), content_type)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="upstream fetch failed") from exc

    # Record the content hash so a duplicate arriving under a different URL can
    # be collapsed. Swallowed on error: the user asked for bytes, and we have them.
    try:
        await record_media_hash(url, digest, db)
    except Exception as exc:  # pragma: no cover
        logger.warning(f"record_media_hash failed for {url}: {exc}")

    return FileResponse(str(path), media_type=content_type)


@router.post("/prefetch/hint")
async def prefetch_hint(
    body: dict[str, str],
    db: _DbDep = None,  # type: ignore[assignment]
) -> dict[str, str]:
    """Trigger background pre-fetching of items ahead of the given item.

    The browser calls this as a fire-and-forget POST whenever it loads a
    new page of items. The hint launches asyncio background tasks; the
    response returns immediately.
    """
    item_id = body.get("item_id", "")
    if not item_id:
        raise HTTPException(status_code=422, detail="item_id required")
    client = get_http_client()
    await prefetch_ahead(item_id, db, client)
    return {"status": "ok"}


@router.get("/status")
async def get_status(
    db: _DbDep = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Return a health/status snapshot: feed count, item counts, cache size, last sync."""
    async with db.execute("SELECT COUNT(*) FROM feeds") as cur:
        feeds_count: int = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        items_total: int = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM items WHERE seen_at IS NULL") as cur:
        items_unseen: int = (await cur.fetchone())[0]

    cache_dir = Path(settings.cache_dir)
    cache_size_mb = 0.0
    if cache_dir.exists():  # noqa: ASYNC240
        cache_size_mb = sum(f.stat().st_size for f in cache_dir.iterdir() if f.is_file()) / (1024 * 1024)  # noqa: ASYNC240

    last_sync = get_last_opml_sync()
    return {
        "feeds": feeds_count,
        "items_total": items_total,
        "items_unseen": items_unseen,
        "cache_size_mb": round(cache_size_mb, 2),
        "last_opml_sync": last_sync.isoformat() if last_sync else None,
    }
