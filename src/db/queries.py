"""SQL fragments shared between the API and the background prefetcher.

The ranked-items CTE is the interleave contract: /api/items paginates over it
and the prefetcher warms the next items in the same order. They used to be
three separate copies (items.py, prefetch.py twice) and had already drifted,
so both import from here.

The window orders by id as well as pub_date. That is not cosmetic: /api/items
resolves a cursor anchor with one statement and reads the page with another,
and two statements can only agree on a rank if ties break deterministically.

This module lives in src/db/ rather than src/api/ because src/media/prefetch.py
imports it, and src/media must not depend on src/api.
"""

RANKED_ITEMS_CTE = """
    WITH ranked AS (
        SELECT id, feed_id, title, media_url, media_type, media_json,
               pub_date, fetched_at, seen_at,
               ROW_NUMBER() OVER (PARTITION BY feed_id ORDER BY pub_date ASC, id ASC) AS rn
        FROM items
    )
"""

INTERLEAVE_ORDER_BY = "ORDER BY rn ASC, feed_id ASC, id ASC"

# warm_startup_cache orders by this. It is INTERLEAVE_ORDER_BY with unseen rows
# ahead of seen ones, because the client defaults to showSeen: false and so asks
# for unseen=true. The warm used to be `ORDER BY pub_date DESC LIMIT
# CACHE_MAX_ITEMS` — the newest items globally — while /api/items serves the
# OLDEST unseen item of each feed first. At the default KEEP_ITEMS=1000 /
# CACHE_MAX_ITEMS=500 that warmed exactly the half of the library the reader
# reaches last, so page one was a guaranteed cache miss and cachedFirst() in the
# browser queue had nothing to prefer.
UNSEEN_FIRST_ORDER_BY = "ORDER BY (seen_at IS NOT NULL) ASC, rn ASC, feed_id ASC, id ASC"

# The anchor lookup and the keyset predicate. These used to be verbatim copies
# in src/api/items.py and src/media/prefetch.py while this module's docstring
# claimed both sides imported from here — it held for the CTE and the ORDER BY
# only. Adding a column to the tiebreak now lands in one place.
ANCHOR_LOOKUP = f"{RANKED_ITEMS_CTE} SELECT rn, feed_id, id FROM ranked WHERE id = ?"  # noqa: S608

KEYSET_AFTER = "(rn, feed_id, id) > (?, ?, ?)"
