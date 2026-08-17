"""GET /api/items and POST /api/items/{id}/seen."""

import asyncio
import datetime as dt
import logging
from typing import Any
from urllib.parse import urlsplit

import aiosqlite
from fastapi import APIRouter, HTTPException, Query

from src.db.connection import DbDep, write_transaction
from src.db.queries import ranked_page, resolve_anchor
from src.logging_utils import loggable
from src.media.cache import cache_name, cache_names_present
from src.media.normalize import item_slides, media_key
from src.timing import timer

logger = logging.getLogger(__name__)
router = APIRouter()


def _row_to_item(row: aiosqlite.Row, cached_names: set[str]) -> dict[str, Any]:
    """Convert an items row to the API shape, expanding media_json to `media`.

    `rn` is part of the response because the client echoes it back as after_rn;
    without it the cursor silently skips items whenever a feed gains a row that
    ranks before the anchor.

    `cached` tells the browser which items are already on disk so it can
    download those first — they decode in milliseconds, while a miss waits on
    the origin. It is a hint, not a promise: an entry can be evicted, or
    warmed, moments after the response goes out.
    """
    # Only the primary media_url is checked, so a gallery counts as cached once
    # its first slide is on disk. That matches the browser queue, which
    # prioritises items it can paint rather than galleries that are fully warm.
    item = dict(row)
    item.pop("media_json")
    item["media"] = item_slides(row)
    item["cached"] = cache_name(item["media_url"]) in cached_names
    return item


@router.get("/items")
async def list_items(
    unseen: bool = False,
    after_id: str | None = None,
    after_rn: int | None = Query(None, ge=1),
    # The browser sends size=FEED_INITIAL_COUNT, so this 200 and the bound
    # _load_settings enforces on that setting (src/config.py) are one quantity:
    # a larger FEED_INITIAL_COUNT would 422 here. Change both together.
    size: int = Query(50, ge=1, le=200),
    *,
    db: DbDep,
) -> list[dict[str, Any]]:
    """Return a keyset-paginated, interleaved list of media items.

    Ranking: the window function assigns rn per feed over the FULL items set,
    with the seen filter applied outside the CTE, so marking an item seen drops
    it from the result without renumbering any other item. Taking rn from the
    window rather than deriving it by counting is what keeps a NULL pub_date
    harmless: ROW_NUMBER sorts NULLs first and ranks them 1..k, while a
    row-value comparison with a NULL member evaluates to NULL in SQLite and so
    drops exactly those rows. A count-based OFFSET is wrong here for a second
    reason: what the client counted over stops being a prefix of the server's
    ranking as soon as any item changes seen state.

    Cursor: the id of the last item the client holds, plus the rank that was
    issued with it (after_rn). rn is not itself a cursor — it is recomputed per
    request, so it moves under an outstanding cursor in both directions: a
    prune lowers it, a row inserted with an older pub_date raises it. So the
    anchor's (rn, feed_id, id) is re-resolved from the same CTE that orders
    this page and the page is bounded by min(after_rn, the resolved rank).
    Taking the lower bound means a raised rank reopens the window instead of
    skipping every undelivered row between the two ranks; the reopened rows
    return as duplicates, which the client's known-set guard drops. Every feed
    that gained a row below the bound contributes such duplicates, so their
    number scales with insertions beneath the cursor across all feeds, not with
    the anchor's feed alone.

    Because a page can come back entirely as duplicates, the client must
    re-anchor on the response's own last row whenever a page is non-empty
    (src/static/item-store.js). Deriving the next cursor only from rows it
    appended would leave after_id/after_rn unchanged, so the next request
    repeats this one and pagination stalls until reload.

    Two things this does not recover. The anchor's rank is read by one
    statement and the page by another, so a feed refresh landing between them
    can still shift it. And min() only reopens rows ahead of the cursor: a row
    inserted behind it — typically an undated entry, which ROW_NUMBER ranks
    first in its feed — is not delivered until the client reloads from the top.

    after_rn is optional, so a client that does not send it still pages on the
    resolved rank alone. When sent it is bounded to >= 1, because ROW_NUMBER
    starts at 1 and a rank of 0 admits the whole table (see below).

    An anchor that no longer exists — pruned, or its feed left the OPML and the
    rows cascaded — answers 410. Resolving it to a rank of 0 instead would make
    `(rn, feed_id, id) > (0, ...)` match everything: page one of the global
    interleave, which the client's known-set filter discards, leaving a cursor
    that never advances.

    src/media/prefetch.py warms ahead using the same fragments (src/db/queries.py)
    but resolves its own anchor, so the window it warms can sit a few rows off
    the one served here. That is a hint, not a contract.
    """
    logger.debug(f"list_items unseen={unseen} after_id={loggable(after_id)} after_rn={after_rn} size={size}")
    anchor = None
    if after_id is not None:
        anchor_elapsed = timer()
        anchor = await resolve_anchor(db, after_id)
        anchor_ms = anchor_elapsed()
        if anchor is None:
            logger.info(f"list_items: 410, cursor anchor {loggable(after_id)} no longer exists (db={anchor_ms:.1f}ms)")
            raise HTTPException(status_code=410, detail="cursor expired")
        logger.debug(f"list_items: anchor {loggable(after_id)} resolved to rn={anchor['rn']} (db={anchor_ms:.1f}ms)")

    db_elapsed = timer()
    rows = await ranked_page(
        db,
        columns="id, feed_id, title, media_url, media_type, media_json, pub_date, fetched_at, seen_at",
        unseen=unseen,
        size=size,
        after=anchor,
        after_rn=after_rn,
    )
    db_ms = db_elapsed()
    cache_elapsed = timer()
    wanted = {cache_name(row["media_url"]) for row in rows}
    cached_names = await asyncio.to_thread(cache_names_present, wanted) if wanted else set()
    logger.debug(f"list_items: {len(cached_names)}/{len(wanted)} media cached, checked in {cache_elapsed():.1f}ms")
    items = [_row_to_item(row, cached_names) for row in rows]
    # The cached count is how many of these the browser can paint instantly; a
    # low ratio is what a sluggish scroll looks like from here, and it is what
    # the UI_DEBUG overlay's HIT/MISS line reports.
    cached_count = sum(1 for i in items if i["cached"])
    logger.debug(f"list_items returned {len(items)} item(s), {cached_count} cached on disk; db={db_ms:.1f}ms")
    return items


