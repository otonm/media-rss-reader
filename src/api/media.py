"""Media proxy and prefetch hint endpoints."""

import asyncio
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import Message, Receive, Scope, Send

from src.db.connection import DbDep, write_transaction
from src.http_client import HttpDep
from src.logging_utils import loggable
from src.media.availability import is_known_media_url, mark_url_dead_and_maybe_drop
from src.media.cache import cache_lookup
from src.media.fetch import NonMediaUpstreamError, UpstreamError, open_upstream, tee_to_cache
from src.media.prefetch import prefetch_ahead
from src.request_id import current_request_id
from src.timing import timer

router = APIRouter()

logger = logging.getLogger(__name__)


class PrefetchHint(BaseModel):
    """The only request body the API accepts.

    max_length because item_id is a sha256 hex string everywhere it is produced
    and neither uvicorn nor FastAPI caps body size by default — without it a
    100 MB id would be read, validated and bound as a SQL parameter before the
    404.

    extra="forbid" because a misspelled field (`unseen_only=true`) would
    otherwise be accepted silently and warm items under the default filter
    instead of the one the client paged with.

    `unseen` is a plain bool, not StrictBool: /api/items coerces `?unseen=1`,
    and a hint that rejects what the page it mirrors accepts would 422 into the
    browser's `.catch(() => {})`, where nothing reports it.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: Annotated[str, Field(min_length=1, max_length=128)]
    # Matches /api/items' `unseen: bool = False`. prefetch_ahead requires the
    # filter from its caller, so this is the single place the default is written.
    unseen: bool = False


class CacheFileResponse(FileResponse):
    """FileResponse that answers 503 when the cached file vanished mid-request.

    evict() runs after every refresh cycle and can unlink an entry between
    cache_lookup and Starlette's own os.stat. Starlette stats and opens the
    file by path, so holding a descriptor here would not close the window, and
    the RuntimeError it raises surfaces after proxy_media has returned —
    outside every except clause the handler has — reaching the browser as a 500
    that also logs the container's cache path.

    503 with Retry-After instead: the next request takes the miss path and
    refetches.

    The window is narrowed, not closed. Closing it needs an in-flight registry
    that evict() consults before unlinking; evict is the only unlinker and runs
    in this process, so that stays a contained change if these 503s are ever
    observed in practice.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = False

        async def _send(message: Message) -> None:
            nonlocal started
            started = True
            await send(message)

        try:
            await super().__call__(scope, receive, _send)
        except RuntimeError:
            # Only the vanished-file case belongs to us: if bytes already went out,
            # or the file is still there, the error came from something else.
            if started or Path(self.path).exists():  # noqa: ASYNC240 — one stat on a rare error path
                raise
            logger.warning(f"proxy_media: cached file for {self.path} vanished before send, answering 503")
            await Response(status_code=503, headers={"Retry-After": "1"})(scope, receive, send)


@router.get("/media/proxy")
async def proxy_media(
    url: str = Query(...),
    item_id: str | None = Query(None),
    *,
    db: DbDep,
    client: HttpDep,
) -> Response:
    """Cache-through proxy for media files.

    On a cache hit: serve the file directly via FileResponse (zero-copy sendfile,
    and Range-capable, which is what makes a cached video seekable).

    On a cache miss: stream from upstream straight to the browser while filling
    the cache in the same pass, so the browser starts painting on the first
    chunk instead of staring at a blank screen for the whole upstream transfer.
    Memory stays at O(chunk_size) either way.

    On upstream non-success, `url` is marked dead and a fully-dead item is
    dropped from the DB before the 502 goes out.

    Limitation: the miss path uses StreamingResponse and does not honour Range
    requests, so seeking an uncached video (or Safari's initial byte-range
    probe) restarts from zero. The hit path handles Range correctly, so the
    same video is seekable on second view once cached. That is the accepted
    trade-off for painting the first frame immediately on a miss.
    """
    logger.debug(f"proxy_media url={loggable(url)} item_id={loggable(item_id)}")
    # The cache lookup goes first: it is one stat against a sha256-derived name,
    # while the gate's second tier is `media_json LIKE '%...%'`, which no index
    # can serve — a full scan of items, for every gallery slide, on the same
    # aiosqlite worker thread /api/items queues on. A hit needs no gate: the key
    # is sha256(url) so it cannot escape CACHE_DIR, and a URL can only be in the
    # cache because it passed the gate on an earlier request.
    hit = await asyncio.to_thread(cache_lookup, url)
    if hit is not None:
        path, media_type = hit
        logger.debug(f"proxy_media: HIT {loggable(url)} -> {path.name} (type={loggable(media_type)})")
        return CacheFileResponse(path, media_type=media_type)

    gate_elapsed = timer()
    known = await is_known_media_url(url, db)
    logger.debug(f"proxy_media: url gate for {loggable(url)} -> {known} in {gate_elapsed():.1f}ms")
    if not known:
        logger.debug(f"proxy_media: refusing unknown url {loggable(url)}")
        raise HTTPException(status_code=404, detail="not a known media url")

    logger.debug(f"proxy_media: MISS {loggable(url)} (item_id={loggable(item_id)}), streaming from upstream")
    try:
        upstream_elapsed = timer()
        response, content_type = await open_upstream(url, item_id, client, request_id=current_request_id())
    except (UpstreamError, NonMediaUpstreamError) as exc:
        # Every raise site in open_upstream/_check_url/tee_to_cache already warns
        # once, at the point that knows the reason, including
        # NonMediaUpstreamError's actual content type. So this line only records
        # that the request became a 502 — plus the duration, which is not
        # available there and is what separates an instant connection refusal
        # from a 30s read timeout.
        #
        # NonMediaUpstreamError is deliberately not an UpstreamError, but both
        # mark the URL dead and drop the item, so they share this line and
        # differ only in the 502 detail below: a non-media response is not a
        # failed fetch, and the client is told which it was.
        logger.debug(
            f"proxy_media: 502 for {loggable(url)} (item_id={loggable(item_id)}) in {upstream_elapsed():.1f}ms — {exc}"
        )
        detail = "upstream content type not media" if isinstance(exc, NonMediaUpstreamError) else "upstream error"
        raise HTTPException(status_code=502, detail=detail) from exc
    except Exception as exc:
        logger.exception(
            f"proxy_media: upstream fetch failed for {loggable(url)} in {upstream_elapsed():.1f}ms: "
            f"{type(exc).__name__}: {exc}"
        )
        raise HTTPException(status_code=502, detail="upstream fetch failed") from exc

    logger.debug(
        f"proxy_media: MISS ok {loggable(url)} -> {response.status_code} type={loggable(content_type)} "
        f"upstream={upstream_elapsed():.1f}ms"
    )
    return StreamingResponse(
        tee_to_cache(url, response, content_type, request_id=current_request_id()),
        media_type=content_type,
    )


