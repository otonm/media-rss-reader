import aiosqlite
import httpx
import pytest
import respx
from httpx import AsyncClient


async def test_feeds_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/feeds")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_feeds_returns_feed_with_counts(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await db.execute("INSERT INTO feeds (id, url, title) VALUES ('f1', 'http://x.com', 'X')")
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type)"
        " VALUES ('i1', 'f1', 'g1', 'http://img.jpg', 'image')"
    )
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type, seen_at)"
        " VALUES ('i2', 'f1', 'g2', 'http://img2.jpg', 'image', datetime('now'))"
    )
    await db.commit()
    resp = await client.get("/api/feeds")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "f1"
    assert data[0]["item_count"] == 2
    assert data[0]["unseen_count"] == 1


async def test_feeds_ordered_by_title(client: AsyncClient, db: aiosqlite.Connection) -> None:
    for fid, title in (("f3", "Zeta"), ("fC", "Alpha"), ("f2", "Mid")):
        await db.execute("INSERT INTO feeds(id, url, title) VALUES (?, ?, ?)", (fid, fid, title))
    await db.commit()
    resp = await client.get("/api/feeds")
    assert [f["title"] for f in resp.json()] == ["Alpha", "Mid", "Zeta"]


# ---------------------------------------------------------------------------
# Helpers for items tests
# ---------------------------------------------------------------------------


async def _insert_feed(
    db: aiosqlite.Connection, feed_id: str = "feed1", url: str = "http://example.com/feed.xml"
) -> None:
    await db.execute(
        "INSERT INTO feeds(id, url, title) VALUES (?, ?, ?)",
        (feed_id, url, "Test Feed"),
    )
    await db.commit()


