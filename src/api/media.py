"""Media proxy and prefetch hint endpoints."""

import logging

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse, StreamingResponse

from src.api.schemas import PrefetchHint, PrefetchHintResponse
from src.db.connection import _DbDep
from src.media.availability import is_known_media_url
from src.media.cache import cache_read, cache_read_meta
from src.media.fetch import UpstreamError, open_upstream, tee_to_cache
from src.media.prefetch import prefetch_ahead
from src.scheduler import get_http_client

router = APIRouter()

logger = logging.getLogger(__name__)


@router.get("/media/proxy", response_model=None)
async def proxy_media(
    url: str = Query(...),
    item_id: str | None = Query(None),
    *,
    db: _DbDep,
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

    Limitation: the miss path uses StreamingResponse and does not honour Range
    requests, so seeking an uncached video (or Safari's initial byte-range probe)
    restarts from zero. The hit path (FileResponse) handles Range correctly, so
    the same video is seekable on second view once cached. Documented trade-off:
    streaming misses through is what prevents the black-screen stall on first
    paint (F7).
    """
    if not await is_known_media_url(url, db):
        logger.debug(f"proxy_media: refusing unknown url {url}")
        raise HTTPException(status_code=404, detail="not a known media url")
    path = cache_read(url)
    if path is not None:
        try:
            # cache_read only checks existence; FileResponse opens the file when
            # the response is *sent*, after this function returned. evict() runs
            # after every refresh cycle, and losing that race was a 500 for media
            # the miss path below would have refetched (R2).
            stat_result = path.stat()
        except FileNotFoundError:
            logger.debug(f"proxy_media: {url} evicted between check and send, falling through to upstream")
        else:
            media_type = cache_read_meta(url) or "application/octet-stream"
            logger.debug(f"proxy_media: HIT {url} -> {path.name} (type={media_type})")
            return FileResponse(
                path,
                media_type=media_type,
                stat_result=stat_result,
                headers={"X-Content-Type-Options": "nosniff"},
            )

    logger.debug(f"proxy_media: MISS {url} (item_id={item_id}), streaming from upstream")
    client = get_http_client()
    try:
        response = await open_upstream(url, item_id, client)
    except UpstreamError as exc:
        # A failed user-visible request, and on 404/410 a destructive state
        # change (the URL marked dead, a fully-dead item dropped). This used to
        # be debug, invisible at the default info level, while the *less*
        # consequential handler below logged at warning (R10).
        logger.warning(f"proxy_media: 502 for {url} (item_id={item_id}) — {exc}")
        raise HTTPException(status_code=502, detail="upstream error") from exc
    except Exception as exc:
        logger.warning(f"proxy_media: upstream fetch failed for {url}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=502, detail="upstream fetch failed") from exc

    content_type = response.headers.get("content-type", "application/octet-stream")
    return StreamingResponse(
        tee_to_cache(url, response),
        media_type=content_type,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/prefetch/hint")
async def prefetch_hint(
    body: PrefetchHint,
    db: _DbDep,
) -> PrefetchHintResponse:
    """Trigger background pre-fetching of items ahead of the given item.

    The browser calls this as a fire-and-forget POST whenever it loads a
    new page of items. The hint launches asyncio background tasks; the
    response returns immediately.
    """
    item_id = body.item_id
    unseen = body.unseen
    logger.debug(f"prefetch_hint item_id={item_id} unseen={unseen}")
    if not item_id:
        logger.debug("prefetch_hint: 422, no item_id in body")
        raise HTTPException(status_code=422, detail="item_id required")
    async with db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)) as cur:
        if await cur.fetchone() is None:
            logger.debug(f"prefetch_hint: 404, item {item_id} not found")
            raise HTTPException(status_code=404, detail="item not found")
    client = get_http_client()
    queued = await prefetch_ahead(item_id, db, client, unseen=unseen)
    logger.debug(f"prefetch_hint item_id={item_id}: queued {queued} warm task(s)")
    return {"status": "ok"}
