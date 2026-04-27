# Media RSS Reader — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-hosted media RSS reader that continuously fetches image/GIF/video items from RSS feeds (OPML-driven) and serves them in a scrollable or slideshow UI, all in a single Docker container.

**Architecture:** Strict outside-in TDD across 6 phases (scaffold → DB → feeds → media → API → frontend → Docker). Each phase closes with a fully-green `ruff check + pytest` run (90 % coverage gate) before the next begins. FastAPI app with aiosqlite, APScheduler background jobs, and a vanilla-JS single-page UI; no external services.

**Tech Stack:** Python 3.14, uv, FastAPI, aiosqlite, feedparser, listparser, httpx, APScheduler, pydantic-settings, pytest + pytest-asyncio + respx, ruff, Docker

---

## File Map

```
pyproject.toml                   project metadata, deps, ruff, pytest, coverage config
src/__init__.py                  makes src a package (enables from src.x imports)
src/config.py                    pydantic-settings Settings class; module-level settings singleton
src/main.py                      FastAPI app; lifespan hook; index.html injection; route includes
src/scheduler.py                 APScheduler setup; start/stop helpers; shared httpx client
src/db/__init__.py
src/db/connection.py             open_db(); get_db() FastAPI dependency; WAL + FK pragma
src/db/schema.py                 create_schema(db): idempotent CREATE TABLE / INDEX
src/db/migrations.py             run_migrations(db): PRAGMA user_version counter
src/feeds/__init__.py
src/feeds/opml.py                parse_opml(path) → list[{url, title}]
src/feeds/fetcher.py             fetch_feed(url, client) → list[item dicts]
src/feeds/sync.py                opml_sync(); refresh_feed(); refresh_all_feeds()
src/media/__init__.py
src/media/detector.py            detect_type(url); detect_media(entry) → (url, type) | None
src/media/cache.py               cache_write(); cache_read(); evict()
src/media/prefetch.py            prefetch_ahead(item_id, db, client) — fire-and-forget
src/api/__init__.py
src/api/feeds.py                 GET /api/feeds
src/api/items.py                 GET /api/items; POST /api/items/{id}/seen
src/api/media.py                 GET /api/media/proxy; POST /api/prefetch/hint; GET /api/status
src/static/index.html            SPA shell with <!-- SLIDESHOW_TRANSITION --> sentinel
src/static/style.css             CSS custom-property themes; slideshow A/B layers
src/static/app.js                all UI logic
tests/conftest.py                db fixture; client fixture; mock_http fixture
tests/test_db.py                 schema, migrations, connection
tests/test_config.py             Settings defaults and env-var override
tests/test_opml.py               valid, missing, empty OPML
tests/test_detector.py           media type detection
tests/test_fetcher.py            feed fetch, item extraction, dedup
tests/test_sync.py               opml_sync, refresh, cascade delete
tests/test_cache.py              write/read, evict by count, evict by age
tests/test_api.py                all endpoints
Dockerfile
docker-compose.yml
.gitignore
feeds.opml                       sample OPML for docker-compose
```

---

## Phase 1 — Scaffold + DB Layer

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `src/__init__.py`, `src/db/__init__.py`, `tests/__init__.py`

- [ ] **Step 1: Create pyproject.toml**

```toml
[project]
name = "media-rss-reader"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = [
    "fastapi",
    "uvicorn[standard]",
    "httpx",
    "feedparser",
    "listparser",
    "apscheduler",
    "aiosqlite",
    "pydantic-settings",
]

[project.optional-dependencies]
dev = [
    "ruff",
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "respx",
]

[tool.ruff]
target-version = "py314"
line-length = 120

[tool.ruff.lint]
select = ["E", "W", "F", "I", "UP", "B", "SIM", "ANN", "ASYNC"]
ignore = ["ANN101", "ANN102"]

[tool.ruff.lint.isort]
known-first-party = ["src"]

[tool.ruff.format]
quote-style = "double"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["."]
addopts = "--cov=src --cov-report=term-missing --cov-report=html:htmlcov --cov-fail-under=90"

[tool.coverage.run]
source = ["src"]
omit = ["src/static/*"]

[tool.coverage.report]
exclude_lines = [
    "pragma: no cover",
    "if TYPE_CHECKING:",
    "raise NotImplementedError",
]
```

- [ ] **Step 2: Create empty init files**

```bash
mkdir -p src/db src/feeds src/media src/api src/static tests
touch src/__init__.py src/db/__init__.py src/feeds/__init__.py
touch src/media/__init__.py src/api/__init__.py tests/__init__.py
```

- [ ] **Step 3: Install dependencies**

```bash
uv sync --extra dev
```

Expected: resolves and installs all deps, creates `uv.lock`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock src/ tests/
git commit -m "chore: project scaffold — pyproject.toml, uv.lock, package dirs"
```

---

### Task 2: Config

**Files:**
- Create: `src/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_config.py
from src.config import Settings


def test_settings_defaults() -> None:
    s = Settings()
    assert s.port == 8080
    assert s.log_level == "info"
    assert s.prefetch_ahead == 5
    assert s.cache_max_items == 500
    assert s.opml_sync_interval == 3600


def test_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "9090")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    s = Settings()
    assert s.port == 9090
    assert s.log_level == "debug"
```

Add `import pytest` at the top.

- [ ] **Step 2: Run test — verify it fails**

```bash
uv run pytest tests/test_config.py -v
```

Expected: `ImportError: cannot import name 'Settings' from 'src.config'`

- [ ] **Step 3: Implement src/config.py**

```python
# src/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    opml_path: str = "/data/feeds.opml"
    db_path: str = "/data/reader.db"
    opml_sync_interval: int = 3600
    feed_refresh_interval: int = 900
    cache_dir: str = "/cache"
    cache_max_items: int = 500
    cache_max_age_hours: int = 48
    prefetch_ahead: int = 5
    image_display_delay_ms: int = 5000
    slideshow_transition_ms: int = 400
    port: int = 8080
    log_level: str = "info"


settings = Settings()
```

- [ ] **Step 4: Run test — verify it passes**

```bash
uv run pytest tests/test_config.py -v
```

Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat: config module with pydantic-settings"
```

---

### Task 3: DB schema

**Files:**
- Create: `src/db/schema.py`
- Create: `tests/conftest.py` (db fixture only)
- Create: `tests/test_db.py` (schema tests)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_db.py
import aiosqlite
import pytest

from src.db.schema import create_schema


async def test_schema_creates_feeds_table() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='feeds'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    await db.close()


async def test_schema_creates_items_table() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='items'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    await db.close()


async def test_schema_creates_indexes() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    async with db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name LIKE 'idx_items_%'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == 3
    await db.close()


async def test_schema_is_idempotent() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    await create_schema(db)  # must not raise
    await db.close()
```

Write the conftest db fixture (needed from Task 4 onward):

```python
# tests/conftest.py
from collections.abc import AsyncGenerator

import aiosqlite
import pytest

from src.db.migrations import run_migrations
from src.db.schema import create_schema


@pytest.fixture
async def db() -> AsyncGenerator[aiosqlite.Connection, None]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await create_schema(conn)
    await run_migrations(conn)
    yield conn
    await conn.close()
```

Note: `run_migrations` doesn't exist yet — conftest will only be imported by tasks that use the `db` fixture, so the ImportError won't block Task 3 tests which import directly.

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_db.py -v
```

