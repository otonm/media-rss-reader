"""GET /api/items and POST /api/items/{id}/seen."""

import asyncio
import datetime as dt
import logging
from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Query

from src.db.connection import DbDep
from src.db.queries import ANCHOR_LOOKUP, INTERLEAVE_ORDER_BY, KEYSET_AFTER, RANKED_ITEMS_CTE
from src.media.cache import cache_name, cache_present_names
from src.media.normalize import item_slides, media_key
from src.timing import timer

logger = logging.getLogger(__name__)
router = APIRouter()

# mark_seen is the request path's only concurrent writer (post_setup runs once,
# ever). get_db hands every request the same connection, and sqlite3 opens one
# implicit transaction per connection, not per coroutine — so without this two
# overlapping marks share a transaction and either one's rollback discards the
# other's UPDATE, leaving seen_media written and items.seen_at not (F11).
_write_lock = asyncio.Lock()


def _row_to_item(row: aiosqlite.Row, cached_names: set[str]) -> dict[str, Any]:
    """Convert an items row to the API shape, expanding media_json to `media`.

    `rn` is returned so the client can send it back as after_rn. It was removed
    once as a dead column; it is not dead, and deleting it re-arms M2 — the
    cursor silently skipping items when a feed gains a row that ranks before
    the anchor.

    `cached` tells the browser which items are already on disk so it can
    download those first — they decode in milliseconds, while a miss waits on
    the origin. It is a hint, not a promise: an entry can be evicted, or
    warmed, moments after the response goes out.
    """
    # ponytail: cached = slide[0] hit; queue prioritises paintable items, not
    # full-gallery warmth (F9). Checking all slides would mark a gallery whose
    # first slide is on disk as a miss, so the queue would re-download it.
    item = dict(row)
    item.pop("media_json")
    item["media"] = item_slides(row)
    item["cached"] = cache_name(item["media_url"]) in cached_names
    return item


