"""Media proxy and prefetch hint endpoints."""

import asyncio
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import Message, Receive, Scope, Send

from src.db.connection import DbDep
from src.http_client import HttpDep
from src.logging_utils import loggable
from src.media.availability import is_known_media_url
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
    and neither uvicorn nor FastAPI caps body size by default — a 100 MB id
    would be read, validated and bound as a SQL parameter before the 404.

    extra="forbid" because a client typo (`unseen_only=true`) was otherwise
    accepted silently with the default filter, re-arming the R12 mismatch this
    model exists to prevent.

    `unseen` is a plain bool, not StrictBool: /api/items coerces `?unseen=1`, and
    a hint that rejects what the page it mirrors accepts is a 422 nothing logs
    and the browser's `.catch(() => {})` does not see.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: Annotated[str, Field(min_length=1, max_length=128)]
    # Matches /api/items' `unseen: bool = False`. prefetch_ahead has no default
    # of its own any more, so this is the single place the filter is written.
    unseen: bool = False


class CacheFileResponse(FileResponse):
    """FileResponse that answers 503 when the cached file vanished mid-request.

    evict() runs after every refresh cycle and can unlink an entry between our
    cache_lookup and Starlette's own os.stat. Starlette stats at
    responses.py:350 and opens at 392, both by path, so no descriptor we hold
    closes the window — and its RuntimeError is raised after proxy_media has
    returned, outside every except clause the handler has. That reached the
    browser as a 500 with the container's cache path in the error log.

    503 with Retry-After: the next request takes the miss path and refetches.

    ponytail: the window is narrowed, not closed. Closing it needs an in-flight
    registry that evict() consults before unlinking; evict is the only unlinker
    and runs in this process, so that is a contained change if the 503s are ever
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
    # The cache lookup goes first. It is one stat against a sha256-derived name;
    # the gate's second tier is `media_json LIKE '%...%'`, which no index can
    # serve, so it was running a full scan of items for every slide of every
    # gallery — on the same aiosqlite worker thread /api/items queues on. A hit
    # needs no gate: the key is sha256(url), so it cannot escape CACHE_DIR, and
    # a URL can only be in the cache because it passed the gate earlier.
    hit = await asyncio.to_thread(cache_lookup, url)
    if hit is not None:
        path, media_type = hit
        logger.debug(f"proxy_media: HIT {loggable(url)} -> {path.name} (type={media_type})")
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
        response, content_type = await open_upstream(url, item_id, client)
    except UpstreamError as exc:
        # Every UpstreamError raise site in open_upstream/_check_url/tee_to_cache
        # now warns once, at the point that knows why (M6 follow-up) — R10's
        # original reason for warning again here (those sites used to be silent)
        # no longer applies, so this just records that the request became a 502.
        # The duration still matters at this level: an instant connection
        # refusal and a 30s read timeout otherwise log identically.
        logger.debug(
            f"proxy_media: 502 for {loggable(url)} (item_id={loggable(item_id)}) in {upstream_elapsed():.1f}ms — {exc}"
        )
        raise HTTPException(status_code=502, detail="upstream error") from exc
    except NonMediaUpstreamError as exc:
        # Deliberately not an UpstreamError: nothing was cached, nothing was
        # marked dead, and the condition can flip back. open_upstream has
        # already logged the real content type at WARNING one line up, so this
        # only records that the request became a 502 — and the client is told
        # what actually happened rather than that the fetch failed, which it
        # did not.
        logger.debug(
            f"proxy_media: 502 for {loggable(url)} (item_id={loggable(item_id)}) in {upstream_elapsed():.1f}ms — {exc}"
        )
        raise HTTPException(status_code=502, detail="upstream content type not media") from exc
    except Exception as exc:
        logger.exception(
            f"proxy_media: upstream fetch failed for {loggable(url)} in {upstream_elapsed():.1f}ms: "
            f"{type(exc).__name__}: {exc}"
        )
        raise HTTPException(status_code=502, detail="upstream fetch failed") from exc

    logger.debug(
        f"proxy_media: MISS ok {loggable(url)} -> {response.status_code} type={content_type} "
        f"upstream={upstream_elapsed():.1f}ms"
    )
    return StreamingResponse(
        tee_to_cache(url, response, content_type, request_id=current_request_id()),
        media_type=content_type,
    )


@router.post("/prefetch/hint")
async def prefetch_hint(
    body: PrefetchHint,
    db: DbDep,
    client: HttpDep,
) -> dict[str, str]:
    """Trigger background pre-fetching of items ahead of the given item.

    The browser calls this as a fire-and-forget POST whenever it loads a new
    page of items. The hint launches asyncio background tasks and does not wait
    for them, but it does await prefetch_ahead's two window-function queries
    over the items table, which are the cost of this endpoint and what db=
    measures.
    """
    logger.debug(f"prefetch_hint item_id={loggable(body.item_id)} unseen={body.unseen}")
    elapsed = timer()
    queued = await prefetch_ahead(body.item_id, db, client, unseen=body.unseen, request_id=current_request_id())
    if queued is None:
        logger.info(f"prefetch_hint: 404, item {loggable(body.item_id)} not found (db={elapsed():.1f}ms)")
        raise HTTPException(status_code=404, detail="item not found")
    logger.debug(f"prefetch_hint item_id={loggable(body.item_id)}: queued {queued} warm task(s); db={elapsed():.1f}ms")
    return {"status": "ok"}
