"""GET /api/items and POST /api/items/{id}/seen."""

from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from src.db.connection import get_db

router = APIRouter()

_DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]


@router.get("/items")
async def list_items(
    unseen: bool = False,
    feed_id: str | None = None,
    page: int = 0,
    size: int = 50,
    db: _DbDep = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """Return a paginated, interleaved list of media items.

    The window-function query assigns a rank (rn) per feed ordered by
    pub_date ASC, then sorts globally by rn then feed_id. This interleaves
    feeds evenly: all feeds contribute their oldest unseen item before any
    feed contributes its second item, preventing one prolific feed from
    dominating the top of the page.
    """
    conditions: list[str] = []
    params: list[Any] = []

    if unseen:
        conditions.append("seen_at IS NULL")
    if feed_id is not None:
        conditions.append("feed_id = ?")
        params.append(feed_id)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.extend([size, page * size])

    query = f"""
        WITH ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (PARTITION BY feed_id ORDER BY pub_date ASC) AS rn
            FROM items
            {where_clause}
        )
        SELECT id, feed_id, title, media_url, media_type, pub_date, fetched_at, seen_at
        FROM ranked
        ORDER BY rn ASC, feed_id ASC
        LIMIT ? OFFSET ?
    """
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


@router.post("/items/{item_id}/seen")
async def mark_seen(
    item_id: str,
    db: _DbDep = None,  # type: ignore[assignment]
) -> dict[str, str]:
    """Mark an item as seen and return the timestamp.

    The browser stores the returned seen_at value on the item object to
    prevent a second POST for the same item during the session.
    """
    await db.execute(
        "UPDATE items SET seen_at = datetime('now') WHERE id = ?",
        (item_id,),
    )
    # Write through to seen_guids so seen state survives pruning.
    await db.execute(
        """INSERT OR REPLACE INTO seen_guids (feed_id, guid, seen_at)
           SELECT feed_id, guid, datetime('now') FROM items WHERE id = ?""",
        (item_id,),
    )
    await db.commit()

    async with db.execute("SELECT seen_at FROM items WHERE id = ?", (item_id,)) as cur:
        row = await cur.fetchone()

    if row is None or row[0] is None:
        raise HTTPException(status_code=404, detail="Not found")

    return {"seen_at": row[0]}
