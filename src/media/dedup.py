"""Content-based deduplication of media that arrives under different URLs.

src.media.normalize catches the same picture when the two feeds hand us
URLs that differ only cosmetically. It cannot catch a genuine re-upload:
two distinct CDN asset IDs holding the same image.

record_media_hash(url, digest, db) is called from the proxy and the prefetch
warmer every time a media file is downloaded — the bytes are already in hand
at that point, so this costs no extra network traffic. It stores the digest,
and if a *different* URL already carries it, drops the newer of the two items
and tombstones it into unavailable_guids, which _refresh_feed already reads to
skip re-insert on the next poll. That is what makes the drop stick.

This deliberately mirrors src.media.availability, which performs the same
record-fact / find-items / delete-and-tombstone dance for dead URLs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiosqlite

from src.config import settings
from src.logging_utils import loggable
from src.media.cache import cache_read

logger = logging.getLogger(__name__)

PHASH_BITS = 256


def _phash(path: Path) -> int:
    """Return a 256-bit block-mean perceptual hash of an image file.

    Ported from the reddit_bro extension's "similar" mode: centre-crop to 80%
    (dropping watermarks and letterboxing), reduce to a 16x16 grid of block
    means, and take one bit per cell for "brighter than the image average".

    PIL's "L" conversion is ITU-R 601-2 luma (0.299R + 0.587G + 0.114B),
    matching the reference exactly, and an Image.BOX downscale *is* the
    4x4 block average the reference computes by hand.

    Raises whatever PIL raises for a file it cannot decode — video is not
    an image, and the caller treats that as "no perceptual hash".
    """
    from PIL import Image

    with Image.open(path) as img:
        grey = img.convert("L")
        w, h = grey.size
        cropped = grey.crop((w // 10, h // 10, w - w // 10, h - h // 10))
        cells = cropped.resize((64, 64), Image.BILINEAR).resize((16, 16), Image.BOX)
        # A 16x16 "L" image is exactly 256 unpadded bytes, one per cell.
        values = cells.tobytes()

    average = sum(values) / len(values)
    bits = 0
    for value in values:
        bits = (bits << 1) | (value > average)  # strict >, ties fall to 0
    return bits


async def _compute_phash(url: str) -> str | None:
    """Perceptual hash of `url`'s cached file as hex, or None if unavailable.

    Returns None when perceptual matching is disabled, the file is not
    cached, or the file is not a decodable image (video, truncated download).
    """
    if settings.dedup_similarity <= 0:
        return None
    path = cache_read(url)
    if path is None:
        return None
    try:
        bits = await asyncio.to_thread(_phash, path)
    except Exception as exc:
        logger.debug(f"_compute_phash: no perceptual hash for {loggable(url)}: {exc}")
        return None
    return f"{bits:0{PHASH_BITS // 4}x}"


async def _similar_urls(db: aiosqlite.Connection, url: str, phash: str) -> list[str]:
    """Return URLs whose perceptual hash is within DEDUP_SIMILARITY of `phash`."""
    bits = int(phash, 16)
    async with db.execute(
        "SELECT url, phash FROM media_hashes WHERE phash IS NOT NULL AND url != ?",
        (url,),
    ) as cur:
        rows = await cur.fetchall()

    # ponytail: O(n) scan over <= KEEP_ITEMS hashes, a few hundred microseconds.
    # Index with a BK-tree (see deduplicators/rededup-master/rededup.js:403) if
    # this ever shows up in a profile.
    matches = []
    for row in rows:
        distance = (bits ^ int(row["phash"], 16)).bit_count()
        if (PHASH_BITS - distance) * 100 // PHASH_BITS > settings.dedup_similarity:
            logger.debug(f"_similar_urls: {loggable(url)} within {distance} bits of {loggable(row['url'])}")
            matches.append(row["url"])
    return matches


async def _drop_item(db: aiosqlite.Connection, row: aiosqlite.Row, reason: str) -> None:
    """Delete an item row and tombstone its (feed_id, guid) against re-insert."""
    await db.execute("DELETE FROM items WHERE id = ?", (row["id"],))
    await db.execute(
        "INSERT OR IGNORE INTO unavailable_guids (feed_id, guid, marked_at) VALUES (?, ?, datetime('now'))",
        (row["feed_id"], row["guid"]),
    )
    logger.info(
        f"Dropped duplicate item {loggable(row['id'])} "
        f"(feed={loggable(row['feed_id'])} guid={loggable(row['guid'])}): {reason}"
    )


async def _newer_item_for_url(db: aiosqlite.Connection, url: str, other_urls: list[str]) -> aiosqlite.Row | None:
    """Return the item at `url` if it is newer than every item at `other_urls`.

    Returns None when there is no item at `url`, or when the item at `url` is
    the oldest of the group — in that case it is the canonical one and the
    duplicates are somebody else's problem to drop.
    """
    async with db.execute(
        "SELECT id, feed_id, guid, fetched_at FROM items WHERE media_url = ? ORDER BY fetched_at ASC LIMIT 1",
        (url,),
    ) as cur:
        candidate = await cur.fetchone()
    if candidate is None:
        return None

    placeholders = ",".join("?" * len(other_urls))
    async with db.execute(
        f"SELECT MIN(fetched_at) FROM items WHERE media_url IN ({placeholders})",  # noqa: S608
        other_urls,
    ) as cur:
        row = await cur.fetchone()
    oldest_other = row[0] if row else None

    if oldest_other is None or candidate["fetched_at"] < oldest_other:
        return None
    return candidate


async def record_media_hash(url: str, digest: str, db: aiosqlite.Connection) -> str | None:
    """Record `url`'s content digest; drop this URL's item if it duplicates another.

    Matches on exact bytes first, then — only when DEDUP_SIMILARITY is set —
    on a perceptual hash, which also catches re-encodes and resizes.

    Returns the dropped item id, or None when nothing was dropped.
    """
    phash = await _compute_phash(url)
    await db.execute(
        "INSERT OR REPLACE INTO media_hashes (url, sha256, phash) VALUES (?, ?, ?)",
        (url, digest, phash),
    )

    async with db.execute(
        "SELECT url FROM media_hashes WHERE sha256 = ? AND url != ?",
        (digest, url),
    ) as cur:
        twins = [row["url"] for row in await cur.fetchall()]

    reason = f"identical bytes to {loggable(twins[0])}" if twins else ""
    if not twins and phash is not None:
        twins = await _similar_urls(db, url, phash)
        reason = f"visually identical to {loggable(twins[0])}" if twins else ""

    if not twins:
        await db.commit()
        return None

    logger.debug(f"record_media_hash: {loggable(url)} duplicates {len(twins)} other url(s)")
    row = await _newer_item_for_url(db, url, twins)
    if row is None:
        await db.commit()
        return None

    await _drop_item(db, row, reason)
    await db.commit()
    return row["id"]
