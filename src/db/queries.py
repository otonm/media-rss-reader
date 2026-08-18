"""SQL fragments shared between the API and the background prefetcher.

The ranked-items CTE is the interleave contract: /api/items paginates over it
and the prefetcher warms the next items in the same order. Both sides import
every fragment from here so the two orderings cannot drift apart.

The window orders by id as well as pub_date, and every ORDER BY below repeats
the same tiebreak. That is not cosmetic: /api/items resolves a cursor anchor
with one statement and reads the page with another, and two statements can only
agree on a rank if ties break deterministically. Change one, change all.

This module lives in src/db/ rather than src/api/ because src/media/prefetch.py
imports it, and src/media must not depend on src/api.
"""

import aiosqlite

RANKED_ITEMS_CTE = """
    WITH ranked AS (
        SELECT id, feed_id, title, media_url, media_type, media_json,
               pub_date, fetched_at, seen_at,
               ROW_NUMBER() OVER (PARTITION BY feed_id ORDER BY pub_date ASC, id ASC) AS rn
        FROM items
    )
"""

INTERLEAVE_ORDER_BY = "ORDER BY rn ASC, feed_id ASC, id ASC"

# warm_startup_cache orders by this: INTERLEAVE_ORDER_BY with unseen rows ahead
# of seen ones, because the client defaults to showSeen: false and so asks for
# unseen=true. Warming in any other order fills the end of the library the
# reader reaches last, leaving page one a guaranteed cache miss.
UNSEEN_FIRST_ORDER_BY = "ORDER BY (seen_at IS NOT NULL) ASC, rn ASC, feed_id ASC, id ASC"

# The cursor pair: ANCHOR_LOOKUP resolves an item id to its rank, KEYSET_AFTER
# reads the page that follows it.
ANCHOR_LOOKUP = f"{RANKED_ITEMS_CTE} SELECT rn, feed_id, id FROM ranked WHERE id = ?"  # noqa: S608 — only source-controlled SQL constants are interpolated

KEYSET_AFTER = "(rn, feed_id, id) > (?, ?, ?)"


async def resolve_anchor(db: aiosqlite.Connection, item_id: str) -> aiosqlite.Row | None:
    """Resolve a cursor anchor id to its current (rn, feed_id, id) in the ranking."""
    async with db.execute(ANCHOR_LOOKUP, (item_id,)) as cur:
        return await cur.fetchone()


async def ranked_page(
    db: aiosqlite.Connection,
    *,
    columns: str,
    unseen: bool,
    size: int,
    after: aiosqlite.Row | None = None,
    after_rn: int | None = None,
    order: str = INTERLEAVE_ORDER_BY,
) -> list[aiosqlite.Row]:
    """One page of the interleave. The single assembly point for the ranking.

    `after` is an already-resolved anchor row (see resolve_anchor), so the
    caller keeps ownership of what a missing anchor means — a 410 for the API,
    a None return for the prefetcher.

    The page is bounded at min(after_rn, after["rn"]). Taking the LOWER bound is
    load-bearing: pruning deletes lowest-rn-first, so every surviving row in
    that feed shifts down, and a stale after_rn would silently skip exactly the
    pruned count. See spec.md §9.2.

    `columns` and `order` are interpolated into the SQL, not bound. They must be
    source-controlled literals — never anything derived from a request. Every
    request value (`size`, `after_rn`, the anchor fields) is bound.
    """
    conditions: list[str] = []
    params: list[str | int] = []
    if unseen:
        conditions.append("seen_at IS NULL")
    if after is not None:
        bound_rn = after["rn"] if after_rn is None else min(after_rn, after["rn"])
        conditions.append(KEYSET_AFTER)
        params.extend([bound_rn, after["feed_id"], after["id"]])
    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(size)

    # Only source-controlled fragments are interpolated; request values stay bound.
    query = f"""
        {RANKED_ITEMS_CTE}
        SELECT {columns}, rn
        FROM ranked
        {where_clause}
        {order}
        LIMIT ?
    """  # noqa: S608
    async with db.execute(query, params) as cur:
        return list(await cur.fetchall())