@router.get("/items", response_model=None)
async def list_items(
    unseen: bool = False,
    after_id: str | None = None,
    after_rn: int | None = None,
    size: int = Query(50, ge=1, le=200),
    *,
    db: DbDep,
) -> list[dict[str, Any]]:
    """Return a keyset-paginated, interleaved list of media items.

    The window function assigns rn per feed over the FULL items set (seen
    filter applied outside the CTE), so rn is stable when the client marks
    items seen: marking item X seen removes it from the result but does not
    renumber any other item.

    The cursor is the id of the last item the client holds — one immutable
    column value. rn itself is NOT a cursor: it is recomputed per request, so
    every prune_items cycle shifts it down under an outstanding client cursor
    and the client would skip exactly as many items as were pruned beneath it
    (R3). Instead the anchor's (rn, feed_id, id) is looked up in the same CTE
    that orders this page, which is what src/media/prefetch.py does with the
    same fragments (src/db/queries.py). The fragments are shared; the window is
    not — the prefetcher resolves its own anchor from the item id it was
    handed, so after a refresh it can warm a window a few rows off the one this
    page serves. That is a hint, not a contract.

    Reading rn from the window rather than reconstructing it by counting is
    what makes a NULL pub_date harmless: ROW_NUMBER sorts NULLs first and ranks
    them 1..k, while a row-value comparison with a NULL member evaluates to
    NULL in SQLite, so any count-based derivation drops exactly those rows.

    A count-based OFFSET cannot be correct here either: the set the client is
    counting over ceases to be a prefix of the server's ranking the moment any
    item changes seen state (F17).

    The cursor carries the rank it was issued alongside the id, and the page is
    bounded by min(issued, resolved). rn is recomputed per request, so it moves
    under an outstanding cursor in both directions: a prune lowers it, and a row
    inserted with an older pub_date raises it. Taking the lower bound turns the
    raise — which used to skip every undelivered row between the two ranks —
    into duplicates the client's known-set guard already drops. The volume is
    bounded at k+1 for k rows inserted before the anchor: the anchor itself plus
    the rows in that feed pushed above the old bound. Other feeds contribute
    none, because the tiebreak is (rn, feed_id, id) and their same-rank rows
    were never delivered under the old numbering either.

    min(issued, resolved) only recovers rows ahead of the cursor — rows that
    already existed and were due to be delivered next. A row inserted behind
    the cursor is not: the routine case is an undated entry, which ROW_NUMBER
    ranks first in its feed, landing below any cursor already past position 1.
    That row is not delivered until the client reloads from the top. This is
    ordinary forward pagination, not M2 — M2 lost rows ahead of the cursor that
    had never been delivered at all.

    after_rn is optional so a page cached in a browser from before this change
    degrades to the old behaviour instead of 422-ing.

    An anchor that no longer exists — pruned, or its feed left the OPML and the
    rows cascaded — answers 410. Resolving it to a position instead is what
    produced a rank of 0, and `(rn, feed_id, id) > (0, ...)` admits the whole
    table: page one of the global interleave, which the client's known-set
    filter discards, leaving a cursor that never advances.
    """
    logger.debug(f"list_items unseen={unseen} after_id={after_id} after_rn={after_rn} size={size}")
    conditions: list[str] = []
    params: list[str | int] = []
    if unseen:
        conditions.append("seen_at IS NULL")
    if after_id is not None:
        anchor_elapsed = timer()
        async with db.execute(ANCHOR_LOOKUP, (after_id,)) as cur:
            anchor = await cur.fetchone()
        anchor_ms = anchor_elapsed()
        if anchor is None:
            logger.info(f"list_items: 410, cursor anchor {after_id} no longer exists (db={anchor_ms:.1f}ms)")
            raise HTTPException(status_code=410, detail="cursor expired")
        bound_rn = anchor["rn"] if after_rn is None else min(after_rn, anchor["rn"])
        if bound_rn != anchor["rn"]:
            logger.info(
                f"list_items: anchor {after_id} rank moved {after_rn}->{anchor['rn']}, "
                f"paging from {bound_rn} so no undelivered row ahead of the cursor is skipped"
            )
        logger.debug(
            f"list_items: anchor {after_id} resolved to rn={anchor['rn']} "
            f"feed_id={anchor['feed_id']} bound={bound_rn} (db={anchor_ms:.1f}ms)"
        )
        conditions.append(KEYSET_AFTER)
        params.extend([bound_rn, anchor["feed_id"], anchor["id"]])
    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(size)

    # SQL fragments are source-controlled; request values remain bound.
    query = f"""
        {RANKED_ITEMS_CTE}
        SELECT id, feed_id, title, media_url, media_type, media_json,
               pub_date, fetched_at, seen_at, rn
        FROM ranked
        {where_clause}
        {INTERLEAVE_ORDER_BY}
        LIMIT ?
    """  # noqa: S608
    db_elapsed = timer()
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    db_ms = db_elapsed()
    cache_elapsed = timer()
    cached_names = await asyncio.to_thread(cache_present_names)
    logger.debug(f"list_items: cache_present_names returned {len(cached_names)} name(s) in {cache_elapsed():.1f}ms")
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
    db: DbDep,
) -> dict[str, str]:
    """Mark an item as seen and return the timestamp.

    Uses UPDATE ... RETURNING (SQLite >= 3.35) so the SELECT-before-UPDATE
    and the trailing SELECT both go away: one statement both updates and
    returns media_url + seen_at. A single Python timestamp is bound to both
    items.seen_at and seen_media.seen_at so the two cannot diverge (F11).

    Both writes run under _write_lock, which is what makes the rollbacks below
    safe: nothing else can have work open on the shared connection, so a
    ROLLBACK here can only discard this request's own statements.

    The browser marks the item locally before firing this beacon request,
    which it discards; the response shape is kept stable for symmetry with
    the optimistic local mark (F20).
    """
    logger.debug(f"mark_seen item_id={item_id}")
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    async with _write_lock:
        update_elapsed = timer()
        async with db.execute(
            "UPDATE items SET seen_at = ? WHERE id = ? RETURNING media_url, seen_at",
            (now, item_id),
        ) as cur:
            row = await cur.fetchone()
        update_ms = update_elapsed()
        if row is None:
            # sqlite3 opens an implicit transaction before any DML, including an
            # UPDATE that matches nothing. Raising without closing it left the
            # process-wide connection holding a RESERVED lock indefinitely: every
            # run_with_own_db write — dead-URL marks, digest records — then waited
            # out the 30 s busy timeout and was swallowed as a warning, and WAL
            # could not checkpoint.
            await db.rollback()
            logger.debug(f"mark_seen item_id={item_id} not found")
            raise HTTPException(status_code=404, detail="Not found")

        try:
            insert_elapsed = timer()
            await db.execute(
                "INSERT OR REPLACE INTO seen_media (media_key, seen_at) VALUES (?, ?)",
                (media_key(row["media_url"]), now),
            )
            insert_ms = insert_elapsed()
            commit_elapsed = timer()
            await db.commit()
            commit_ms = commit_elapsed()
        except Exception:
            logger.warning(f"mark_seen item_id={item_id}: write failed, rolling back the seen mark")
            try:
                await db.rollback()
            except Exception:
                # Rollback is I/O on a possibly-broken connection. Letting it
                # propagate would replace the exception that describes what
                # actually went wrong.
                logger.exception(f"mark_seen item_id={item_id}: rollback failed as well")
            raise

    logger.debug(
        f"mark_seen item_id={item_id} seen_at={row['seen_at']} "
        f"update={update_ms:.1f}ms insert={insert_ms:.1f}ms commit={commit_ms:.1f}ms"
    )
    return {"seen_at": row["seen_at"]}
