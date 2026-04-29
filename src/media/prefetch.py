import asyncio
import logging

import aiosqlite
import httpx

from src.config import settings
from src.media.cache import cache_read, cache_write

logger = logging.getLogger(__name__)


async def _warm(url: str, client: httpx.AsyncClient) -> None:
    if cache_read(url) is not None:
        return
    try:
        response = await client.get(url, follow_redirects=True, timeout=30)
        if response.is_success:
            await cache_write(url, await response.aread())
    except Exception as exc:  # pragma: no cover
        logger.debug("prefetch failed for %s: %s", url, exc)


async def prefetch_ahead(
    item_id: str, db: aiosqlite.Connection, client: httpx.AsyncClient
) -> None:
    async with db.execute(
        """SELECT media_url FROM items
           WHERE pub_date < (SELECT pub_date FROM items WHERE id = ?)
           ORDER BY pub_date DESC
           LIMIT ?""",
        (item_id, settings.prefetch_ahead),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        asyncio.create_task(_warm(row["media_url"], client))
