"""GET /api/items and POST /api/items/{id}/seen."""

import json
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from src.db.connection import get_db

router = APIRouter()

_DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]


def _row_to_item(row: aiosqlite.Row) -> dict[str, Any]:
    """Convert an items row to the API shape, expanding media_json to `media`.

    Rows predating migration v5 have media_json NULL; they fall back to a
    1-element list built from media_url/media_type so the frontend always
    receives a `media` array.
    """
    item = dict(row)
    raw = item.pop("media_json")
    if raw:
        item["media"] = json.loads(raw)
    else:
        item["media"] = [{"url": item["media_url"], "type": item["media_type"]}]
    return item


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
        SELECT id, feed_id, title, media_url, media_type, media_json, pub_date, fetched_at, seen_at
        FROM ranked
        ORDER BY rn ASC, feed_id ASC
        LIMIT ? OFFSET ?
    """
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_item(row) for row in rows]


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


@router.get("/items/count")
async def count_items(
    unseen: bool = True,
    feed_id: str | None = None,
    db: _DbDep = None,  # type: ignore[assignment]
) -> dict[str, int]:
    """Return the total count of media items matching the filter.

    Defaults to unseen=true to match the frontend's default request. Used
    by the WebUI to populate the N / total counter and to detect "end of
    feed" without a separate count query per page.
    """
    conditions: list[str] = []
    params: list[Any] = []

    if unseen:
        conditions.append("seen_at IS NULL")
    if feed_id is not None:
        conditions.append("feed_id = ?")
        params.append(feed_id)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    query = f"SELECT COUNT(*) FROM items {where_clause}"
    async with db.execute(query, params) as cur:
        row = await cur.fetchone()
    return {"count": row[0]}
