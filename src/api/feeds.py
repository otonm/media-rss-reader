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

    The one consumer is initDebugOverlay (src/static/controls.js:158-168),
    which builds a feed-id -> title map for the UI_DEBUG overlay. It used to
    return item_count and unseen_count too, from a LEFT JOIN and GROUP BY over
    the whole items table, defended by a docstring about the round-trips it
    saved — for counts nobody requested.
    """
    logger.debug("list_feeds querying all feeds")
    elapsed = timer()
    async with db.execute("SELECT id, title FROM feeds ORDER BY title COLLATE NOCASE") as cur:
        rows = await cur.fetchall()
    logger.debug(f"list_feeds returned {len(rows)} feed(s); db={elapsed():.1f}ms")
    return [dict(row) for row in rows]
