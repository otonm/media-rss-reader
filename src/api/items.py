"""GET /api/items and POST /api/items/{id}/seen."""

import asyncio
import datetime as dt
import hashlib
import json
import logging
import time
from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Query

from src.api.schemas import ItemOut, SeenResponse
from src.db.connection import _DbDep
from src.db.queries import INTERLEAVE_ORDER_BY, RANKED_ITEMS_CTE
from src.media.cache import cache_present_names
from src.media.normalize import media_key

logger = logging.getLogger(__name__)
router = APIRouter()


def _row_to_item(row: aiosqlite.Row, cached_names: set[str]) -> dict[str, Any]:
    """Convert an items row to the API shape, expanding media_json to `media`.

    Rows predating migration v5 have media_json NULL; they fall back to a
    1-element list built from media_url/media_type so the frontend always
    receives a `media` array.

    `cached` tells the browser which items are already on disk so it can
    download those first — they decode in milliseconds, while a miss waits on
    the origin. It is a hint, not a promise: an entry can be evicted, or
    warmed, moments after the response goes out.
    """
    # ponytail: cached = slide[0] hit; queue prioritises paintable items, not
    # full-gallery warmth (F9). Checking all slides would mark a gallery whose
    # first slide is on disk as a miss, so the queue would re-download it.
    item = dict(row)
    raw = item.pop("media_json")
    if raw:
        item["media"] = json.loads(raw)
    else:
        item["media"] = [{"url": item["media_url"], "type": item["media_type"]}]
    item["cached"] = hashlib.sha256(item["media_url"].encode()).hexdigest() in cached_names
    return item


@router.get("/items", response_model=None)
async def list_items(
    unseen: bool = False,
    after_feed_id: str | None = None,
    after_pub_date: str | None = None,
    after_id: str | None = None,
    size: int = Query(50, ge=1, le=200),
    *,
    db: _DbDep,
) -> list[ItemOut]:
    """Return a keyset-paginated, interleaved list of media items.

    The window function assigns rn per feed over the FULL items set (seen
    filter applied outside the CTE), so rn is stable when the client marks
    items seen: marking item X seen removes it from the result but does not
    renumber any other item.

    The cursor is the last item's (feed_id, pub_date, id) — three immutable
    column values. rn itself is NOT a cursor: it is recomputed per request, so
    every prune_items cycle shifts it down under an outstanding client cursor
    and the client skips exactly as many items as were pruned beneath it (R3).
    Instead the anchor's rank is derived here by counting the rows of its feed
    at or before it, which moves in lockstep with the ranks of the rows after
    it. The count also resolves correctly when the anchor row was itself
    pruned: it yields the position of the last surviving row at or before it.

    A count-based OFFSET cannot be correct here either: the set the client is
    counting over ceases to be a prefix of the server's ranking the moment any
    item changes seen state (F17).

    Limitation: a newly inserted item with an older pub_date shifts its feed's
    rn values and can invalidate an outstanding cursor. Rare in practice (new
    feed entries vs. per-scroll mark-seen); the client's known-set guard dedups
    the duplicate side.
    """
    cursor = (after_feed_id, after_pub_date, after_id)
    if any(part is not None for part in cursor) and not all(part is not None for part in cursor):
        logger.debug(
            f"list_items: 422, partial cursor after_feed_id={after_feed_id} "
            f"after_pub_date={after_pub_date} after_id={after_id}"
        )
        raise HTTPException(
            status_code=422,
            detail="after_feed_id, after_pub_date and after_id must be given together",
        )

    conditions: list[str] = []
    params: list[str | int] = []
    if unseen:
        conditions.append("seen_at IS NULL")
    if all(part is not None for part in cursor):
        # The anchor's rank, derived from immutable values rather than echoed
        # back by the client. Same partition, same tiebreak as the CTE window.
        conditions.append(
            "(rn, feed_id, id) > ((SELECT COUNT(*) FROM items WHERE feed_id = ? AND (pub_date, id) <= (?, ?)), ?, ?)"
        )
        params.extend([after_feed_id, after_pub_date, after_id, after_feed_id, after_id])
    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(size)

    # SQL fragments are source-controlled; request values remain bound.
    query = f"""
        {RANKED_ITEMS_CTE}
        SELECT id, feed_id, title, media_url, media_type, media_json,
               pub_date, fetched_at, seen_at
        FROM ranked
        {where_clause}
        {INTERLEAVE_ORDER_BY}
        LIMIT ?
    """  # noqa: S608
    logger.debug(f"list_items unseen={unseen} cursor=({after_feed_id},{after_pub_date},{after_id}) size={size}")
    t0 = time.perf_counter()
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    db_ms = (time.perf_counter() - t0) * 1000
    t_cache = time.perf_counter()
    cached_names = await asyncio.to_thread(cache_present_names)
    cache_ms = (time.perf_counter() - t_cache) * 1000
    logger.debug(f"list_items: cache_present_names returned {len(cached_names)} name(s) in {cache_ms:.1f}ms")
    items = [_row_to_item(row, cached_names) for row in rows]
    # The cached count is the number the browser can paint instantly; a low
    # ratio here is why a scroll feels slow, and it is what the UI_DEBUG
    # overlay's HIT/MISS line reflects.
    cached_count = sum(1 for i in items if i["cached"])
    logger.debug(f"list_items returned {len(items)} item(s), {cached_count} cached on disk; db={db_ms:.1f}ms")
    return items


@router.post("/items/{item_id}/seen", response_model=None)
async def mark_seen(
    item_id: str,
    db: _DbDep,
) -> SeenResponse:
    """Mark an item as seen and return the timestamp.

    Uses UPDATE ... RETURNING (SQLite >= 3.35) so the SELECT-before-UPDATE
    and the trailing SELECT both go away: one statement both updates and
    returns media_url + seen_at. A single Python timestamp is bound to both
    items.seen_at and seen_media.seen_at so the two cannot diverge (F11).

    The browser marks the item locally before firing this beacon request,
    which it discards; the response shape is kept stable for symmetry with
    the optimistic local mark (F20).
    """
    logger.debug(f"mark_seen item_id={item_id}")
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.perf_counter()
    async with db.execute(
        "UPDATE items SET seen_at = ? WHERE id = ? RETURNING media_url, seen_at",
        (now, item_id),
    ) as cur:
        row = await cur.fetchone()
    update_ms = (time.perf_counter() - t0) * 1000
    if row is None:
        logger.debug(f"mark_seen item_id={item_id} not found")
        raise HTTPException(status_code=404, detail="Not found")

    t0 = time.perf_counter()
    await db.execute(
        "INSERT OR REPLACE INTO seen_media (media_key, seen_at) VALUES (?, ?)",
        (media_key(row["media_url"]), now),
    )
    insert_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    await db.commit()
    commit_ms = (time.perf_counter() - t0) * 1000

    logger.debug(
        f"mark_seen item_id={item_id} seen_at={row['seen_at']} "
        f"update={update_ms:.1f}ms insert={insert_ms:.1f}ms commit={commit_ms:.1f}ms"
    )
    return {"seen_at": row["seen_at"]}
