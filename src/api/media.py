"""Media proxy, prefetch hint, and status endpoints."""

import logging
from pathlib import Path
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse, StreamingResponse

from src.config import settings
from src.db.connection import get_db
from src.media.cache import cache_read, cache_read_meta
from src.media.fetch import UpstreamError, open_upstream, tee_to_cache
from src.media.prefetch import prefetch_ahead
from src.scheduler import get_http_client, get_last_opml_sync

router = APIRouter()

logger = logging.getLogger(__name__)

_DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]


@router.get("/media/proxy", response_model=None)
async def proxy_media(
    url: str = Query(...),
    item_id: str | None = Query(None),
) -> Response:
    """Cache-through proxy for media files.

    On a cache hit: serve the file directly via FileResponse (zero-copy sendfile,
    and Range-capable, which is what makes a cached video seekable).

    On a cache miss: stream from upstream straight to the browser while filling
    the cache in the same pass. The browser starts painting on the first chunk.
    Downloading to disk first and only then replying meant a full-screen spinner
    for the whole upstream transfer, which is the black screen users saw.
    Memory stays at O(chunk_size) either way.

    On upstream non-success, `url` is marked dead and a fully-dead item is
    dropped from the DB before the 502 goes out.
    """
    path = cache_read(url)
    if path is not None:
        media_type = cache_read_meta(url)
        logger.debug(f"proxy_media: HIT {url} -> {path.name} (type={media_type})")
        return FileResponse(str(path), media_type=media_type)

    logger.debug(f"proxy_media: MISS {url} (item_id={item_id}), streaming from upstream")
    client = get_http_client()
    try:
        response = await open_upstream(url, item_id, client)
    except UpstreamError as exc:
        logger.debug(f"proxy_media: 502 for {url} — {exc}")
        raise HTTPException(status_code=502, detail="upstream error") from exc
    except Exception as exc:
        logger.warning(f"proxy_media: upstream fetch failed for {url}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=502, detail="upstream fetch failed") from exc

    content_type = response.headers.get("content-type", "application/octet-stream")
    return StreamingResponse(tee_to_cache(url, response), media_type=content_type)


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
