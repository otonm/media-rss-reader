"""GET /api/feeds — list all feeds."""

import logging
from typing import Any

from fastapi import APIRouter

from src.db.connection import DbDep
from src.timing import timer

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/feeds")
async def list_feeds(db: DbDep) -> list[dict[str, Any]]:
    """Return every feed's id and title.

    The one consumer is initDebugOverlay in src/static/controls.js: /api/items
    carries feed_id but not the feed's name, so the UI_DEBUG overlay fetches
    this once to build a feed-id -> title map. Deliberately no item counts —
    they cost a LEFT JOIN and GROUP BY over the whole items table and nothing
    asks for them.
    """
    logger.debug("list_feeds querying all feeds")
    elapsed = timer()
    async with db.execute("SELECT id, title FROM feeds ORDER BY title COLLATE NOCASE") as cur:
        rows = await cur.fetchall()
    logger.debug(f"list_feeds returned {len(rows)} feed(s); db={elapsed():.1f}ms")
    return [dict(row) for row in rows]
