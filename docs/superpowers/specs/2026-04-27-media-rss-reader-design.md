# Media RSS Reader — Design Spec

**Date:** 2026-04-27  
**Status:** Approved

---

## Context

Building a self-hosted media RSS reader from scratch. No source code exists yet — only the CLAUDE.md spec and tooling config. The goal is a Docker-deployed FastAPI backend that continuously fetches RSS feeds and stores media items (images, GIFs, videos) in SQLite, served to a vanilla JS single-page app with scroll and slideshow modes.

The implementation follows **strict TDD**: tests are written before implementation in every phase. Each phase must reach a fully green `ruff check` + `pytest` run (90% coverage gate) before the next phase starts.

---

## Approach: Outside-in TDD, phased by layer

Each phase builds one cohesive layer, tests first. No phase begins until all prior phases are green. The coverage gate (90%) is enforced from Phase 1 onward.

**TDD rhythm per phase:**
1. Write failing tests
2. Write minimum implementation to make them pass
3. `uv run ruff check .` + `uv run pytest` — both green
4. Commit

---

## Phase Breakdown

### Phase 1 — Scaffold + DB layer

**Purpose:** Working project scaffold with a tested, migrating SQLite layer.

Files (tests first):
- `pyproject.toml`, `uv.lock` — tooling only, no tests
- `src/__init__.py`, `src/config.py` — Pydantic Settings, all env vars
- `src/db/__init__.py`, `src/db/connection.py` — aiosqlite; sets WAL mode on connect; `get_db()` FastAPI dependency
- `src/db/schema.py` — idempotent `CREATE TABLE IF NOT EXISTS` for `feeds` and `items`
- `src/db/migrations.py` — `PRAGMA user_version` counter; applies pending migrations in sequence
- `tests/conftest.py` — `db` fixture: in-memory aiosqlite, runs schema + migrations
- `tests/test_db.py` — schema idempotency, migration versioning, WAL mode pragma

### Phase 2 — Feed ingestion

**Purpose:** OPML parsing, per-feed fetching, deduplication, scheduler wiring.

Files (tests first):
- `src/feeds/__init__.py`, `src/feeds/opml.py` — parse OPML; return `[{url, title}]`
- `src/feeds/fetcher.py` — fetch + parse one RSS feed via httpx + feedparser; extract media items
- `src/feeds/sync.py` — orchestrate OPML sync and per-feed refresh; write to DB with `INSERT OR IGNORE`
- `src/scheduler.py` — APScheduler setup; registers `opml_sync` and `refresh_all_feeds` jobs
- `tests/test_opml.py` — valid file, missing file, malformed XML
- `tests/test_fetcher.py` — new items, duplicate suppression, media detection
- `tests/test_sync.py` — add feed, remove feed cascade, `refresh_all_feeds`
- `conftest.py` gains `mock_http` fixture (respx MockRouter)

### Phase 3 — Media layer

**Purpose:** Media type detection, filesystem cache, prefetch queue.

Files (tests first):
- `src/media/__init__.py`, `src/media/detector.py` — detection priority: enclosure → `<media:content>` → `<media:thumbnail>` → `og:image`; best-guess type at ingest
- `src/media/cache.py` — write/read by `sha256(url)`; evict by count (`CACHE_MAX_ITEMS`) and age (`CACHE_MAX_AGE_HOURS`)
- `src/media/prefetch.py` — receives current item id; looks up next N by `pub_date DESC`; fires `asyncio.create_task` GETs to warm cache
- `tests/test_detector.py` — enclosure, media:content, og:image, skip
- `tests/test_cache.py` — write/read, evict by count, evict by age

Note: `prefetch.py` has no dedicated test file — its `asyncio.create_task` fire-and-forget logic is tested via `POST /api/prefetch/hint` in `test_api.py` (Phase 4).

### Phase 4 — API endpoints + app wiring

**Purpose:** All HTTP endpoints working against the real DB layer; FastAPI app assembled.

Files (tests first):
- `src/main.py` — FastAPI app; lifespan hook: DB init → migrations → scheduler start; serves `index.html` with `SLIDESHOW_TRANSITION_MS` injected via `str.replace` on a sentinel comment
- `src/api/__init__.py`, `src/api/feeds.py` — `GET /api/feeds`
- `src/api/items.py` — `GET /api/items`, `POST /api/items/{id}/seen`
- `src/api/media.py` — `GET /api/media/proxy` (streaming), `POST /api/prefetch/hint`, `GET /api/status`
- `tests/test_api.py` — all endpoints; filters, pagination, seen, proxy, status
- `conftest.py` gains `client` fixture: `httpx.AsyncClient` wrapping the app with in-memory DB override

### Phase 5 — Frontend

**Purpose:** Full single-page UI — scroll mode, slideshow mode, dark/light mode, key bindings, seen tracking, pagination.

Files:
- `src/static/index.html` — SPA shell; sentinel comment for transition injection
- `src/static/style.css` — CSS custom properties for dark/light themes; slideshow layers A/B; transitions
- `src/static/app.js` — scroll, slideshow, auto-scroll, key bindings (`j/k/a/s/m/d`), IntersectionObserver seen tracking, pagination (load next page within 10 items of end)

Verification: manual smoke via `uv run uvicorn src.main:app --reload --port 8080`. No automated JS tests.

### Phase 6 — Docker + smoke test

**Purpose:** Containerised deployment; end-to-end smoke test.

Files:
- `Dockerfile` — `python:3.14-alpine`; installs via `uv sync --no-dev --frozen`; `COPY src/ ./src/`
- `docker-compose.yml` — wires volumes (`/data`, `/cache`), env_file, port 8080
- `.gitignore` — `htmlcov/`, `__pycache__/`, `.env`, `*.db`
- `feeds.opml` — minimal sample OPML

Smoke test:
```bash
docker compose up --build -d
curl -f http://localhost:8080/api/status
docker compose down
```

---

## Key Implementation Decisions

**DB connections**
- Scheduler: one persistent `aiosqlite` connection, opened at startup in the lifespan hook
- API: per-request connection via `get_db()` FastAPI dependency
- Both set `PRAGMA journal_mode=WAL` immediately on connect

**`src/` package**
- `src/__init__.py` enables `from src.db.connection import get_db` imports
- `pyproject.toml` sets `known-first-party = ["src"]`

**Config**
- Single `Settings` (pydantic-settings) in `src/config.py`
- Module-level singleton `settings = Settings()` — imported directly, no DI

**Slideshow transition injection**
- `src/main.py` reads `index.html` as a string and `str.replace`s a sentinel comment with a `<style>` block containing the `SLIDESHOW_TRANSITION_MS` value — no Jinja2 dependency

**Media type at ingest**
- Stored as a URL-extension guess; corrected from `Content-Type` header on first proxy fetch and updated in DB

**Prefetch**
- `POST /api/prefetch/hint` → look up next N items → `asyncio.create_task` for each cache-miss fetch — fire-and-forget

---

## Verification

Each phase: `uv run ruff check . && uv run pytest` must be green with ≥ 90% coverage.

Final: `docker compose up --build -d && curl -f http://localhost:8080/api/status && docker compose down`.
