# CLAUDE.md — Media RSS Reader

## Project Overview

A self-hosted media RSS reader that fetches feeds from an OPML file and presents images, animations, and videos in a vertical scrolling interface or a slideshow. The backend runs continuously — fetching and storing feed items into SQLite in the background, exactly like a headless RSS client. Designed to run inside Docker and serve a browser-based UI over HTTP.

---

## Goals & Non-Goals

**Goals**
- Display only media items (images, GIFs, videos) from RSS feeds
- Smooth, responsive scrolling — including auto-scroll mode
- Slideshow mode: fullscreen, centered, with CSS fade transitions between items
- Dark/light mode toggle
- Pre-fetch and cache upcoming media to eliminate load stalls
- OPML-driven feed list with periodic sync
- Persistent storage of feed items and seen state across restarts
- Continuous background fetching independent of browser activity
- Configurable entirely via environment variables

**Non-Goals**
- Article/text rendering (only a simple interface with some options and hints)
- User accounts or authentication
- External database service (SQLite only)

The media is at the center of attention.

---

## Stack

| Layer | Choice | Rationale |
|---|---|---|
| Backend language | Python 3.14 | User preference |
| Package manager | `uv` | Fast, lockfile-based, replaces pip + venv |
| Web framework | FastAPI + Uvicorn | Async-native; easy SSE/streaming |
| Database | SQLite via `aiosqlite` | Persistent, file-based, no extra service |
| RSS parsing | `feedparser` | De-facto standard |
| OPML parsing | `listparser` | Lightweight, no deps |
| HTTP client | `httpx` (async) | Async media fetching and feed refresh |
| Media cache | Filesystem (`/cache`) | Docker volume-mounted |
| Background tasks | `asyncio` + `APScheduler` | In-process scheduler for continuous feed refresh and OPML sync |
| Frontend | Vanilla JS + HTML/CSS | No build step; served as static files from FastAPI |
| Linter / formatter | `ruff` | Single tool for lint + format |
| Tests | `pytest` + `pytest-asyncio` | Async-native test runner |
| Coverage | `pytest-cov` | HTML + terminal coverage reports |
| Container | Docker (single image) | `python:3.14-alpine` base |
| Orchestration | `docker-compose.yml` | Wires volumes, env vars, port mapping |

---

## Directory Structure

```
media-rss-reader/
├── CLAUDE.md
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml            # single source of truth: deps, ruff, pytest, coverage
├── uv.lock                   # committed lockfile
│
├── src/
│   ├── main.py               # FastAPI app; lifespan hook starts scheduler + DB init
│   ├── config.py             # Pydantic Settings — all config from env vars
│   ├── scheduler.py          # APScheduler setup; registers all periodic jobs
│   │
│   ├── db/
│   │   ├── connection.py     # aiosqlite connection; get_db() FastAPI dependency
│   │   ├── schema.py         # CREATE TABLE statements; run on startup (idempotent)
│   │   └── migrations.py     # Integer-versioned migrations via PRAGMA user_version
│   │
│   ├── feeds/
│   │   ├── opml.py           # Parse OPML; return list of {url, title} dicts
│   │   ├── fetcher.py        # Fetch + parse one RSS feed; extract media items
│   │   └── sync.py           # Orchestrates OPML sync and per-feed refresh; writes to DB
│   │
│   ├── media/
│   │   ├── detector.py       # Detect media type from enclosure/media:content/og:image
│   │   ├── cache.py          # Filesystem cache: write, read, evict by count+age
│   │   └── prefetch.py       # Async prefetch queue: given current item id, warm next N
│   │
│   ├── api/
│   │   ├── feeds.py          # GET /api/feeds
│   │   ├── items.py          # GET /api/items, POST /api/items/{id}/seen
│   │   └── media.py          # GET /api/media/proxy?url=...
│   │
│   └── static/
│       ├── index.html        # Single-page app shell
│       ├── app.js            # All UI logic (scroll, slideshow, dark mode, key bindings)
│       └── style.css         # Layout, CSS variables for theming, transitions
│
└── tests/
    ├── conftest.py           # shared fixtures: in-memory DB, mock httpx client
    ├── test_opml.py
    ├── test_fetcher.py
    ├── test_sync.py
    ├── test_cache.py
    ├── test_detector.py
    └── test_api.py           # FastAPI TestClient tests for all endpoints
```

---

## pyproject.toml

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
select = [
    "E",    # pycodestyle errors
    "W",    # pycodestyle warnings
    "F",    # pyflakes
    "I",    # isort
    "UP",   # pyupgrade
    "B",    # flake8-bugbear
    "SIM",  # flake8-simplify
    "ANN",  # flake8-annotations (type hints)
    "ASYNC",# flake8-async (await hygiene)
]
ignore = [
    "ANN101",  # missing type for self
    "ANN102",  # missing type for cls
]

[tool.ruff.lint.isort]
known-first-party = ["src"]

[tool.ruff.format]
quote-style = "double"

