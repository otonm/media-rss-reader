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
