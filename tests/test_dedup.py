import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import aiosqlite
import pytest

from src.media import cache as cache_mod
from src.media.dedup import record_media_hash


async def _drain(url: str, chunks: AsyncIterator[bytes]) -> None:
    async for _ in cache_mod.cache_stream_tee(url, chunks):
        pass


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def _add_item(
    db: aiosqlite.Connection,
    item_id: str,
    feed_id: str,
    guid: str,
    media_url: str,
    fetched_at: str,
) -> None:
    await db.execute("INSERT OR IGNORE INTO feeds (id, url, title) VALUES (?, ?, ?)", (feed_id, feed_id, feed_id))
    await db.execute(
        """INSERT INTO items (id, feed_id, guid, media_url, media_key, media_type, fetched_at)
           VALUES (?, ?, ?, ?, ?, 'image', ?)""",
        (item_id, feed_id, guid, media_url, media_url, fetched_at),
    )
    await db.commit()


async def test_record_media_hash_stores_digest(db: aiosqlite.Connection) -> None:
    dropped = await record_media_hash("https://a.example.com/x.jpg", "d" * 64, db)

    assert dropped is None
    async with db.execute("SELECT url, sha256 FROM media_hashes") as cur:
        rows = await cur.fetchall()
    assert [(r["url"], r["sha256"]) for r in rows] == [("https://a.example.com/x.jpg", "d" * 64)]


async def test_record_media_hash_drops_newer_duplicate(db: aiosqlite.Connection) -> None:
    """Two URLs with identical bytes: the newer item is dropped and tombstoned."""
    digest = "a" * 64
    await _add_item(db, "item-old", "feed-a", "guid-a", "https://a.example.com/x.jpg", "2026-01-01 00:00:00")
    await _add_item(db, "item-new", "feed-b", "guid-b", "https://b.example.com/y.jpg", "2026-01-02 00:00:00")

    assert await record_media_hash("https://a.example.com/x.jpg", digest, db) is None
    assert await record_media_hash("https://b.example.com/y.jpg", digest, db) == "item-new"

    async with db.execute("SELECT id FROM items") as cur:
        assert [r["id"] for r in await cur.fetchall()] == ["item-old"]
    async with db.execute("SELECT feed_id, guid FROM resolved_guids") as cur:
        assert [(r["feed_id"], r["guid"]) for r in await cur.fetchall()] == [("feed-b", "guid-b")]


async def test_record_media_hash_keeps_the_older_item(db: aiosqlite.Connection) -> None:
    """Hashing the older URL second must not drop the older (canonical) item."""
    digest = "b" * 64
    await _add_item(db, "item-old", "feed-a", "guid-a", "https://a.example.com/x.jpg", "2026-01-01 00:00:00")
    await _add_item(db, "item-new", "feed-b", "guid-b", "https://b.example.com/y.jpg", "2026-01-02 00:00:00")

    # Newer URL hashed first, then the older one — the older must survive.
    assert await record_media_hash("https://b.example.com/y.jpg", digest, db) is None
    assert await record_media_hash("https://a.example.com/x.jpg", digest, db) is None

    async with db.execute("SELECT id FROM items ORDER BY id") as cur:
        assert [r["id"] for r in await cur.fetchall()] == ["item-new", "item-old"]


async def test_record_media_hash_ignores_distinct_content(db: aiosqlite.Connection) -> None:
    await _add_item(db, "item-a", "feed-a", "guid-a", "https://a.example.com/x.jpg", "2026-01-01 00:00:00")
    await _add_item(db, "item-b", "feed-b", "guid-b", "https://b.example.com/y.jpg", "2026-01-02 00:00:00")

    await record_media_hash("https://a.example.com/x.jpg", "1" * 64, db)
    assert await record_media_hash("https://b.example.com/y.jpg", "2" * 64, db) is None

    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 2