async def _insert_item(
    db: aiosqlite.Connection,
    item_id: str,
    feed_id: str,
    seen_at: str | None = None,
) -> None:
    await db.execute(
        """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date, seen_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
        (item_id, feed_id, item_id, "Title", "http://example.com/img.jpg", "image", seen_at),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# GET /api/items tests
# ---------------------------------------------------------------------------


async def test_items_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/items")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_items_returns_items(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1")
    resp = await client.get("/api/items")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    item = data[0]
    assert item["id"] == "item1"
    assert item["feed_id"] == "feed1"
    assert item["title"] == "Title"
    assert item["media_url"] == "http://example.com/img.jpg"
    assert item["media_type"] == "image"
    assert "pub_date" in item
    assert "fetched_at" in item
    assert "seen_at" in item


async def test_items_unseen_filter(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "seen_item", "feed1", seen_at="2024-01-01T00:00:00")
    await _insert_item(db, "unseen_item", "feed1", seen_at=None)
    resp = await client.get("/api/items", params={"unseen": "true"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "unseen_item"


async def test_items_feed_filter(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db, feed_id="feedA", url="http://a.com/feed.xml")
    await _insert_feed(db, feed_id="feedB", url="http://b.com/feed.xml")
    await _insert_item(db, "itemA", "feedA")
    await _insert_item(db, "itemB", "feedB")
    resp = await client.get("/api/items", params={"feed_id": "feedA"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "itemA"


async def test_items_offset(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1")
    await _insert_item(db, "item2", "feed1")
    await _insert_item(db, "item3", "feed1")
    resp_head = await client.get("/api/items", params={"offset": 0, "size": 2})
    assert resp_head.status_code == 200
    assert [i["id"] for i in resp_head.json()] == ["item1", "item2"]
    resp_rest = await client.get("/api/items", params={"offset": 2, "size": 2})
    assert resp_rest.status_code == 200
    assert [i["id"] for i in resp_rest.json()] == ["item3"]


async def test_items_unseen_offset_counts_only_remaining(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """The client's offset is how many *unseen* items it already holds.

    Page numbers used to be wrong here: marking items seen shrinks the
    unseen result set, so OFFSET page*size silently skipped items.
    """
    await _insert_feed(db)
    for n in range(1, 5):
        await _insert_item(db, f"item{n}", "feed1")

    first = await client.get("/api/items", params={"unseen": "true", "offset": 0, "size": 2})
    assert [i["id"] for i in first.json()] == ["item1", "item2"]

    # The client marks both seen; it now holds zero unseen items, so it asks
    # again from offset 0 and must get the next two, not item3 onwards skipped.
    await client.post("/api/items/item1/seen")
    await client.post("/api/items/item2/seen")

    second = await client.get("/api/items", params={"unseen": "true", "offset": 0, "size": 2})
    assert [i["id"] for i in second.json()] == ["item3", "item4"]


async def test_items_returns_media_array_from_media_json(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    media_json = (
        '[{"url": "http://example.com/a.jpg", "type": "image"}, {"url": "http://example.com/b.gif", "type": "gif"}]'
    )
    await db.execute(
        """INSERT INTO items(id, feed_id, guid, title, media_url, media_type, media_json, pub_date)
           VALUES ('g1', 'feed1', 'g1', 'Gallery', 'http://example.com/a.jpg', 'image', ?, datetime('now'))""",
        (media_json,),
    )
    await db.commit()
    resp = await client.get("/api/items")
    assert resp.status_code == 200
    (item,) = resp.json()
    assert item["media"] == [
        {"url": "http://example.com/a.jpg", "type": "image"},
        {"url": "http://example.com/b.gif", "type": "gif"},
    ]
    assert "media_json" not in item


async def test_items_without_media_json_falls_back(client: AsyncClient, db: aiosqlite.Connection) -> None:
    # Rows predating migration v5 have media_json NULL; the API must still
    # return a 1-element media array built from media_url/media_type.
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1")
    resp = await client.get("/api/items")
    (item,) = resp.json()
    assert item["media"] == [{"url": "http://example.com/img.jpg", "type": "image"}]


# ---------------------------------------------------------------------------
# POST /api/items/{id}/seen tests
# ---------------------------------------------------------------------------


async def test_mark_seen(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1", seen_at=None)
    resp = await client.post("/api/items/item1/seen")
    assert resp.status_code == 200
    data = resp.json()
    assert "seen_at" in data
    assert data["seen_at"] is not None
    # Verify DB was updated
    async with db.execute("SELECT seen_at FROM items WHERE id = 'item1'") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] is not None


async def test_mark_seen_writes_seen_media(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """seen_media is the durable record — it must outlive the items row."""
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1", seen_at=None)
    assert (await client.post("/api/items/item1/seen")).status_code == 200

    async with db.execute("SELECT media_key, seen_at FROM seen_media") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["media_key"] == "http://example.com/img.jpg"
    assert rows[0]["seen_at"] is not None

    # Deleting the item (as prune_items does) must not take the record with it.
    await db.execute("DELETE FROM items WHERE id = 'item1'")
    await db.commit()
    async with db.execute("SELECT COUNT(*) FROM seen_media") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_mark_seen_not_found(client: AsyncClient) -> None:
    resp = await client.post("/api/items/nonexistent/seen")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/media/proxy tests
# ---------------------------------------------------------------------------


async def test_proxy_cache_hit(client: AsyncClient, tmp_path: object, monkeypatch: object) -> None:
    import hashlib

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/img.jpg"
    filename = hashlib.sha256(url.encode()).hexdigest()
    (tmp_path / filename).write_bytes(b"cached")  # type: ignore[operator]

    resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 200
    assert resp.content == b"cached"


async def test_proxy_cache_hit_returns_correct_content_type(
    client: AsyncClient, tmp_path: object, monkeypatch: object
) -> None:
    """Cache hit must serve the stored Content-Type, not octet-stream.

    The cached file is named by sha256(url) with no extension, so
    FileResponse's extension-inference would otherwise return
    application/octet-stream. That breaks the browser's ability to
    animate a cached GIF.
    """
    import hashlib

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/anim.gif"
    filename = hashlib.sha256(url.encode()).hexdigest()
    (tmp_path / filename).write_bytes(b"GIF89a")  # type: ignore[operator]
    (tmp_path / f"{filename}.meta").write_text("image/gif")  # type: ignore[operator]

    resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/gif")


async def test_proxy_cache_hit_falls_back_when_sidecar_missing(
    client: AsyncClient, tmp_path: object, monkeypatch: object
) -> None:
    """Pre-sidecar cached files (no .meta sibling) must still be servable."""
    import hashlib

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/anim.gif"
    filename = hashlib.sha256(url.encode()).hexdigest()
    (tmp_path / filename).write_bytes(b"GIF89a")  # type: ignore[operator]
    # no .meta written — simulates a cache file from before sidecars existed

    resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 200
    assert resp.content == b"GIF89a"
    # No sidecar → must NOT be served as text/plain (Starlette's guess on a
    # bare-sha256 filename). octet-stream lets the browser sniff and render;
    # no nosniff on this path (F5 scopes nosniff to the miss path).
    assert resp.headers["content-type"].startswith("application/octet-stream")


async def test_proxy_cache_miss(client: AsyncClient, tmp_path: object, monkeypatch: object) -> None:
    import httpx
    import respx

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/photo.jpg"

    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(200, content=b"freshdata", headers={"content-type": "image/jpeg"})
        )
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
        resp = await client.get(f"/api/media/proxy?url={url}")
        await real_client.aclose()

    assert resp.status_code == 200
    assert resp.content == b"freshdata"
    # Sidecar must have been written so a subsequent cache hit serves the
    # correct Content-Type.
    import hashlib

    fname = hashlib.sha256(url.encode()).hexdigest()
    assert (tmp_path / f"{fname}.meta").read_text() == "image/jpeg"  # type: ignore[operator]


async def test_proxy_upstream_error(
    client: AsyncClient,
    tmp_path: object,
    monkeypatch: object,
    db: aiosqlite.Connection,
) -> None:
    import httpx
    import respx

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/broken.jpg"

    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
        resp = await client.get(f"/api/media/proxy?url={url}")
        await real_client.aclose()

    assert resp.status_code == 502
    async with db.execute("SELECT url FROM dead_urls") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == [url]


async def test_proxy_404_marks_item_unavailable(
    client: AsyncClient,
    tmp_path: object,
    monkeypatch: object,
    db: aiosqlite.Connection,
) -> None:
    """When the upstream returns 404, the proxy must mark the item's URL
    dead so the post can be dropped once every URL of its gallery is dead."""
    import httpx
    import respx

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))

    feed_id = "f1"
    item_id = "i1"
    url = "http://example.com/broken.jpg"
    await db.execute(
        "INSERT INTO feeds (id, url, title) VALUES (?, ?, ?)",
        (feed_id, "http://feed.example.com", "F"),
    )
    await db.execute(
        """INSERT INTO items (id, feed_id, guid, title, media_url, media_type)
           VALUES (?, ?, ?, ?, ?, 'image')""",
        (item_id, feed_id, "g1", "T", url),
    )
    await db.commit()

    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
        resp = await client.get(f"/api/media/proxy?url={url}&item_id={item_id}")
        await real_client.aclose()

    assert resp.status_code == 502
    async with db.execute("SELECT url FROM dead_urls") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == [url]
    # Single-media post: all (1) URLs are dead -> item should be gone.
    async with db.execute("SELECT id FROM items WHERE id = ?", (item_id,)) as cur:
        assert await cur.fetchone() is None
    async with db.execute("SELECT guid FROM unavailable_guids WHERE feed_id = ?", (feed_id,)) as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["g1"]


async def test_proxy_404_without_item_id_still_returns_502(
    client: AsyncClient,
    tmp_path: object,
    monkeypatch: object,
    db: aiosqlite.Connection,
) -> None:
    """Backwards compat: item_id is optional, missing item_id must not
    break the 502 contract -- the URL still gets marked dead."""
    import httpx
    import respx

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/broken.jpg"

    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real_client)
        resp = await client.get(f"/api/media/proxy?url={url}")
        await real_client.aclose()

    assert resp.status_code == 502
    async with db.execute("SELECT url FROM dead_urls") as cur:
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == [url]


# ---------------------------------------------------------------------------
# POST /api/prefetch/hint tests
# ---------------------------------------------------------------------------


async def test_prefetch_hint(client: AsyncClient, db: aiosqlite.Connection, monkeypatch: object) -> None:
    import httpx

    import src.api.media as media_mod

    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x.com/feed', 'F')")
    await db.execute(
        "INSERT INTO items(id, feed_id, guid, title, media_url, media_type, pub_date) "
        "VALUES ('i1', 'f1', 'g1', 'T', 'http://x.com/img.jpg', 'image', datetime('now'))"
    )
    await db.commit()

    monkeypatch.setattr(media_mod, "get_http_client", lambda: httpx.AsyncClient())
    resp = await client.post("/api/prefetch/hint", json={"item_id": "i1"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_prefetch_hint_missing_item_id(client: AsyncClient) -> None:
    resp = await client.post("/api/prefetch/hint", json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/reddit-feeds/status tests
# ---------------------------------------------------------------------------


async def test_reddit_feeds_status_success(
    client: AsyncClient, mock_http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream_json = {
        "feeds": [
            {
                "name": "EarthPorn",
                "last_status": "success",
                "last_fetch": "2026-07-27T14:02:00.123456+00:00",
                "last_item_count": 5,
                "total_items": 42,
            }
        ],
        "last_run": "2026-07-27T14:02:05.654321+00:00",
    }
    mock_http.get("http://127.0.0.1:9090/status").mock(return_value=httpx.Response(200, json=upstream_json))
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
    resp = await client.get("/api/reddit-feeds/status")
    await real_client.aclose()
    assert resp.status_code == 200
    assert resp.json() == upstream_json


async def test_reddit_feeds_status_unreachable(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.api.reddit_feeds as rf_mod

    fake_client = httpx.AsyncClient()

    async def fake_get(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    fake_client.get = fake_get  # type: ignore[method-assign]
    monkeypatch.setattr(rf_mod, "get_http_client", lambda: fake_client)

    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 502


async def test_reddit_feeds_status_upstream_error(
    client: AsyncClient, mock_http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_http.get("http://127.0.0.1:9090/status").mock(return_value=httpx.Response(500))
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
    resp = await client.get("/api/reddit-feeds/status")
    await real_client.aclose()
    assert resp.status_code == 500


async def test_reddit_feeds_status_pending_status(
    client: AsyncClient, mock_http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream_json = {
        "feeds": [
            {
                "name": "EarthPorn",
                "last_status": "pending",
                "last_fetch": "2026-07-27T14:02:00.123456+00:00",
                "last_item_count": 5,
                "total_items": 42,
            },
            {
                "name": "Python",
                "last_status": "success",
                "last_fetch": "2026-07-27T14:02:02.123456+00:00",
                "last_item_count": 3,
                "total_items": 18,
            },
        ],
        "last_run": "2026-07-27T14:02:05.654321+00:00",
    }
    mock_http.get("http://127.0.0.1:9090/status").mock(return_value=httpx.Response(200, json=upstream_json))
    real_client = httpx.AsyncClient()
    monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
    resp = await client.get("/api/reddit-feeds/status")
    await real_client.aclose()
    assert resp.status_code == 200
    assert resp.json() == upstream_json


@pytest.mark.asyncio
async def test_items_interleaved_across_feeds(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """Items from multiple feeds should be interleaved round-robin, oldest first."""
    import datetime
    import hashlib  # noqa: F401

    async def insert_feed(fid: str, url: str) -> None:
        await db.execute("INSERT INTO feeds (id, url) VALUES (?, ?)", (fid, url))

    async def insert_item(iid: str, feed_id: str, guid: str, pub_offset_days: int) -> None:
        pub = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=pub_offset_days)).isoformat()
        await db.execute(
            "INSERT INTO items (id, feed_id, guid, title, media_url, media_type, pub_date, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (iid, feed_id, guid, "t", "http://x.com/a.jpg", "image", pub),
        )

    fa, fb = "feedA", "feedB"
    await insert_feed(fa, "http://a.com")
    await insert_feed(fb, "http://b.com")
    # Feed A: 3 items (oldest pub_date = 3 days ago)
    await insert_item("a1", fa, "g1", pub_offset_days=3)
    await insert_item("a2", fa, "g2", pub_offset_days=2)
    await insert_item("a3", fa, "g3", pub_offset_days=1)
    # Feed B: 2 items
    await insert_item("b1", fb, "g1", pub_offset_days=4)
    await insert_item("b2", fb, "g2", pub_offset_days=1)
    await db.commit()

    resp = await client.get("/api/items?offset=0&size=10")
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()]

    # Round 1: rn=1 for each feed — feedA (oldest=3d) and feedB (oldest=4d)
    # Round 2: rn=2 for each feed
    # Round 3: only feedA remains (rn=3)
    # Within each round, feedA < feedB alphabetically
    assert ids == ["a1", "b1", "a2", "b2", "a3"]


async def test_items_rejects_invalid_size(client: AsyncClient) -> None:
    for bad in (0, -1, 201, 100000):
        resp = await client.get("/api/items", params={"size": bad})
        assert resp.status_code == 422, f"size={bad} should be rejected"


async def test_items_report_whether_media_is_already_cached(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: object, monkeypatch: object
) -> None:
    """The browser downloads cached items first, so it has to be told which are cached.

    A cached item decodes in milliseconds; an uncached one waits on the origin.
    Without this flag the queue orders blindly and a miss at the front of the
    lookahead holds up hits behind it.
    """
    import hashlib

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    warm = "http://example.com/warm.jpg"
    cold = "http://example.com/cold.jpg"
    (tmp_path / hashlib.sha256(warm.encode()).hexdigest()).write_bytes(b"cached")  # type: ignore[operator]

    await db.execute("INSERT INTO feeds (id, url, title) VALUES ('f1', 'http://x.com', 'X')")
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type, pub_date)"
        " VALUES ('i1', 'f1', 'g1', ?, 'image', '2024-01-01')",
        (warm,),
    )
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type, pub_date)"
        " VALUES ('i2', 'f1', 'g2', ?, 'image', '2024-01-02')",
        (cold,),
    )
    await db.commit()

    resp = await client.get("/api/items")
    assert resp.status_code == 200
    cached_by_id = {i["id"]: i["cached"] for i in resp.json()}
    assert cached_by_id == {"i1": True, "i2": False}


async def test_items_cached_excludes_meta_and_tmp(
    client: AsyncClient,
    db: aiosqlite.Connection,
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    warm = "http://example.com/warm.jpg"
    (tmp_path / hashlib.sha256(warm.encode()).hexdigest()).write_bytes(b"x")
    (tmp_path / "abc123.tmp").write_bytes(b"partial")  # in-flight, must not count
    await db.execute("INSERT INTO feeds(id,url,title) VALUES ('f1','http://x','X')")
    await db.execute(
        "INSERT INTO items(id,feed_id,guid,media_url,media_type,pub_date)"
        " VALUES ('i1','f1','g1',?,'image','2026-01-01')",
        (warm,),
    )
    await db.commit()
    resp = await client.get("/api/items")
    assert resp.json()[0]["cached"] is True
