from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends

from src.db.connection import get_db

router = APIRouter()


@router.get("/feeds")
async def list_feeds(db: aiosqlite.Connection = Depends(get_db)) -> list[dict[str, Any]]:  # noqa: B008
    async with db.execute(
        """SELECT f.id, f.title, f.url, f.last_fetched_at,
                  COUNT(i.id)                                  AS item_count,
                  COUNT(CASE WHEN i.seen_at IS NULL THEN 1 END) AS unseen_count
           FROM feeds f
           LEFT JOIN items i ON i.feed_id = f.id
           GROUP BY f.id"""
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]