async def test_record_media_hash_is_idempotent(db: aiosqlite.Connection) -> None:
    """Re-hashing the same URL must not drop the item it belongs to."""
    digest = "c" * 64
    await _add_item(db, "item-a", "feed-a", "guid-a", "https://a.example.com/x.jpg", "2026-01-01 00:00:00")

    for _ in range(3):
        assert await record_media_hash("https://a.example.com/x.jpg", digest, db) is None

    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 1
    async with db.execute("SELECT COUNT(*) FROM media_hashes") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_dropped_duplicate_log_escapes_a_hostile_guid(
    db: aiosqlite.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """A feed is a trust boundary too: a guid with an embedded newline must not
    forge a second log line (minor 6)."""
    hostile_guid = "g1\nERROR fake injected line"
    digest = "f" * 64
    await _add_item(db, "item-old", "feed-a", "guid-a", "https://a.example.com/x.jpg", "2026-01-01 00:00:00")
    await _add_item(db, "item-new", "feed-b", hostile_guid, "https://b.example.com/y.jpg", "2026-01-02 00:00:00")

    with caplog.at_level(logging.INFO, logger="src.media.dedup"):
        assert await record_media_hash("https://a.example.com/x.jpg", digest, db) is None
        assert await record_media_hash("https://b.example.com/y.jpg", digest, db) == "item-new"

    record = next(m for m in caplog.messages if "Dropped duplicate" in m)
    assert "\n" not in record
    assert repr(hostile_guid) in record


async def test_record_media_hash_tombstone_blocks_reinsert(db: aiosqlite.Connection, tmp_path: Path) -> None:
    """The tombstone written on drop must stop the next feed poll re-adding it."""
    import httpx
    import respx

    from src.feeds.fetcher import _feed_id
    from src.feeds.sync import refresh_all_feeds

    feed_url = "https://b.example.com/feed.xml"
    feed_id = _feed_id(feed_url)
    rss = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>B</title>
  <item><guid>guid-b</guid>
    <enclosure url="https://b.example.com/y.jpg" type="image/jpeg" length="0"/>
  </item>
</channel></rss>"""

    digest = "e" * 64
    await _add_item(db, "item-old", "feed-a", "guid-a", "https://a.example.com/x.jpg", "2026-01-01 00:00:00")
    await _add_item(db, "item-new", feed_id, "guid-b", "https://b.example.com/y.jpg", "2026-01-02 00:00:00")

    await record_media_hash("https://a.example.com/x.jpg", digest, db)
    assert await record_media_hash("https://b.example.com/y.jpg", digest, db) == "item-new"

    with respx.mock:
        respx.get(feed_url).mock(return_value=httpx.Response(200, text=rss))
        await db.execute("UPDATE feeds SET url = ? WHERE id = ?", (feed_url, feed_id))
        await db.commit()
        async with httpx.AsyncClient() as client:
            await refresh_all_feeds(db, client)

    async with db.execute("SELECT id FROM items") as cur:
        assert [r["id"] for r in await cur.fetchall()] == ["item-old"]


# --- Tier C: perceptual hashing (DEDUP_SIMILARITY) ---------------------------


def _write_image(
    path: Path,
    colour_fn: Callable[[int, int], tuple[int, int, int]],
    size: int = 200,
    **save_kwargs: object,
) -> None:
    """Render a deterministic gradient/pattern image to `path`."""
    from PIL import Image

    img = Image.new("RGB", (size, size))
    img.putdata([colour_fn(x, y) for y in range(size) for x in range(size)])
    img.save(path, **save_kwargs)


def _gradient(x: int, y: int) -> tuple[int, int, int]:
    return (x % 256, y % 256, (x + y) % 256)


def _checkers(x: int, y: int) -> tuple[int, int, int]:
    v = 255 if (x // 20 + y // 20) % 2 else 0
    return (v, v, v)


async def _cache_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, src: Path) -> None:
    """Place `src`'s bytes into the media cache under `url`."""
    from src.config import settings

    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    payload = src.read_bytes()  # noqa: ASYNC240
    await _drain(url, _chunks(payload))


async def test_phash_survives_recompression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same picture re-encoded as a lossy JPEG hashes within the threshold."""
    from src.config import settings
    from src.media.dedup import PHASH_BITS, _compute_phash

    monkeypatch.setattr(settings, "dedup_similarity", 97)

    png = tmp_path / "a.png"
    jpg = tmp_path / "a.jpg"
    _write_image(png, _gradient)
    _write_image(jpg, _gradient, quality=70)

    await _cache_file(tmp_path, monkeypatch, "https://a.example.com/a.png", png)
    await _cache_file(tmp_path, monkeypatch, "https://b.example.com/a.jpg", jpg)

    h1 = await _compute_phash("https://a.example.com/a.png")
    h2 = await _compute_phash("https://b.example.com/a.jpg")

    assert h1 is not None and h2 is not None
    assert len(h1) == PHASH_BITS // 4  # exactly 256 bits
    distance = (int(h1, 16) ^ int(h2, 16)).bit_count()
    assert (PHASH_BITS - distance) * 100 // PHASH_BITS > 97, f"hamming distance {distance} too large"


async def test_phash_separates_different_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.config import settings
    from src.media.dedup import PHASH_BITS, _compute_phash

    monkeypatch.setattr(settings, "dedup_similarity", 97)

    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _write_image(a, _gradient)
    _write_image(b, _checkers)

    await _cache_file(tmp_path, monkeypatch, "https://a.example.com/a.png", a)
    await _cache_file(tmp_path, monkeypatch, "https://b.example.com/b.png", b)

    h1 = await _compute_phash("https://a.example.com/a.png")
    h2 = await _compute_phash("https://b.example.com/b.png")

    distance = (int(h1, 16) ^ int(h2, 16)).bit_count()
    assert (PHASH_BITS - distance) * 100 // PHASH_BITS <= 97, f"hamming distance {distance} too small"


async def test_phash_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dedup_similarity = 0 short-circuits before touching the cache or PIL."""
    from src.config import settings
    from src.media.dedup import _compute_phash

    monkeypatch.setattr(settings, "dedup_similarity", 0)

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("cache_read must not be called when dedup_similarity is 0")

    monkeypatch.setattr("src.media.dedup.cache_read", _explode)
    assert await _compute_phash("https://a.example.com/a.png") is None


async def test_phash_skips_undecodable_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Video (and any non-image) yields no perceptual hash rather than an error."""
    from src.config import settings
    from src.media.dedup import _compute_phash

    monkeypatch.setattr(settings, "dedup_similarity", 97)
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    await _drain("https://a.example.com/clip.mp4", _chunks(b"\x00\x00\x00\x20ftypmp42"))

    assert await _compute_phash("https://a.example.com/clip.mp4") is None


async def test_record_media_hash_drops_visually_identical(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-encode with different bytes is still dropped when similarity is on."""
    from src.config import settings

    monkeypatch.setattr(settings, "dedup_similarity", 97)

    png, jpg = tmp_path / "a.png", tmp_path / "a.jpg"
    _write_image(png, _gradient)
    _write_image(jpg, _gradient, quality=70)
    await _cache_file(tmp_path, monkeypatch, "https://a.example.com/a.png", png)
    await _cache_file(tmp_path, monkeypatch, "https://b.example.com/a.jpg", jpg)

    await _add_item(db, "item-old", "feed-a", "guid-a", "https://a.example.com/a.png", "2026-01-01 00:00:00")
    await _add_item(db, "item-new", "feed-b", "guid-b", "https://b.example.com/a.jpg", "2026-01-02 00:00:00")

    assert await record_media_hash("https://a.example.com/a.png", "1" * 64, db) is None
    # Different sha256 — only the perceptual hash can catch this one.
    assert await record_media_hash("https://b.example.com/a.jpg", "2" * 64, db) == "item-new"

    async with db.execute("SELECT id FROM items") as cur:
        assert [r["id"] for r in await cur.fetchall()] == ["item-old"]


async def test_record_media_hash_keeps_visually_different(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.config import settings

    monkeypatch.setattr(settings, "dedup_similarity", 97)

    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _write_image(a, _gradient)
    _write_image(b, _checkers)
    await _cache_file(tmp_path, monkeypatch, "https://a.example.com/a.png", a)
    await _cache_file(tmp_path, monkeypatch, "https://b.example.com/b.png", b)

    await _add_item(db, "item-a", "feed-a", "guid-a", "https://a.example.com/a.png", "2026-01-01 00:00:00")
    await _add_item(db, "item-b", "feed-b", "guid-b", "https://b.example.com/b.png", "2026-01-02 00:00:00")

    await record_media_hash("https://a.example.com/a.png", "1" * 64, db)
    assert await record_media_hash("https://b.example.com/b.png", "2" * 64, db) is None

    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 2