Expected: `ImportError: cannot import name 'create_schema'`

- [ ] **Step 3: Implement src/db/schema.py**

```python
# src/db/schema.py
import aiosqlite

_CREATE_FEEDS = """
CREATE TABLE IF NOT EXISTS feeds (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL UNIQUE,
    title           TEXT,
    last_fetched_at TIMESTAMP,
    created_at      TIMESTAMP DEFAULT (datetime('now'))
)
"""

_CREATE_ITEMS = """
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,
    feed_id     TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid        TEXT NOT NULL,
    title       TEXT,
    media_url   TEXT NOT NULL,
    media_type  TEXT NOT NULL,
    pub_date    TIMESTAMP,
    fetched_at  TIMESTAMP DEFAULT (datetime('now')),
    seen_at     TIMESTAMP,
    UNIQUE(feed_id, guid)
)
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_items_feed_id  ON items(feed_id)",
    "CREATE INDEX IF NOT EXISTS idx_items_pub_date ON items(pub_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_items_seen_at  ON items(seen_at)",
]


async def create_schema(db: aiosqlite.Connection) -> None:
    await db.execute(_CREATE_FEEDS)
    await db.execute(_CREATE_ITEMS)
    for sql in _CREATE_INDEXES:
        await db.execute(sql)
    await db.commit()
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_db.py -v
```

Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/db/schema.py tests/conftest.py tests/test_db.py
git commit -m "feat: DB schema — feeds and items tables with indexes"
```

---

### Task 4: DB migrations

**Files:**
- Create: `src/db/migrations.py`
- Modify: `tests/test_db.py` (add migration tests)

- [ ] **Step 1: Add failing tests to tests/test_db.py**

Append to `tests/test_db.py`:

```python
from src.db.migrations import MIGRATIONS, run_migrations


async def test_migrations_sets_user_version() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    await run_migrations(db)
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    assert row[0] == len(MIGRATIONS)
    await db.close()


async def test_migrations_are_idempotent() -> None:
    db = await aiosqlite.connect(":memory:")
    await create_schema(db)
    await run_migrations(db)
    await run_migrations(db)  # second call must not re-apply
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    assert row[0] == len(MIGRATIONS)
    await db.close()
```

- [ ] **Step 2: Run new tests — verify they fail**

```bash
uv run pytest tests/test_db.py::test_migrations_sets_user_version -v
```

Expected: `ImportError: cannot import name 'MIGRATIONS'`

- [ ] **Step 3: Implement src/db/migrations.py**

```python
# src/db/migrations.py
import aiosqlite

# Each entry is a SQL string for one migration step.
# The list index + 1 is the migration number.
# Extend this list to add future migrations.
MIGRATIONS: list[str] = []


async def run_migrations(db: aiosqlite.Connection) -> None:
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    current_version: int = row[0]
    pending = MIGRATIONS[current_version:]
    for sql in pending:
        await db.execute(sql)
    new_version = len(MIGRATIONS)
    await db.execute(f"PRAGMA user_version = {new_version}")
    await db.commit()
```

- [ ] **Step 4: Run all DB tests**

```bash
uv run pytest tests/test_db.py -v
```

Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add src/db/migrations.py tests/test_db.py
git commit -m "feat: DB migrations with PRAGMA user_version versioning"
```

---

### Task 5: DB connection

**Files:**
- Create: `src/db/connection.py`
- Modify: `tests/test_db.py` (add connection tests)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_db.py`:

```python
import os
import tempfile

from src.db.connection import open_db


async def test_open_db_sets_wal_and_fk() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = await open_db(path)
        async with db.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        assert row[0] == "wal"
        async with db.execute("PRAGMA foreign_keys") as cur:
            row = await cur.fetchone()
        assert row[0] == 1
        await db.close()
    finally:
        os.unlink(path)


async def test_open_db_sets_row_factory() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = await open_db(path)
        await db.execute("CREATE TABLE t (x INTEGER)")
        await db.execute("INSERT INTO t VALUES (42)")
        async with db.execute("SELECT x FROM t") as cur:
            row = await cur.fetchone()
        assert row["x"] == 42
        await db.close()
    finally:
        os.unlink(path)
```

- [ ] **Step 2: Run new tests — verify they fail**

```bash
uv run pytest tests/test_db.py::test_open_db_sets_wal_and_fk -v
```

Expected: `ImportError: cannot import name 'open_db'`

- [ ] **Step 3: Implement src/db/connection.py**

```python
# src/db/connection.py
from collections.abc import AsyncGenerator

import aiosqlite

from src.config import settings


