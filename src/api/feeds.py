"""GET /api/feeds — list all feeds with item counts."""

import logging

from fastapi import APIRouter

from src.api.schemas import FeedOut
from src.db.connection import _DbDep

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/feeds", response_model=None)
async def list_feeds(db: _DbDep) -> list[FeedOut]:
    """Return all feeds with total and unseen item counts.

    The LEFT JOIN + conditional COUNT gives both counts in one query,
    avoiding a second round-trip per feed.
    """
    logger.debug("list_feeds querying all feeds with counts")
    async with db.execute(
        """SELECT f.id, f.title, f.url, f.last_fetched_at,
                  COUNT(i.id)                                  AS item_count,
                  COUNT(CASE WHEN i.seen_at IS NULL THEN i.id END) AS unseen_count
           FROM feeds f
           LEFT JOIN items i ON i.feed_id = f.id
           GROUP BY f.id
           ORDER BY f.title"""
    ) as cur:
        rows = await cur.fetchall()
    logger.debug(f"list_feeds returned {len(rows)} feed(s)")
    return [dict(row) for row in rows]
