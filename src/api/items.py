"""GET /api/items and POST /api/items/{id}/seen."""

import json
import logging
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from src.db.connection import get_db
from src.media.cache import cache_read
from src.media.normalize import media_key

logger = logging.getLogger(__name__)
router = APIRouter()

_DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]


def _row_to_item(row: aiosqlite.Row) -> dict[str, Any]:
    """Convert an items row to the API shape, expanding media_json to `media`.

    Rows predating migration v5 have media_json NULL; they fall back to a
    1-element list built from media_url/media_type so the frontend always
    receives a `media` array.

    `cached` tells the browser which items are already on disk so it can
    download those first — they decode in milliseconds, while a miss waits on
    the origin. It costs one stat() per row and is a hint, not a promise: an
    entry can be evicted, or warmed, moments after the response goes out.
    """
    item = dict(row)
    raw = item.pop("media_json")
    if raw:
        item["media"] = json.loads(raw)
    else:
        item["media"] = [{"url": item["media_url"], "type": item["media_type"]}]
    item["cached"] = cache_read(item["media_url"]) is not None
    return item


@router.get("/items")
async def list_items(
    unseen: bool = False,
    feed_id: str | None = None,
    offset: int = 0,
    size: int = 50,
    db: _DbDep = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """Return a paginated, interleaved list of media items.

    The window-function query assigns a rank (rn) per feed ordered by
    pub_date ASC, then sorts globally by rn then feed_id. This interleaves
    feeds evenly: all feeds contribute their oldest unseen item before any
    feed contributes its second item, preventing one prolific feed from
    dominating the top of the page.

    `offset` is a raw row offset, not a page number, because with unseen=true
    the result set shrinks as the client marks items seen. The client sends
    how many matching items it already holds; a page number would multiply by
    a size that no longer describes what came before.
    """
    conditions: list[str] = []
    params: list[Any] = []

    if unseen:
        conditions.append("seen_at IS NULL")
    if feed_id is not None:
        conditions.append("feed_id = ?")
        params.append(feed_id)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.extend([size, offset])

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
    logger.debug(f"list_items unseen={unseen} feed_id={feed_id} offset={offset} size={size}")
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    items = [_row_to_item(row) for row in rows]
    # The cached count is the number the browser can paint instantly; a low
    # ratio here is why a scroll feels slow, and it is what the UI_DEBUG
    # overlay's HIT/MISS line reflects.
    cached_count = sum(1 for i in items if i["cached"])
    logger.debug(f"list_items returned {len(items)} item(s), {cached_count} already cached on disk")
    return items


@router.post("/items/{item_id}/seen")
async def mark_seen(
    item_id: str,
    db: _DbDep = None,  # type: ignore[assignment]
) -> dict[str, str]:
    """Mark an item as seen and return the timestamp.

    Writes through to seen_media, which is what actually keeps the item out
    of the feed: items.seen_at dies with the row when prune_items evicts it,
    and the feed still lists the entry, so the next sync would re-insert it
    unseen. seen_media is keyed on the normalised media URL, so the same
    picture stays seen no matter which feed carries it.

    The browser marks the item locally before firing this request, so the
    response is only used to confirm the item existed.
    """
    logger.debug(f"mark_seen item_id={item_id}")
    async with db.execute("SELECT media_url FROM items WHERE id = ?", (item_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        logger.debug(f"mark_seen item_id={item_id} not found")
        raise HTTPException(status_code=404, detail="Not found")

    await db.execute(
        "UPDATE items SET seen_at = datetime('now') WHERE id = ?",
        (item_id,),
    )
    await db.execute(
        "INSERT OR REPLACE INTO seen_media (media_key, seen_at) VALUES (?, datetime('now'))",
        (media_key(row["media_url"]),),
    )
    await db.commit()

    async with db.execute("SELECT seen_at FROM items WHERE id = ?", (item_id,)) as cur:
        seen_row = await cur.fetchone()

    logger.debug(f"mark_seen item_id={item_id} seen_at={seen_row[0]}")
    return {"seen_at": seen_row[0]}