async def open_db(path: str | None = None) -> aiosqlite.Connection:
    db = await aiosqlite.connect(path or settings.db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    db = await open_db()
    try:
        yield db
    finally:
        await db.close()
```

- [ ] **Step 4: Run full phase-1 tests and coverage check**

```bash
uv run pytest tests/test_db.py tests/test_config.py -v
uv run ruff check .
```

Expected: all tests pass, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/db/connection.py tests/test_db.py
git commit -m "feat: DB connection with WAL mode and get_db FastAPI dependency"
```

---

## Phase 2 — Feed Ingestion

### Task 6: OPML parsing

**Files:**
- Create: `src/feeds/opml.py`
- Create: `tests/test_opml.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_opml.py
from pathlib import Path

import pytest

from src.feeds.opml import parse_opml

_VALID_OPML = """\
<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Test</title></head>
  <body>
    <outline type="rss" text="Feed One" xmlUrl="https://example.com/feed1.xml"/>
    <outline type="rss" text="Feed Two" xmlUrl="https://example.com/feed2.xml"/>
  </body>
</opml>"""


def test_parse_valid_opml(tmp_path: Path) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_VALID_OPML)
    feeds = parse_opml(str(f))
    assert len(feeds) == 2
    assert feeds[0]["url"] == "https://example.com/feed1.xml"
    assert feeds[0]["title"] == "Feed One"
    assert feeds[1]["url"] == "https://example.com/feed2.xml"


def test_parse_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        parse_opml("/nonexistent/path/feeds.opml")


def test_parse_empty_opml(tmp_path: Path) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(
        '<?xml version="1.0"?><opml version="2.0"><head/><body/></opml>'
    )
    feeds = parse_opml(str(f))
    assert feeds == []


def test_parse_uses_url_as_fallback_title(tmp_path: Path) -> None:
    opml = """\
<?xml version="1.0"?>
<opml version="2.0"><head/><body>
  <outline type="rss" xmlUrl="https://example.com/no-title.xml"/>
</body></opml>"""
    f = tmp_path / "feeds.opml"
    f.write_text(opml)
    feeds = parse_opml(str(f))
    assert feeds[0]["title"] == "https://example.com/no-title.xml"
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_opml.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Implement src/feeds/opml.py**

```python
# src/feeds/opml.py
import listparser


def parse_opml(path: str) -> list[dict[str, str]]:
    with open(path) as f:
        result = listparser.parse(f.read())
    return [
        {"url": feed.url, "title": feed.title or feed.url}
        for feed in result.feeds
        if feed.url
    ]
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_opml.py -v
```

Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/feeds/opml.py tests/test_opml.py
git commit -m "feat: OPML parser using listparser"
```

---

### Task 7: Media type detector

**Files:**
- Create: `src/media/detector.py`
- Create: `tests/test_detector.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_detector.py
from src.media.detector import detect_media, detect_type


def test_detect_type_jpeg() -> None:
    assert detect_type("https://example.com/photo.jpg") == "image"


def test_detect_type_png() -> None:
    assert detect_type("https://example.com/photo.png") == "image"


def test_detect_type_gif() -> None:
    assert detect_type("https://example.com/anim.gif") == "gif"


def test_detect_type_mp4() -> None:
    assert detect_type("https://example.com/clip.mp4") == "video"


def test_detect_type_webm() -> None:
    assert detect_type("https://example.com/clip.webm") == "video"


def test_detect_type_unknown_returns_none() -> None:
    assert detect_type("https://example.com/doc.pdf") is None


def test_detect_type_strips_query_string() -> None:
    assert detect_type("https://example.com/photo.jpg?v=1") == "image"


def test_detect_media_from_enclosure() -> None:
    entry = {"enclosures": [{"url": "https://example.com/photo.jpg", "type": "image/jpeg"}]}
    assert detect_media(entry) == ("https://example.com/photo.jpg", "image")


def test_detect_media_from_media_content() -> None:
    entry = {
        "enclosures": [],
        "media_content": [{"url": "https://example.com/anim.gif"}],
    }
    assert detect_media(entry) == ("https://example.com/anim.gif", "gif")


def test_detect_media_from_media_thumbnail() -> None:
    entry = {
        "enclosures": [],
        "media_content": [],
        "media_thumbnail": [{"url": "https://example.com/thumb.jpg"}],
    }
    assert detect_media(entry) == ("https://example.com/thumb.jpg", "image")


def test_detect_media_from_og_image() -> None:
    entry = {
        "enclosures": [],
        "media_content": [],
        "media_thumbnail": [],
        "summary": '<meta property="og:image" content="https://example.com/og.png"/>',
    }
    assert detect_media(entry) == ("https://example.com/og.png", "image")


def test_detect_media_returns_none_when_no_media() -> None:
    entry = {"enclosures": [], "summary": "<p>Text only</p>"}
    assert detect_media(entry) is None


def test_detect_media_enclosure_takes_priority_over_media_content() -> None:
    entry = {
        "enclosures": [{"url": "https://example.com/enc.jpg"}],
        "media_content": [{"url": "https://example.com/mc.gif"}],
    }
    url, _ = detect_media(entry)
    assert url == "https://example.com/enc.jpg"
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_detector.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Implement src/media/detector.py**

```python
# src/media/detector.py
from html.parser import HTMLParser
from pathlib import PurePosixPath

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".svg"}
_GIF_EXTS = {".gif"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi"}


def detect_type(url: str) -> str | None:
    suffix = PurePosixPath(url.split("?")[0]).suffix.lower()
    if suffix in _GIF_EXTS:
        return "gif"
    if suffix in _IMAGE_EXTS:
        return "image"
    if suffix in _VIDEO_EXTS:
        return "video"
    return None


class _OGParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.og_image: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            d = dict(attrs)
            if d.get("property") == "og:image":
                self.og_image = d.get("content")


def _extract_og_image(html: str) -> str | None:
    parser = _OGParser()
    parser.feed(html)
    return parser.og_image


def detect_media(entry: dict) -> tuple[str, str] | None:
    for enc in entry.get("enclosures", []):
        url = enc.get("url", "")
        t = detect_type(url)
        if url and t:
            return url, t

    for mc in entry.get("media_content", []):
        url = mc.get("url", "")
        t = detect_type(url)
        if url and t:
            return url, t

    for mt in entry.get("media_thumbnail", []):
        url = mt.get("url", "")
        if url:
            return url, detect_type(url) or "image"

    summary = entry.get("summary", "") or ""
    og_url = _extract_og_image(summary)
    if og_url:
        return og_url, detect_type(og_url) or "image"

    return None
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_detector.py -v
```

Expected: `13 passed`

- [ ] **Step 5: Commit**

```bash
git add src/media/detector.py tests/test_detector.py
git commit -m "feat: media type detector (enclosure/media:content/og:image priority chain)"
```

---

### Task 8: Feed fetcher

**Files:**
- Create: `src/feeds/fetcher.py`
- Create: `tests/test_fetcher.py`
- Modify: `tests/conftest.py` (add mock_http fixture)

- [ ] **Step 1: Add mock_http fixture to conftest.py**

Append to `tests/conftest.py`:

```python
import respx

@pytest.fixture
def mock_http() -> respx.MockRouter:
    with respx.MockRouter() as router:
        yield router
```

Add `import respx` at the top of conftest.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_fetcher.py
import httpx
import pytest
import respx

from src.feeds.fetcher import _feed_id, _item_id, fetch_feed

_RSS = """\
<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Image Item</title>
      <guid>guid-img-1</guid>
      <enclosure url="https://example.com/photo.jpg" type="image/jpeg" length="0"/>
    </item>
    <item>
      <title>Text Only</title>
      <guid>guid-text-1</guid>
      <description>no media</description>
    </item>
    <item>
      <title>GIF Item</title>
      <guid>guid-gif-1</guid>
      <enclosure url="https://example.com/anim.gif" type="image/gif" length="0"/>
    </item>
  </channel>
</rss>"""


async def test_fetch_feed_returns_only_media_items(mock_http: respx.MockRouter) -> None:
    mock_http.get("https://example.com/feed.xml").mock(
        return_value=httpx.Response(200, text=_RSS)
    )
    async with httpx.AsyncClient() as client:
        items = await fetch_feed("https://example.com/feed.xml", client)
    assert len(items) == 2
    urls = [i["media_url"] for i in items]
    assert "https://example.com/photo.jpg" in urls
    assert "https://example.com/anim.gif" in urls


async def test_fetch_feed_item_has_correct_fields(mock_http: respx.MockRouter) -> None:
    mock_http.get("https://example.com/feed.xml").mock(
        return_value=httpx.Response(200, text=_RSS)
    )
    async with httpx.AsyncClient() as client:
        items = await fetch_feed("https://example.com/feed.xml", client)
    img = next(i for i in items if i["media_type"] == "image")
    assert img["media_url"] == "https://example.com/photo.jpg"
    assert img["feed_id"] == _feed_id("https://example.com/feed.xml")
    assert img["guid"] == "guid-img-1"
    assert "id" in img


async def test_fetch_feed_same_guid_produces_same_id(mock_http: respx.MockRouter) -> None:
    mock_http.get("https://example.com/feed.xml").mock(
        return_value=httpx.Response(200, text=_RSS)
    )
    async with httpx.AsyncClient() as client:
        items1 = await fetch_feed("https://example.com/feed.xml", client)
    mock_http.get("https://example.com/feed.xml").mock(
        return_value=httpx.Response(200, text=_RSS)
    )
    async with httpx.AsyncClient() as client:
        items2 = await fetch_feed("https://example.com/feed.xml", client)
    assert items1[0]["id"] == items2[0]["id"]
```

- [ ] **Step 3: Run tests — verify they fail**

```bash
uv run pytest tests/test_fetcher.py -v
```

Expected: `ImportError`

- [ ] **Step 4: Implement src/feeds/fetcher.py**

```python
# src/feeds/fetcher.py
import hashlib

import feedparser
import httpx

from src.media.detector import detect_media


def _feed_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _item_id(feed_id: str, guid: str) -> str:
    return hashlib.sha256((feed_id + guid).encode()).hexdigest()


async def fetch_feed(url: str, client: httpx.AsyncClient) -> list[dict]:
    response = await client.get(url, follow_redirects=True, timeout=30)
    feed = feedparser.parse(response.text)
    feed_id = _feed_id(url)
    items = []
    for entry in feed.entries:
        result = detect_media(entry)
        if result is None:
            continue
        media_url, media_type = result
        guid = entry.get("id") or entry.get("link") or media_url
        items.append({
            "id": _item_id(feed_id, guid),
            "feed_id": feed_id,
            "guid": guid,
            "title": entry.get("title"),
            "media_url": media_url,
            "media_type": media_type,
            "pub_date": entry.get("published") or entry.get("updated"),
        })
    return items
```

- [ ] **Step 5: Run tests — verify they pass**

```bash
uv run pytest tests/test_fetcher.py -v
```

Expected: `3 passed`

- [ ] **Step 6: Commit**

```bash
git add src/feeds/fetcher.py tests/test_fetcher.py tests/conftest.py
git commit -m "feat: feed fetcher — extracts media items from RSS via httpx + feedparser"
```

---

### Task 9: Feed sync

**Files:**
- Create: `src/feeds/sync.py`
- Create: `tests/test_sync.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_sync.py
import aiosqlite
import httpx
import pytest
import respx

from src.feeds.fetcher import _feed_id
from src.feeds.sync import opml_sync, refresh_all_feeds

_OPML = """\
<?xml version="1.0"?>
<opml version="2.0"><head/><body>
  <outline type="rss" text="Feed" xmlUrl="https://example.com/feed.xml"/>
</body></opml>"""

_RSS = """\
<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
  <item>
    <guid>g1</guid>
    <enclosure url="https://example.com/img.jpg" type="image/jpeg" length="0"/>
  </item>
</channel></rss>"""


async def test_opml_sync_inserts_new_feeds(
    db: aiosqlite.Connection, tmp_path: pytest.fixture
) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_OPML)
    async with httpx.AsyncClient() as client:
        await opml_sync(db, str(f), client)
    async with db.execute("SELECT COUNT(*) FROM feeds") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_opml_sync_is_idempotent(
    db: aiosqlite.Connection, tmp_path: pytest.fixture
) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_OPML)
    async with httpx.AsyncClient() as client:
        await opml_sync(db, str(f), client)
        await opml_sync(db, str(f), client)
    async with db.execute("SELECT COUNT(*) FROM feeds") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_opml_sync_removes_deleted_feeds(
    db: aiosqlite.Connection, tmp_path: pytest.fixture
) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_OPML)
    async with httpx.AsyncClient() as client:
        await opml_sync(db, str(f), client)
    f.write_text(
        '<?xml version="1.0"?><opml version="2.0"><head/><body/></opml>'
    )
    async with httpx.AsyncClient() as client:
        await opml_sync(db, str(f), client)
    async with db.execute("SELECT COUNT(*) FROM feeds") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_refresh_all_feeds_inserts_items(
    db: aiosqlite.Connection, tmp_path: pytest.fixture
) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_OPML)
    with respx.mock:
        respx.get("https://example.com/feed.xml").mock(
            return_value=httpx.Response(200, text=_RSS)
        )
        async with httpx.AsyncClient() as client:
            await opml_sync(db, str(f), client)
            await refresh_all_feeds(db, client)
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_refresh_all_feeds_deduplicates(
    db: aiosqlite.Connection, tmp_path: pytest.fixture
) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_OPML)
    with respx.mock:
        respx.get("https://example.com/feed.xml").mock(
            return_value=httpx.Response(200, text=_RSS)
        )
        async with httpx.AsyncClient() as client:
            await opml_sync(db, str(f), client)
            await refresh_all_feeds(db, client)
        respx.get("https://example.com/feed.xml").mock(
            return_value=httpx.Response(200, text=_RSS)
        )
        async with httpx.AsyncClient() as client:
            await refresh_all_feeds(db, client)
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        assert (await cur.fetchone())[0] == 1
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_sync.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Implement src/feeds/sync.py**

```python
# src/feeds/sync.py
import aiosqlite
import httpx

from src.feeds.fetcher import _feed_id, fetch_feed
from src.feeds.opml import parse_opml


async def opml_sync(
    db: aiosqlite.Connection, opml_path: str, client: httpx.AsyncClient
) -> None:
    feeds = parse_opml(opml_path)
    feed_ids = []
    for feed in feeds:
        fid = _feed_id(feed["url"])
        feed_ids.append(fid)
        await db.execute(
            "INSERT OR IGNORE INTO feeds (id, url, title) VALUES (?, ?, ?)",
            (fid, feed["url"], feed["title"]),
        )
    if feed_ids:
        placeholders = ",".join("?" * len(feed_ids))
        await db.execute(
            f"DELETE FROM feeds WHERE id NOT IN ({placeholders})", feed_ids
        )
    else:
        await db.execute("DELETE FROM feeds")
    await db.commit()


async def _refresh_feed(
    db: aiosqlite.Connection,
    feed_id: str,
    url: str,
    client: httpx.AsyncClient,
) -> None:
    items = await fetch_feed(url, client)
    for item in items:
        await db.execute(
            """INSERT OR IGNORE INTO items
               (id, feed_id, guid, title, media_url, media_type, pub_date)
               VALUES (:id, :feed_id, :guid, :title, :media_url, :media_type, :pub_date)""",
            item,
        )
    await db.execute(
        "UPDATE feeds SET last_fetched_at = datetime('now') WHERE id = ?",
        (feed_id,),
    )
    await db.commit()


async def refresh_all_feeds(
    db: aiosqlite.Connection, client: httpx.AsyncClient
) -> None:
    async with db.execute("SELECT id, url FROM feeds") as cur:
        feeds = await cur.fetchall()
    for feed in feeds:
        await _refresh_feed(db, feed["id"], feed["url"], client)
```

- [ ] **Step 4: Run all phase-2 tests**

```bash
uv run pytest tests/test_opml.py tests/test_detector.py tests/test_fetcher.py tests/test_sync.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/feeds/sync.py tests/test_sync.py
git commit -m "feat: feed sync — opml_sync and refresh_all_feeds with deduplication"
```

---

### Task 10: Scheduler

**Files:**
- Create: `src/scheduler.py`

Note: APScheduler internals are not unit-tested. Scheduler correctness is verified via the Docker smoke test in Phase 6. The module is covered by import in `src/main.py` (Phase 4).

- [ ] **Step 1: Create src/scheduler.py**

```python
# src/scheduler.py
import logging

import aiosqlite
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.config import settings
from src.feeds.sync import opml_sync, refresh_all_feeds

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("HTTP client not initialised — call start_scheduler first")
    return _client


async def start_scheduler(db: aiosqlite.Connection) -> None:
    global _scheduler, _client
    _client = httpx.AsyncClient()
    _scheduler = AsyncIOScheduler()

    _scheduler.add_job(
        opml_sync,
        "interval",
        seconds=settings.opml_sync_interval,
        args=[db, settings.opml_path, _client],
        id="opml_sync",
    )
    _scheduler.add_job(
        refresh_all_feeds,
        "interval",
        seconds=settings.feed_refresh_interval,
        args=[db, _client],
        id="refresh_feeds",
    )
    _scheduler.start()
    try:
        await opml_sync(db, settings.opml_path, _client)
    except Exception:
        logger.warning("Initial OPML sync failed (file may not exist yet)")


async def stop_scheduler() -> None:
    global _scheduler, _client
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
    if _client:
        await _client.aclose()
        _client = None
```

- [ ] **Step 2: Commit**

```bash
git add src/scheduler.py
git commit -m "feat: APScheduler setup — opml_sync and refresh_all_feeds jobs"
```

---

## Phase 3 — Media Layer

### Task 11: Media cache

**Files:**
- Create: `src/media/cache.py`
- Create: `tests/test_cache.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cache.py
import time
from pathlib import Path

import pytest

from src.media import cache as cache_mod


async def test_write_and_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    await cache_mod.cache_write("https://example.com/img.jpg", b"bytes")
    path = cache_mod.cache_read("https://example.com/img.jpg")
    assert path is not None
    assert path.read_bytes() == b"bytes"


async def test_read_miss_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    assert cache_mod.cache_read("https://example.com/missing.jpg") is None


async def test_evict_by_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(cache_mod.settings, "cache_max_items", 2)
    monkeypatch.setattr(cache_mod.settings, "cache_max_age_hours", 9999)
    for i in range(3):
        (tmp_path / f"file{i}").write_bytes(b"x")
        time.sleep(0.01)
    await cache_mod.evict()
    assert len(list(tmp_path.iterdir())) == 2


async def test_evict_by_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(cache_mod.settings, "cache_max_items", 9999)
    monkeypatch.setattr(cache_mod.settings, "cache_max_age_hours", 0)
    (tmp_path / "stale").write_bytes(b"x")
    await cache_mod.evict()
    assert len(list(tmp_path.iterdir())) == 0


async def test_evict_nonexistent_dir_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cache_mod.settings, "cache_dir", "/nonexistent/cache")
    await cache_mod.evict()  # must not raise
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_cache.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Implement src/media/cache.py**

