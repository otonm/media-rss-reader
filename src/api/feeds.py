"""GET /api/feeds — list all feeds with item counts."""
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Depends

from src.db.connection import get_db

router = APIRouter()


@router.get("/feeds")
async def list_feeds(db: Annotated[aiosqlite.Connection, Depends(get_db)]) -> list[dict[str, Any]]:
    """Return all feeds with total and unseen item counts.

    The LEFT JOIN + conditional COUNT gives both counts in one query,
    avoiding a second round-trip per feed.
    """
    async with db.execute(
        """SELECT f.id, f.title, f.url, f.last_fetched_at,
                  COUNT(i.id)                                  AS item_count,
                  COUNT(CASE WHEN i.seen_at IS NULL THEN i.id END) AS unseen_count
           FROM feeds f
           LEFT JOIN items i ON i.feed_id = f.id
           GROUP BY f.id"""
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]
