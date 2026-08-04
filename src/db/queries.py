"""SQL fragments shared between the API and the background prefetcher.

The ranked-items CTE is the interleave contract: /api/items paginates over it
and the prefetcher warms the next items in the same order. They used to be
three separate copies (items.py, prefetch.py twice) and had already drifted,
so both import from here.

The window orders by id as well as pub_date. That is not cosmetic: the
/api/items cursor derives an anchor's rank by counting rows <= (pub_date, id),
and that count only equals ROW_NUMBER if ties break by id.

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