```python
# src/media/cache.py
import asyncio
import hashlib
import time
from pathlib import Path

from src.config import settings


def _cache_path(url: str) -> Path:
    return Path(settings.cache_dir) / hashlib.sha256(url.encode()).hexdigest()


async def cache_write(url: str, data: bytes) -> Path:
    path = _cache_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, data)
    return path


def cache_read(url: str) -> Path | None:
    path = _cache_path(url)
    return path if path.exists() else None


async def evict() -> None:
    cache_dir = Path(settings.cache_dir)
    if not cache_dir.exists():
        return
    now = time.time()
    max_age_secs = settings.cache_max_age_hours * 3600

    files = sorted(cache_dir.iterdir(), key=lambda p: p.stat().st_mtime)
    surviving: list[Path] = []
    for f in files:
        if now - f.stat().st_mtime > max_age_secs:
            f.unlink(missing_ok=True)
        else:
            surviving.append(f)

    while len(surviving) > settings.cache_max_items:
        surviving.pop(0).unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_cache.py -v
```

Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add src/media/cache.py tests/test_cache.py
git commit -m "feat: media cache — write/read by sha256(url), evict by age and count"
```

---

### Task 12: Prefetch

**Files:**
- Create: `src/media/prefetch.py`

Note: `prefetch_ahead` is tested via `POST /api/prefetch/hint` in Task 16.

- [ ] **Step 1: Create src/media/prefetch.py**

```python
# src/media/prefetch.py
import asyncio
import logging