# ---------------------------------------------------------------------------
# pytest
# ---------------------------------------------------------------------------
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "--cov=src --cov-report=term-missing --cov-report=html:htmlcov --cov-fail-under=90"

# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------
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

---

## Database Schema

SQLite file at `DB_PATH` (default `/data/reader.db`), volume-mounted for persistence.

```sql
CREATE TABLE IF NOT EXISTS feeds (
    id              TEXT PRIMARY KEY,       -- sha256(url)
    url             TEXT NOT NULL UNIQUE,
    title           TEXT,
    last_fetched_at TIMESTAMP,
    created_at      TIMESTAMP DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,           -- sha256(feed_id || guid)
    feed_id     TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid        TEXT NOT NULL,
    title       TEXT,
    media_url   TEXT NOT NULL,
    media_type  TEXT NOT NULL,              -- 'image' | 'gif' | 'video'
    pub_date    TIMESTAMP,
    fetched_at  TIMESTAMP DEFAULT (datetime('now')),
    seen_at     TIMESTAMP,                  -- NULL = unseen
    UNIQUE(feed_id, guid)
);

CREATE INDEX IF NOT EXISTS idx_items_feed_id  ON items(feed_id);
CREATE INDEX IF NOT EXISTS idx_items_pub_date ON items(pub_date DESC);
CREATE INDEX IF NOT EXISTS idx_items_seen_at  ON items(seen_at);
```

**Deduplication**: `INSERT OR IGNORE` on `(feed_id, guid)`.

**Feed removal**: `ON DELETE CASCADE` drops all items when a feed is removed from OPML.

**Schema versioning**: `PRAGMA user_version` is used as a migration counter. `migrations.py` runs on every startup and applies pending migrations in sequence.

---

## Architecture

### Background Fetch Loop (always running)

```
startup
   │
   ├─► db/schema.py            → ensure tables and indexes exist
   ├─► db/migrations.py        → apply pending migrations
   │
   └─► scheduler.py
          │
          ├─► job: opml_sync()              every OPML_SYNC_INTERVAL seconds
          │         ├─ parse OPML file
          │         ├─ INSERT OR IGNORE new feeds
          │         ├─ DELETE removed feeds (cascades to items)
          │         └─ trigger immediate fetch for new feeds
          │
          └─► job: refresh_all_feeds()      every FEED_REFRESH_INTERVAL seconds
                    └─ for each feed in DB:
                         fetch via httpx + feedparser
                         detect media items
                         INSERT OR IGNORE into items
                         UPDATE feeds.last_fetched_at
```

### Data Flow (browser session)

```
Browser
   │
   ├─► GET /api/items?unseen=true&page=0&size=50
   │         └─ SELECT FROM items WHERE seen_at IS NULL ORDER BY pub_date DESC
   │
   ├─► POST /api/items/{id}/seen        (Intersection Observer, threshold 0.8)
   │         └─ UPDATE items SET seen_at = datetime('now') WHERE id = ?
   │
   └─► GET /api/media/proxy?url=...
             ├─ hit  → stream from /cache/<sha256(url)>
             └─ miss → fetch, write to /cache, stream response
```

---

## Configuration (Environment Variables)

| Variable | Default | Description |
|---|---|---|
| `OPML_PATH` | `/data/feeds.opml` | Path to OPML file |
| `DB_PATH` | `/data/reader.db` | Path to SQLite database file |
| `OPML_SYNC_INTERVAL` | `3600` | Seconds between OPML re-reads |
| `FEED_REFRESH_INTERVAL` | `900` | Seconds between feed refresh cycles |
| `CACHE_DIR` | `/cache` | Directory for cached media files |
| `CACHE_MAX_ITEMS` | `500` | Max cached media files |
| `CACHE_MAX_AGE_HOURS` | `48` | Max age of cached files |
| `PREFETCH_AHEAD` | `5` | Items to pre-fetch ahead of current position |
| `IMAGE_DISPLAY_DELAY_MS` | `5000` | Dwell time per image in auto-scroll / slideshow |
| `SLIDESHOW_TRANSITION_MS` | `400` | CSS crossfade duration between slideshow items |
| `PORT` | `8080` | Port exposed by the container |
| `LOG_LEVEL` | `info` | Uvicorn log level |

---

## API Endpoints

```
GET  /                              → serve index.html
GET  /static/*                      → static files

GET  /api/feeds                     → [{id, title, url, item_count, unseen_count, last_fetched_at}]

GET  /api/items                     → paginated media items
                                      ?unseen=true   unseen only (default: false)
                                      ?feed_id=<id>  filter by feed
                                      ?page=0&size=50
                                      → [{id, feed_id, title, media_url, media_type,
                                           pub_date, fetched_at, seen_at}]

POST /api/items/{id}/seen           → {seen_at: iso8601}

GET  /api/media/proxy               → ?url=<encoded> — cache-through proxy

POST /api/prefetch/hint             → {item_id: str} — warm next N items

GET  /api/status                    → {feeds, items_total, items_unseen,
                                        cache_size_mb, last_opml_sync}
```

