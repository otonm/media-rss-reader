"""GET /api/feeds — list all feeds with item counts."""

import logging
from typing import Any

from fastapi import APIRouter

from src.db.connection import DbDep
from src.timing import timer

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/feeds")
async def list_feeds(db: DbDep) -> list[dict[str, Any]]:
    """Return all feeds with total and unseen item counts.

    The LEFT JOIN + conditional COUNT gives both counts in one query,
    avoiding a second round-trip per feed.
    """
    logger.debug("list_feeds querying all feeds with counts")
    elapsed = timer()
    async with db.execute(
        """SELECT f.id, f.title, f.url, f.last_fetched_at,
                  COUNT(i.id)                                  AS item_count,
                  COUNT(CASE WHEN i.seen_at IS NULL THEN i.id END) AS unseen_count
           FROM feeds f
           LEFT JOIN items i ON i.feed_id = f.id
           GROUP BY f.id
           ORDER BY f.title COLLATE NOCASE"""
    ) as cur:
        rows = await cur.fetchall()
    logger.debug(f"list_feeds returned {len(rows)} feed(s); db={elapsed():.1f}ms")
    return [dict(row) for row in rows]