import aiosqlite
import httpx

from src.config import settings
from src.media.cache import _cache_path, cache_read

logger = logging.getLogger(__name__)


async def _warm(url: str, client: httpx.AsyncClient) -> None:
    if cache_read(url) is not None:
        return
    try:
        response = await client.get(url, follow_redirects=True, timeout=30)
        data = await response.aread()
        path = _cache_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
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
```

- [ ] **Step 2: Commit**

```bash
git add src/media/prefetch.py
git commit -m "feat: prefetch — fire-and-forget cache warming for next N items"
```

---

## Phase 4 — API Endpoints + App Wiring

### Task 13: FastAPI app + test client fixture

**Files:**
- Create: `src/main.py`
- Modify: `tests/conftest.py` (add client fixture)

- [ ] **Step 1: Create src/main.py**

```python
# src/main.py
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import aiosqlite
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from src.api import feeds, items, media
from src.config import settings
from src.db.connection import open_db
from src.db.migrations import run_migrations
from src.db.schema import create_schema
from src.scheduler import start_scheduler, stop_scheduler

_static_dir = Path(__file__).parent / "static"
_index_path = _static_dir / "index.html"


def _injected_html() -> str:
    if not _index_path.exists():
        return ""
    style = (
        f"<style>:root{{"
        f"--slideshow-transition-ms:{settings.slideshow_transition_ms}ms;"
        f"--image-display-delay-ms:{settings.image_display_delay_ms}ms"
        f"}}</style>"
    )
    return _index_path.read_text().replace("<!-- SLIDESHOW_TRANSITION -->", style)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    db = await open_db(settings.db_path)
    await create_schema(db)
    await run_migrations(db)
    app.state.db = db
    await start_scheduler(db)
    yield
    await stop_scheduler()
    await db.close()


app = FastAPI(lifespan=lifespan)
app.include_router(feeds.router, prefix="/api")
app.include_router(items.router, prefix="/api")
app.include_router(media.router, prefix="/api")

if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _injected_html()
```

- [ ] **Step 2: Add client fixture to tests/conftest.py**

Append to `tests/conftest.py`:

```python
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import feeds as feeds_router
from src.api import items as items_router
from src.api import media as media_router
from src.db.connection import get_db


@pytest.fixture
async def client(db: aiosqlite.Connection) -> AsyncClient:
    test_app = FastAPI()
    test_app.include_router(feeds_router.router, prefix="/api")
    test_app.include_router(items_router.router, prefix="/api")
    test_app.include_router(media_router.router, prefix="/api")

    async def _override_db() -> AsyncGenerator[aiosqlite.Connection, None]:
        yield db

    test_app.dependency_overrides[get_db] = _override_db

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as c:
        yield c
```

Add `from collections.abc import AsyncGenerator` and the httpx imports at the top of conftest.

- [ ] **Step 3: Commit**

```bash
git add src/main.py tests/conftest.py src/api/__init__.py
git commit -m "feat: FastAPI app with lifespan and test client fixture"
```

---

### Task 14: /api/feeds endpoint

**Files:**
- Create: `src/api/feeds.py`
- Create: `tests/test_api.py` (feeds tests)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_api.py
import aiosqlite
import pytest
from httpx import AsyncClient


async def test_feeds_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/feeds")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_feeds_returns_feed_with_counts(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    await db.execute(
        "INSERT INTO feeds (id, url, title) VALUES ('f1', 'http://x.com', 'X')"
    )
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
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_api.py::test_feeds_empty -v
```

Expected: `ImportError` or router not found

- [ ] **Step 3: Implement src/api/feeds.py**

```python
# src/api/feeds.py
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends

from src.db.connection import get_db

router = APIRouter()


@router.get("/feeds")
async def list_feeds(db: aiosqlite.Connection = Depends(get_db)) -> list[dict[str, Any]]:
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
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_api.py::test_feeds_empty tests/test_api.py::test_feeds_returns_feed_with_counts -v
```

Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add src/api/feeds.py tests/test_api.py
git commit -m "feat: GET /api/feeds with item and unseen counts"
```

---

### Task 15: /api/items and /api/items/{id}/seen

**Files:**
- Create: `src/api/items.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Add failing tests to tests/test_api.py**

