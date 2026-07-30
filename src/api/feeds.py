"""GET /api/feeds — list all feeds with item counts."""

import logging
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Depends

from src.db.connection import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/feeds")
async def list_feeds(db: Annotated[aiosqlite.Connection, Depends(get_db)]) -> list[dict[str, Any]]:
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
           GROUP BY f.id"""
    ) as cur:
        rows = await cur.fetchall()
    logger.debug(f"list_feeds returned {len(rows)} feed(s)")
    return [dict(row) for row in rows]