@router.post("/media/failed")
async def report_media_failed(
    url: str = Query(...),
    item_id: str | None = Query(None),
    *,
    db: DbDep,
) -> dict[str, int]:
    """Record media the browser could not load, and drop the item it belongs to.

    The browser gives up on a download after MEDIA_LOAD_TIMEOUT_S and reports it
    here. A genuine upstream failure answers 502 and is surfaced by the media
    element's `error` event within a second, so this endpoint mostly hears about
    media that is slow rather than gone.

    That is a deliberate policy choice by the operator, and it is stricter than
    open_upstream's: that path only marks dead on a permanent answer — a
    PERMANENT_STATUSES code or a non-media body — because dropping on transient
    failures erases posts that would have loaded later. A timeout cannot tell
    gone from slow, so MEDIA_LOAD_TIMEOUT_S is the knob to raise if usable
    posts start disappearing.

    Gated on is_known_media_url for the same reason proxy_media is: this deletes
    rows on the client's say-so, so a URL the database has never heard of must
    not be able to put anything into dead_urls.
    """
    logger.debug(f"report_media_failed url={loggable(url)} item_id={loggable(item_id)}")
    if not await is_known_media_url(url, db):
        logger.debug(f"report_media_failed: refusing unknown url {loggable(url)}")
        raise HTTPException(status_code=404, detail="not a known media url")

    # write_transaction because get_db shares one connection across requests and
    # sqlite3's implicit transaction is per connection: even a single-statement
    # write needs the lock, or it commits whatever another coroutine has in
    # flight. mark_url_dead_and_maybe_drop doesn't commit; this call owns the
    # transaction boundary.
    async with write_transaction(db):
        dropped = await mark_url_dead_and_maybe_drop(url, item_id, db)

    logger.info(f"report_media_failed: {loggable(url)} marked dead, dropped {len(dropped)} item(s)")
    return {"dropped": len(dropped)}


@router.post("/prefetch/hint")
async def prefetch_hint(
    body: PrefetchHint,
    db: DbDep,
    client: HttpDep,
) -> dict[str, str]:
    """Trigger background pre-fetching of items ahead of the given item.

    The browser calls this as a fire-and-forget POST whenever it loads a new
    page of items. The warm tasks run in the background and are not awaited,
    but prefetch_ahead's window-function queries over the items table are: the
    anchor lookup, then the page of items to warm — the second is skipped when
    the backlog cap is already reached. That wait is the whole cost of this
    endpoint and what the logged db= measures.
    """
    logger.debug(f"prefetch_hint item_id={loggable(body.item_id)} unseen={body.unseen}")
    elapsed = timer()
    queued = await prefetch_ahead(body.item_id, db, client, unseen=body.unseen, request_id=current_request_id())
    if queued is None:
        logger.info(f"prefetch_hint: 404, item {loggable(body.item_id)} not found (db={elapsed():.1f}ms)")
        raise HTTPException(status_code=404, detail="item not found")
    logger.debug(f"prefetch_hint item_id={loggable(body.item_id)}: queued {queued} warm task(s); db={elapsed():.1f}ms")
    return {"status": "ok"}