```python
async def test_items_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/items")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_items_unseen_filter(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    await db.execute(
        "INSERT INTO feeds (id, url, title) VALUES ('f1', 'http://x.com', 'X')"
    )
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type)"
        " VALUES ('i1', 'f1', 'g1', 'http://img.jpg', 'image')"
    )
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type, seen_at)"
        " VALUES ('i2', 'f1', 'g2', 'http://img2.jpg', 'image', datetime('now'))"
    )
    await db.commit()
    resp = await client.get("/api/items?unseen=true")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "i1"


async def test_items_feed_id_filter(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    await db.execute(
        "INSERT INTO feeds (id, url, title) VALUES ('f1', 'http://a.com', 'A')"
    )
    await db.execute(
        "INSERT INTO feeds (id, url, title) VALUES ('f2', 'http://b.com', 'B')"
    )
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type)"
        " VALUES ('i1', 'f1', 'g1', 'http://a.jpg', 'image')"
    )
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type)"
        " VALUES ('i2', 'f2', 'g2', 'http://b.jpg', 'image')"
    )
    await db.commit()
    resp = await client.get("/api/items?feed_id=f1")
    data = resp.json()
    assert len(data) == 1
    assert data[0]["feed_id"] == "f1"


async def test_mark_seen(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT INTO feeds (id, url, title) VALUES ('f1', 'http://x.com', 'X')"
    )
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type)"
        " VALUES ('i1', 'f1', 'g1', 'http://img.jpg', 'image')"
    )
    await db.commit()
    resp = await client.post("/api/items/i1/seen")
    assert resp.status_code == 200
    assert resp.json()["seen_at"] is not None


async def test_mark_seen_idempotent(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    await db.execute(
        "INSERT INTO feeds (id, url, title) VALUES ('f1', 'http://x.com', 'X')"
    )
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type)"
        " VALUES ('i1', 'f1', 'g1', 'http://img.jpg', 'image')"
    )
    await db.commit()
    r1 = await client.post("/api/items/i1/seen")
    r2 = await client.post("/api/items/i1/seen")
    assert r1.json()["seen_at"] == r2.json()["seen_at"]
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_api.py::test_items_empty -v
```

Expected: router 404

- [ ] **Step 3: Implement src/api/items.py**

```python
# src/api/items.py
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends

from src.db.connection import get_db

router = APIRouter()


@router.get("/items")
async def list_items(
    unseen: bool = False,
    feed_id: str | None = None,
    page: int = 0,
    size: int = 50,
    db: aiosqlite.Connection = Depends(get_db),
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if unseen:
        conditions.append("seen_at IS NULL")
    if feed_id:
        conditions.append("feed_id = ?")
        params.append(feed_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([size, page * size])
    async with db.execute(
        f"SELECT * FROM items {where} ORDER BY pub_date DESC LIMIT ? OFFSET ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


@router.post("/items/{item_id}/seen")
async def mark_seen(
    item_id: str, db: aiosqlite.Connection = Depends(get_db)
) -> dict[str, Any]:
    await db.execute(
        "UPDATE items SET seen_at = datetime('now') WHERE id = ? AND seen_at IS NULL",
        (item_id,),
    )
    await db.commit()
    async with db.execute("SELECT seen_at FROM items WHERE id = ?", (item_id,)) as cur:
        row = await cur.fetchone()
    return {"seen_at": row["seen_at"] if row else None}
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_api.py -k "items or seen" -v
```

Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/api/items.py tests/test_api.py
git commit -m "feat: GET /api/items with filters and POST /api/items/{id}/seen"
```

---

### Task 16: /api/media/proxy, /api/prefetch/hint, /api/status

**Files:**
- Create: `src/api/media.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Add failing tests to tests/test_api.py**

```python
import httpx
import respx


async def test_status(client: AsyncClient) -> None:
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["feeds"] == 0
    assert data["items_total"] == 0
    assert data["items_unseen"] == 0
    assert "cache_size_mb" in data


async def test_proxy_cache_miss_fetches_and_returns(
    client: AsyncClient, tmp_path: pytest.fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.media.cache as cache_mod
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    with respx.mock:
        respx.get("https://example.com/img.jpg").mock(
            return_value=httpx.Response(
                200, content=b"img-bytes", headers={"content-type": "image/jpeg"}
            )
        )
        resp = await client.get(
            "/api/media/proxy?url=https%3A%2F%2Fexample.com%2Fimg.jpg"
        )
    assert resp.status_code == 200
    assert resp.content == b"img-bytes"


async def test_proxy_cache_hit_serves_from_disk(
    client: AsyncClient, tmp_path: pytest.fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.media.cache as cache_mod
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    await cache_mod.cache_write("https://example.com/cached.jpg", b"cached-bytes")
    resp = await client.get(
        "/api/media/proxy?url=https%3A%2F%2Fexample.com%2Fcached.jpg"
    )
    assert resp.status_code == 200
    assert resp.content == b"cached-bytes"


async def test_prefetch_hint_returns_ok(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    await db.execute(
        "INSERT INTO feeds (id, url, title) VALUES ('f1', 'http://x.com', 'X')"
    )
    await db.execute(
        "INSERT INTO items (id, feed_id, guid, media_url, media_type, pub_date)"
        " VALUES ('i1', 'f1', 'g1', 'http://img.jpg', 'image', '2024-01-01')"
    )
    await db.commit()
    resp = await client.post("/api/prefetch/hint", json={"item_id": "i1"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_api.py::test_status -v
```

Expected: 404 (router not registered)

- [ ] **Step 3: Implement src/api/media.py**

```python
# src/api/media.py
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.config import settings
from src.db.connection import get_db
from src.media.cache import _cache_path, cache_read, cache_write
from src.media.prefetch import prefetch_ahead

router = APIRouter()


@router.get("/media/proxy")
async def proxy_media(
    url: str, db: aiosqlite.Connection = Depends(get_db)
) -> StreamingResponse:
    cached = cache_read(url)
    if cached:
        data = cached.read_bytes()

        async def _cached_stream():
            yield data

        return StreamingResponse(_cached_stream(), media_type="application/octet-stream")

    async with httpx.AsyncClient() as client:
        response = await client.get(url, follow_redirects=True, timeout=30)
    content_type = response.headers.get("content-type", "application/octet-stream")
    data = response.content
    await cache_write(url, data)

    if "gif" in content_type:
        await db.execute(
            "UPDATE items SET media_type='gif' WHERE media_url=? AND media_type='image'",
            (url,),
        )
        await db.commit()

    async def _stream():
        yield data

    return StreamingResponse(_stream(), media_type=content_type)


class _PrefetchBody(BaseModel):
    item_id: str


@router.post("/prefetch/hint")
async def prefetch_hint(
    body: _PrefetchBody, db: aiosqlite.Connection = Depends(get_db)
) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        await prefetch_ahead(body.item_id, db, client)
    return {"ok": True}


@router.get("/status")
async def status(db: aiosqlite.Connection = Depends(get_db)) -> dict[str, Any]:
    async with db.execute("SELECT COUNT(*) FROM feeds") as cur:
        feeds_count: int = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        items_total: int = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM items WHERE seen_at IS NULL") as cur:
        items_unseen: int = (await cur.fetchone())[0]

    cache_dir = Path(settings.cache_dir)
    cache_size_mb = 0.0
    if cache_dir.exists():
        cache_size_mb = (
            sum(f.stat().st_size for f in cache_dir.iterdir() if f.is_file())
            / (1024 * 1024)
        )

    return {
        "feeds": feeds_count,
        "items_total": items_total,
        "items_unseen": items_unseen,
        "cache_size_mb": round(cache_size_mb, 2),
    }
```