def _usable_media_url(url: str | None) -> bool:
    """Whether a client-supplied media URL may seed seen_media on its own.

    Only consulted once the item row is already gone, so nothing on this side
    can corroborate it. Requiring a real http(s) URL keeps a stray or truncated
    value from landing a key in seen_media that suppresses unrelated media.
    """
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


@router.post("/items/{item_id}/seen")
async def mark_seen(
    item_id: str,
    db: DbDep,
    media_url: str | None = None,
) -> dict[str, str]:
    """Mark an item as seen and return the timestamp.

    `media_url` is the browser's own copy of the item's media URL. It exists so
    the durable seen record survives the row disappearing: prune_items evicts
    oldest-first while this module serves oldest-first, so a refresh cycle
    routinely deletes a row between the page being served and the reader
    scrolling past it. Without it the UPDATE matches nothing, the request 404s,
    and seen_media — the record whose entire job is to outlive pruning — never
    learns the item was seen.

    The stored row wins whenever it exists; the parameter is consulted only
    when it is gone, and only if it passes _usable_media_url. A request that
    sends neither still 404s, so a genuinely unknown id is still an error.

    UPDATE ... RETURNING (SQLite >= 3.35) updates the row and reads back
    media_url + seen_at in one statement. One Python timestamp is bound to both
    items.seen_at and seen_media.seen_at so the two cannot diverge.

    The UPDATE and the insert both run inside write_transaction, which holds
    the shared connection's lock and rolls back on any exit that is not a clean
    commit — including the 404 and including cancellation.

    The browser marks the item locally and sends this as a beacon whose
    response it discards; the timestamp is returned for symmetry with that
    optimistic local mark.
    """
    logger.debug(f"mark_seen item_id={loggable(item_id)}")
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    try:
        async with write_transaction(db):
            update_elapsed = timer()
            async with db.execute(
                "UPDATE items SET seen_at = ? WHERE id = ? RETURNING media_url, seen_at",
                (now, item_id),
            ) as cur:
                row = await cur.fetchone()
            update_ms = update_elapsed()
            if row is None and not _usable_media_url(media_url):
                # DEBUG rather than INFO: the browser fires this as a beacon and
                # discards the response, so a 404 here is routine and not a status
                # change an operator needs to see.
                logger.debug(f"mark_seen item_id={loggable(item_id)} not found")
                raise HTTPException(status_code=404, detail="Not found")
            if row is None:
                logger.debug(
                    f"mark_seen item_id={loggable(item_id)} row already pruned; "
                    f"recording seen_media from the client's media_url"
                )
            seen_url = row["media_url"] if row is not None else media_url
            insert_elapsed = timer()
            await db.execute(
                "INSERT OR REPLACE INTO seen_media (media_key, seen_at) VALUES (?, ?)",
                (media_key(seen_url), now),
            )
            insert_ms = insert_elapsed()
    except HTTPException:
        raise
    except Exception:
        logger.warning(
            f"mark_seen item_id={loggable(item_id)}: write failed, rolling back the seen mark", exc_info=True
        )
        raise

    seen_at = row["seen_at"] if row is not None else now
    logger.debug(
        f"mark_seen item_id={loggable(item_id)} seen_at={seen_at} update={update_ms:.1f}ms insert={insert_ms:.1f}ms"
    )
    return {"seen_at": seen_at}