---

## Frontend Behaviour

### View Modes

The UI has two mutually exclusive view modes. The active mode is stored in `localStorage`.

**Scroll mode** (default)
- Items rendered in a vertical list.
- Scrollable by mouse wheel, `j`/`↓` (next), `k`/`↑` (previous).
- Toggle auto-scroll with `a`.
- Images dwell for `IMAGE_DISPLAY_DELAY_MS` in auto-scroll; videos advance on `ended`.

**Slideshow mode** (toggle: `s`)
- A single item fills the viewport (centered, `object-fit: contain`).
- Advancing to the next item triggers a CSS crossfade: the incoming item fades in over the outgoing one (`opacity` transition, duration `SLIDESHOW_TRANSITION_MS` injected into CSS from a `<meta>` tag rendered by the backend).
- Implementation: two `<div>` layers (A and B) swap `active` class on each advance. No JS animation libraries.
- Auto-advances identically to scroll mode auto-scroll (same `IMAGE_DISPLAY_DELAY_MS` and `ended` event logic).
- `j`/`k` and mouse wheel still work for manual advance.

### Dark / Light Mode (toggle: `d`)

- CSS custom properties define the full palette under `[data-theme="dark"]` and `[data-theme="light"]` selectors on `<html>`.
- Default theme: `dark`.
- Toggle flips `data-theme` and writes the value to `localStorage("theme")`.
- On load, `localStorage("theme")` is read before first render to avoid a flash.

### Key Bindings Summary

| Key | Action |
|---|---|
| `j` / `↓` | Next item |
| `k` / `↑` | Previous item |
| `a` | Toggle auto-scroll |
| `s` | Toggle slideshow mode |
| `m` | Toggle mute |
| `d` | Toggle dark / light mode |

### Seen Tracking

`IntersectionObserver` (threshold: 0.8) fires `POST /api/items/{id}/seen` the first time an item enters the viewport. Items already in the DB with a non-null `seen_at` are not re-posted.

### Pagination

Items are fetched in pages of 50. When the user is within 10 items of the end of the loaded list, the next page is requested and appended.

---

## Tests

All tests live in `tests/`. The shared `conftest.py` provides:

- `db` fixture — opens an `:memory:` SQLite connection, runs `schema.py`, yields the connection.
- `client` fixture — `httpx.AsyncClient` wrapping the FastAPI app with the in-memory DB overriding the `get_db()` dependency.
- `mock_http` fixture — `respx.MockRouter` for mocking external RSS/media fetches.

Coverage target: **90 %** (enforced by `--cov-fail-under=90` in `pytest` config).

Coverage report is written to `htmlcov/` (gitignored).

```
tests/
├── conftest.py       # db, client, mock_http fixtures
├── test_opml.py      # OPML parsing: valid file, missing file, malformed XML
├── test_fetcher.py   # feed fetch: new items, duplicate suppression, media detection
├── test_sync.py      # opml_sync: add feed, remove feed cascade, refresh_all_feeds
├── test_cache.py     # cache: write/read, evict by count, evict by age
├── test_detector.py  # media type detection: enclosure, media:content, og:image, skip
└── test_api.py       # endpoint tests: /api/feeds, /api/items filters, /seen, /proxy, /status
```

---

## Dev Workflow

```bash
# install all deps including dev extras
uv sync --extra dev

# run the app
uv run uvicorn src.main:app --reload --port 8080

# lint (check only)
uv run ruff check .

# lint + fix
uv run ruff check --fix .

# format
uv run ruff format .

# tests + coverage
uv run pytest

# open coverage report
open htmlcov/index.html
```

---

## Docker

### Dockerfile

```dockerfile
FROM python:3.14-alpine

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

COPY src/ ./src/

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

### docker-compose.yml

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

---

## Key Implementation Notes

- **SQLite WAL mode**: `PRAGMA journal_mode=WAL` on every connection. Concurrent API reads while the scheduler writes, no locking.
- **Connection strategy**: scheduler holds one persistent `aiosqlite` connection; API uses per-request connections via `get_db()`.
- **Slideshow transition timing**: `SLIDESHOW_TRANSITION_MS` is injected as a CSS variable via a `<style>` block rendered into `index.html` by the backend at serve time — avoids a separate API call from JS.
- **Media detection priority**: enclosure → `<media:content>` → `<media:thumbnail>` → `og:image` from description HTML → skip.
- **GIF confirmation**: media_type stored at ingest is a URL-extension guess; confirmed from `Content-Type` on first proxy fetch and updated in DB if different.
- **Streaming proxy**: `httpx` async streaming + FastAPI `StreamingResponse`. Full file never buffered in memory.
- **Item retention**: no automatic pruning. Add `ITEM_RETENTION_DAYS` in a future migration if needed.
- **`uv.lock` is committed**: ensures reproducible installs in Docker and CI.

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.