- [ ] **Step 4: Run all API tests + full coverage check**

```bash
uv run pytest tests/ -v
uv run ruff check .
```

Expected: all pass, ruff clean, ≥ 90 % coverage.

- [ ] **Step 5: Commit**

```bash
git add src/api/media.py tests/test_api.py
git commit -m "feat: GET /api/media/proxy, POST /api/prefetch/hint, GET /api/status"
```

---

## Phase 5 — Frontend

### Task 17: HTML shell + CSS

**Files:**
- Create: `src/static/index.html`
- Create: `src/static/style.css`

- [ ] **Step 1: Create src/static/index.html**

```html
<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Media RSS Reader</title>
  <link rel="stylesheet" href="/static/style.css"/>
  <!-- SLIDESHOW_TRANSITION -->
</head>
<body>
  <header id="toolbar">
    <span id="mode-label">scroll</span>
    <button id="btn-mode" title="Toggle slideshow (s)">slideshow</button>
    <button id="btn-auto" title="Toggle auto-scroll (a)">auto</button>
    <button id="btn-mute" title="Toggle mute (m)">mute</button>
    <button id="btn-theme" title="Toggle theme (d)">theme</button>
    <span id="status-label"></span>
  </header>

  <main id="scroll-view">
    <div id="item-list"></div>
  </main>

  <div id="slideshow-view" hidden>
    <div class="ss-layer" id="layer-a"></div>
    <div class="ss-layer" id="layer-b"></div>
  </div>

  <script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Create src/static/style.css**

```css
:root {
  --bg: #111;
  --fg: #eee;
  --toolbar-bg: #222;
  --slideshow-transition-ms: 400ms;
  --image-display-delay-ms: 5000ms;
}

[data-theme="light"] {
  --bg: #f8f8f8;
  --fg: #111;
  --toolbar-bg: #e0e0e0;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html, body {
  height: 100%;
  background: var(--bg);
  color: var(--fg);
  font-family: system-ui, sans-serif;
}

#toolbar {
  position: fixed;
  top: 0; left: 0; right: 0;
  height: 40px;
  background: var(--toolbar-bg);
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 12px;
  z-index: 100;
}

#toolbar button {
  background: transparent;
  border: 1px solid var(--fg);
  color: var(--fg);
  padding: 2px 8px;
  cursor: pointer;
  border-radius: 3px;
  font-size: 0.8rem;
}

#scroll-view {
  margin-top: 40px;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.item {
  width: 100%;
  max-width: 900px;
  margin: 16px auto;
  display: flex;
  justify-content: center;
}

.item img, .item video {
  max-width: 100%;
  max-height: 90vh;
  object-fit: contain;
  display: block;
}

/* Slideshow */
#slideshow-view {
  position: fixed;
  inset: 40px 0 0 0;
  background: var(--bg);
}

.ss-layer {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0;
  transition: opacity var(--ss-transition) ease;
  pointer-events: none;
}

.ss-layer.active {
  opacity: 1;
  pointer-events: auto;
}

.ss-layer img, .ss-layer video {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}
```

- [ ] **Step 3: Start dev server and verify HTML + CSS load**

```bash
uv run uvicorn src.main:app --reload --port 8080
```

Open `http://localhost:8080` in a browser. Expected: dark toolbar visible, no JS errors in console.

- [ ] **Step 4: Commit**

```bash
git add src/static/index.html src/static/style.css
git commit -m "feat: frontend HTML shell and CSS with dark/light theme variables"
```

---

### Task 18: Frontend JavaScript

**Files:**
- Create: `src/static/app.js`

- [ ] **Step 1: Create src/static/app.js**

```javascript
// src/static/app.js

// ── State ──────────────────────────────────────────────────────────────────
let items = [];
let page = 0;
let loading = false;
let currentIndex = 0;
let mode = localStorage.getItem('mode') || 'scroll';  // 'scroll' | 'slideshow'
let autoOn = false;
let autoTimer = null;
let muted = true;
let activeLayer = 'a';

// ── DOM refs ───────────────────────────────────────────────────────────────
const scrollView   = document.getElementById('scroll-view');
const slideshowView = document.getElementById('slideshow-view');
const itemList     = document.getElementById('item-list');
const layers       = { a: document.getElementById('layer-a'), b: document.getElementById('layer-b') };
const btnMode      = document.getElementById('btn-mode');
const btnAuto      = document.getElementById('btn-auto');
const btnMute      = document.getElementById('btn-mute');
const btnTheme     = document.getElementById('btn-theme');
const statusLabel  = document.getElementById('status-label');

// ── Theme init (before first render to avoid flash) ────────────────────────
document.documentElement.dataset.theme = localStorage.getItem('theme') || 'dark';

// ── API ────────────────────────────────────────────────────────────────────
async function fetchItems(p) {
  const r = await fetch(`/api/items?unseen=true&page=${p}&size=50`);
  return r.json();
}

async function postSeen(id) {
  await fetch(`/api/items/${id}/seen`, { method: 'POST' });
}

async function postPrefetch(id) {
  await fetch('/api/prefetch/hint', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ item_id: id }),
  });
}

// ── Item rendering ─────────────────────────────────────────────────────────
function mediaEl(item) {
  const proxyUrl = `/api/media/proxy?url=${encodeURIComponent(item.media_url)}`;
  if (item.media_type === 'video') {
    const v = document.createElement('video');
    v.src = proxyUrl;
    v.muted = muted;
    v.controls = true;
    v.addEventListener('ended', () => { if (autoOn) advanceItem(); });
    return v;
  }
  const img = document.createElement('img');
  img.src = proxyUrl;
  img.loading = 'lazy';
  return img;
}

function renderScrollItem(item, index) {
  const div = document.createElement('div');
  div.className = 'item';
  div.dataset.id = item.id;
  div.dataset.index = String(index);
  div.appendChild(mediaEl(item));
  itemList.appendChild(div);
}

function renderSlideshowItem(item) {
  const next = activeLayer === 'a' ? 'b' : 'a';
  layers[next].innerHTML = '';
  layers[next].appendChild(mediaEl(item));
  layers[activeLayer].classList.remove('active');
  layers[next].classList.add('active');
  activeLayer = next;
}

// ── Observer (seen + pagination + prefetch) ────────────────────────────────
const observer = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting || e.intersectionRatio < 0.8) continue;
    const el = e.target;
    observer.unobserve(el);
    const id = el.dataset.id;
    const idx = parseInt(el.dataset.index, 10);
    postSeen(id);
    postPrefetch(id);
    if (items.length - idx <= 10) loadMore();
  }
}, { threshold: 0.8 });

// ── Load more ──────────────────────────────────────────────────────────────
async function loadMore() {
  if (loading) return;
  loading = true;
  const newItems = await fetchItems(page++);
  const startIndex = items.length;
  items.push(...newItems);
  newItems.forEach((item, i) => {
    renderScrollItem(item, startIndex + i);
  });
  document.querySelectorAll('.item:not([data-observed])').forEach(el => {
    el.dataset.observed = '1';
    observer.observe(el);
  });
  statusLabel.textContent = `${items.length} items`;
  loading = false;
}

// ── Navigation ─────────────────────────────────────────────────────────────
function advanceItem() {
  if (currentIndex >= items.length - 1) return;
  currentIndex++;
  if (mode === 'slideshow') {
    renderSlideshowItem(items[currentIndex]);
  } else {
    document.querySelectorAll('.item')[currentIndex]?.scrollIntoView({ behavior: 'smooth' });
  }
  stopAuto();
  startAuto();
}

function retreatItem() {
  if (currentIndex <= 0) return;
  currentIndex--;
  if (mode === 'slideshow') {
    renderSlideshowItem(items[currentIndex]);
  } else {
    document.querySelectorAll('.item')[currentIndex]?.scrollIntoView({ behavior: 'smooth' });
  }
}

// ── Auto-scroll ────────────────────────────────────────────────────────────
function startAuto() {
  if (!autoOn) return;
  stopAuto();
  const item = items[currentIndex];
  if (!item || item.media_type === 'video') return;  // video advances on 'ended'
  const delayRaw = getComputedStyle(document.documentElement)
    .getPropertyValue('--image-display-delay-ms').trim() || '5000ms';
  const delay = parseInt(delayRaw, 10);
  autoTimer = setTimeout(advanceItem, delay);
}

function stopAuto() {
  if (autoTimer) { clearTimeout(autoTimer); autoTimer = null; }
}

function toggleAuto() {
  autoOn = !autoOn;
  btnAuto.classList.toggle('active', autoOn);
  autoOn ? startAuto() : stopAuto();
}

// ── Mode toggle ────────────────────────────────────────────────────────────
function setMode(m) {
  mode = m;
  localStorage.setItem('mode', m);
  if (m === 'slideshow') {
    scrollView.hidden = true;
    slideshowView.hidden = false;
    if (items[currentIndex]) renderSlideshowItem(items[currentIndex]);
  } else {
    slideshowView.hidden = true;
    scrollView.hidden = false;
  }
  btnMode.textContent = m === 'slideshow' ? 'scroll' : 'slideshow';
}

// ── Mute toggle ────────────────────────────────────────────────────────────
function toggleMute() {
  muted = !muted;
  document.querySelectorAll('video').forEach(v => { v.muted = muted; });
}

// ── Theme toggle ───────────────────────────────────────────────────────────
function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('theme', next);
}

// ── Keyboard ───────────────────────────────────────────────────────────────
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  switch (e.key) {
    case 'j': case 'ArrowDown':  e.preventDefault(); advanceItem(); break;
    case 'k': case 'ArrowUp':    e.preventDefault(); retreatItem(); break;
    case 'a': toggleAuto();  break;
    case 's': setMode(mode === 'slideshow' ? 'scroll' : 'slideshow'); break;
    case 'm': toggleMute();  break;
    case 'd': toggleTheme(); break;
  }
});

// ── Button wiring ──────────────────────────────────────────────────────────
btnMode.addEventListener('click', () => setMode(mode === 'slideshow' ? 'scroll' : 'slideshow'));
btnAuto.addEventListener('click', toggleAuto);
btnMute.addEventListener('click', toggleMute);
btnTheme.addEventListener('click', toggleTheme);

// ── Boot ───────────────────────────────────────────────────────────────────
setMode(mode);
loadMore();
```

