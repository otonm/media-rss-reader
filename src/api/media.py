from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from src.config import settings
from src.db.connection import get_db
from src.media.cache import cache_read, cache_write
from src.media.prefetch import prefetch_ahead
from src.scheduler import get_http_client, get_last_opml_sync

router = APIRouter()


@router.get("/media/proxy", response_model=None)
async def proxy_media(url: str = Query(...)) -> FileResponse | StreamingResponse:  # noqa: B008
    path = cache_read(url)
    if path is not None:
        return FileResponse(str(path))

    client = get_http_client()
    try:
        response = await client.get(url, follow_redirects=True, timeout=30)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="upstream fetch failed") from exc
    if not response.is_success:
        raise HTTPException(status_code=502, detail="upstream error")

    data = await response.aread()
    await cache_write(url, data)
    content_type = response.headers.get("content-type", "application/octet-stream")

    async def _stream() -> bytes:
        yield data

    return StreamingResponse(_stream(), media_type=content_type)


@router.post("/prefetch/hint")
async def prefetch_hint(
    body: dict[str, str],
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
) -> dict[str, str]:
    item_id = body.get("item_id", "")
    if not item_id:
        raise HTTPException(status_code=422, detail="item_id required")
    client = get_http_client()
    await prefetch_ahead(item_id, db, client)
    return {"status": "ok"}


@router.get("/status")
async def get_status(
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
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