- [ ] **Step 2: Verify in browser**

With the dev server still running (`uv run uvicorn src.main:app --reload --port 8080`):

1. Open `http://localhost:8080`. Items should load (if feeds are configured) or show empty state.
2. Press `d` — theme should toggle dark ↔ light.
3. Press `s` — slideshow view should appear.
4. Press `s` again — scroll view should return.
5. Press `j` / `k` — navigate items.
6. Press `a` — auto-scroll should activate.
7. Check browser console for JS errors — expect none.

- [ ] **Step 3: Commit**

```bash
git add src/static/app.js
git commit -m "feat: frontend JS — scroll/slideshow modes, key bindings, IntersectionObserver, pagination"
```

---

## Phase 6 — Docker + Smoke Test

### Task 19: Dockerfile, docker-compose, .gitignore, sample OPML

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.gitignore`
- Create: `feeds.opml`

- [ ] **Step 1: Create Dockerfile**

```dockerfile
FROM python:3.14-alpine

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

COPY src/ ./src/

ENV PYTHONPATH=/app

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

- [ ] **Step 2: Create docker-compose.yml**

```yaml
services:
  media-rss:
    build: .
    ports:
      - "8080:8080"
    volumes:
      - ./feeds.opml:/data/feeds.opml:ro
      - reader_data:/data
      - media_cache:/cache
    env_file:
      - .env
    restart: unless-stopped

volumes:
  reader_data:
  media_cache:
```

- [ ] **Step 3: Create .gitignore**

```
__pycache__/
*.py[cod]
.venv/
htmlcov/
.coverage
*.db
.env
```

- [ ] **Step 4: Create feeds.opml (sample)**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Media Feeds</title></head>
  <body>
    <outline type="rss" text="NASA Image of the Day"
             xmlUrl="https://www.nasa.gov/rss/dyn/lg_image_of_the_day.rss"/>
  </body>
</opml>
```

- [ ] **Step 5: Commit**

```bash
git add Dockerfile docker-compose.yml .gitignore feeds.opml
git commit -m "chore: Dockerfile, docker-compose, .gitignore, sample OPML"
```

---

### Task 20: Docker smoke test

- [ ] **Step 1: Create .env for docker-compose**

```bash
cat > .env <<'EOF'
LOG_LEVEL=info
OPML_SYNC_INTERVAL=60
FEED_REFRESH_INTERVAL=120
EOF
```

- [ ] **Step 2: Build and start**

```bash
docker compose up --build -d
```

Expected: build succeeds, container starts.

- [ ] **Step 3: Smoke-test the API**

```bash
sleep 5
curl -sf http://localhost:8080/api/status | python3 -m json.tool
```

Expected output (values may differ):
```json
{
  "feeds": 0,
  "items_total": 0,
  "items_unseen": 0,
  "cache_size_mb": 0.0
}
```

```bash
curl -sf http://localhost:8080/ | head -5
```

Expected: HTML starting with `<!DOCTYPE html>`

- [ ] **Step 4: Tear down**

```bash
docker compose down
```

- [ ] **Step 5: Final lint + test run**

```bash
uv run ruff check .
uv run pytest --tb=short
```

Expected: ruff clean, all tests pass, coverage ≥ 90 %.

- [ ] **Step 6: Commit**

```bash
git add .env
git commit -m "chore: add .env for local docker-compose smoke test"
```

---

## Verification Summary

| Phase | Gate |
|---|---|
| 1 — Scaffold + DB | `uv run pytest tests/test_db.py tests/test_config.py` green |
| 2 — Feed ingestion | `uv run pytest tests/test_opml.py tests/test_detector.py tests/test_fetcher.py tests/test_sync.py` green |
| 3 — Media layer | `uv run pytest tests/test_cache.py` green |
| 4 — API | `uv run pytest tests/test_api.py` green; coverage ≥ 90 % |
| 5 — Frontend | Manual browser smoke: themes, modes, key bindings, no JS errors |
| 6 — Docker | `curl -sf http://localhost:8080/api/status` returns valid JSON |
