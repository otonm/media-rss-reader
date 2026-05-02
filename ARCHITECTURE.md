# Architecture

Developer reference for media-rss-reader. Covers system structure, data flows, and the reasoning behind key design decisions.

---

## System Overview

Three planes interact at runtime:

```
┌─────────────────────────────────────────────────────────┐
│  Browser (Vanilla JS)                                   │
│  Scroll view / Slideshow view                           │
│  IntersectionObserver × 3  ·  RAF auto-scroll loop     │
└───────────────┬─────────────────────────────────────────┘
                │  HTTP  (fetch / POST)
┌───────────────▼─────────────────────────────────────────┐
│  FastAPI  (Uvicorn, async)                              │
│  /api/feeds  /api/items  /api/media/proxy               │
│  /api/prefetch/hint  /api/status                        │
└───────────┬─────────────────────┬───────────────────────┘
            │  aiosqlite          │  filesystem
┌───────────▼──────────┐  ┌──────▼──────────────────────┐
│  SQLite (WAL mode)   │  │  /cache  (sha256-named files)│
│  feeds · items       │  │  evict by age + count        │
└──────────────────────┘  └─────────────────────────────-┘
            ▲
┌───────────┴─────────────────────────────────────────────┐
│  APScheduler  (AsyncIO, in-process)                     │
│  opml_sync  every OPML_SYNC_INTERVAL s                  │
│  refresh_all_feeds  every FEED_REFRESH_INTERVAL s       │
└─────────────────────────────────────────────────────────┘
```

The scheduler and the API share **one persistent aiosqlite connection** (`app.state.db`). API endpoints open short-lived per-request connections. SQLite WAL mode allows concurrent reads while the scheduler writes.

---

## Directory Map

```
src/
├── main.py          FastAPI app; lifespan hook; HTML injection
├── config.py        Pydantic Settings — all config from env vars
├── scheduler.py     APScheduler setup; HTTP client singleton; startup tasks
│
├── db/
│   ├── connection.py   open_db() + get_db() FastAPI dependency
│   ├── schema.py       CREATE TABLE / INDEX statements (idempotent)
│   └── migrations.py   Integer-versioned migrations via PRAGMA user_version
│
├── feeds/
│   ├── opml.py      Parse OPML file → list of {url, title}
│   ├── fetcher.py   Fetch one RSS feed via httpx; extract media items
│   └── sync.py      Orchestrate OPML sync + per-feed refresh; prune old items
│
├── media/
│   ├── detector.py  Detect media URL + type from a feedparser entry
│   ├── cache.py     Filesystem cache: write, read, evict
│   └── prefetch.py  Background warm tasks: startup warmup + ahead-of-cursor
│
├── api/
│   ├── feeds.py     GET /api/feeds
│   ├── items.py     GET /api/items, POST /api/items/{id}/seen
│   └── media.py     GET /api/media/proxy, POST /api/prefetch/hint, GET /api/status
│
└── static/
    ├── index.html   App shell; <!-- SLIDESHOW_TRANSITION --> injection point
    ├── app.js       All UI logic (~540 lines, 14 sections)
    └── style.css    Layout + theming via CSS custom properties
```

---

## Startup Sequence

`main.py` uses FastAPI's `lifespan` context manager. Steps run in order on `docker compose up`:

1. **`_build_html()`** — reads `index.html`, injects a `<style>` block containing CSS variables derived from settings (`--slideshow-transition-ms`, `--image-display-delay-ms`, `--prefetch-ahead`, `--auto-scroll-speed`). The result is cached in `app.state.html` for the lifetime of the process. This avoids a per-request API call from JS to retrieve these values.

2. **`open_db()`** — opens the SQLite file, sets `row_factory = aiosqlite.Row` (so rows behave like dicts), enables `PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON`. Creates parent directories if needed.

3. **`create_schema()`** — runs `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` for `feeds` and `items`. Safe to run on every startup.

4. **`run_migrations()`** — reads `PRAGMA user_version`, applies any pending SQL statements from `MIGRATIONS[]`, increments the version after each one.

5. **`start_scheduler()`** — creates the shared `httpx.AsyncClient`, registers two APScheduler interval jobs, starts the scheduler, then immediately fires both jobs (OPML sync + feed refresh) so the reader is populated on first boot without waiting for the first scheduled interval. Startup errors are caught and logged as warnings — the scheduler will retry on the next interval.

6. **`warm_startup_cache()`** — fired as a background `asyncio.Task` (does not block startup). Queries the most recent `CACHE_MAX_ITEMS` media URLs and downloads them with a semaphore of 10 concurrent fetches and a 100 ms stagger to avoid thundering-herd on the upstream servers.

---

## Background Scheduler

`scheduler.py` owns a `_State` singleton holding the `AsyncIOScheduler` instance and the shared `httpx.AsyncClient`. Both are created in `start_scheduler()` and torn down in `stop_scheduler()`.

**Job 1 — `opml_sync`** (every `OPML_SYNC_INTERVAL` seconds, default 1 h):
- Parse the OPML file with `listparser`
- `INSERT OR IGNORE` new feeds into the `feeds` table
- `DELETE FROM feeds WHERE id NOT IN (...)` — removes feeds no longer in the file; `ON DELETE CASCADE` drops their items automatically
- Does **not** fetch feed content — that is Job 2's responsibility

**Job 2 — `refresh_all_feeds`** (every `FEED_REFRESH_INTERVAL` seconds, default 15 min):
- `SELECT id, url FROM feeds` to get the current feed list
- For each feed: HTTP GET → feedparser → media detection → `INSERT OR IGNORE` into `items`
- After all feeds: `prune_items()` enforces `KEEP_ITEMS` and `ITEMS_MAX_AGE_HOURS`

**Pruning strategy** (`prune_items` in `sync.py`):
1. Delete seen items older than `ITEMS_MAX_AGE_HOURS` (seen-only — unseen items are never aged out)
2. Count remaining; if under `KEEP_ITEMS`, stop
3. Delete the oldest seen items until under the limit
4. If still over the limit, delete the oldest unseen items (last resort)

---

## Feed Pipeline

```
OPML file
   │  listparser.parse()
   ▼
[{url, title}, ...]
   │  httpx.AsyncClient.get(url)
   ▼
feedparser.parse(response.text)
   │  for each entry:
   ▼
detect_media(entry)  ←── detector.py
   │
   ├─ 1. enclosures[]         .url + file extension
   ├─ 2. media_content[]      .url + file extension
   ├─ 3. media_thumbnail[]    .url + file extension
   └─ 4. summary HTML         og:image meta tag + file extension
   │
   ▼  (url, media_type) or None
INSERT OR IGNORE INTO items
```

**Media type detection** (`detector.py`): determined by file extension on the URL path (query string stripped). Extensions map to `image`, `gif`, or `video`. URLs with no recognised extension are skipped. The type is stored at ingest time; for `.gif` files, the proxy can confirm via `Content-Type` on first fetch if needed.

**IDs** are SHA-256 hashes:
- `feed_id = sha256(feed_url)`
- `item_id = sha256(feed_id + entry_guid)`

This makes IDs stable and collision-resistant without a sequence counter, and deduplication (`INSERT OR IGNORE` on the `(feed_id, guid)` unique constraint) is handled entirely by SQLite.

---

## Database

### Schema

```sql
feeds  (id PK, url UNIQUE, title, last_fetched_at, created_at)
items  (id PK, feed_id FK→feeds CASCADE, guid, title,
        media_url, media_type, pub_date, fetched_at, seen_at)
```

Indexes on `items`: `feed_id`, `pub_date DESC`, `seen_at`, `fetched_at`.

### WAL Mode

`PRAGMA journal_mode=WAL` is set on every connection. WAL allows multiple concurrent readers while one writer is active — essential because the scheduler writes continuously while the API serves reads. Without WAL, the scheduler's write transactions would block API reads.

### Connection Strategy

| User | Connection | Lifetime |
|------|-----------|---------|
| Scheduler | `app.state.db` (persistent) | Process lifetime |
| API endpoints | `get_db()` dependency | One request |

API connections are opened and closed per request via the `get_db()` async generator. This avoids connection pool complexity while keeping the scheduler's long-running connection isolated.

### Migrations

`migrations.py` holds a flat list of SQL strings (`MIGRATIONS[]`). `PRAGMA user_version` stores the count of applied migrations. On startup, any items from `MIGRATIONS[current_version:]` are applied in sequence, with the version incremented after each one. Adding a migration = appending one string to the list.

---

## Media Subsystem

### Cache (`media/cache.py`)

Files are stored at `{CACHE_DIR}/{sha256(url)}` — no subdirectories, no extension. The sha256 filename makes lookup O(1) and avoids filesystem issues with special characters in URLs.

**Write**: `cache_write(url, data)` — creates parent dirs, writes bytes off the event loop via `asyncio.to_thread`.

**Read**: `cache_read(url)` — returns the `Path` if the file exists, else `None`. Synchronous (stat only).

**Evict**: `evict()` — called after each feed refresh. Deletes files older than `CACHE_MAX_AGE_HOURS` first, then trims by count from the oldest if still over `CACHE_MAX_ITEMS`. Files are sorted by `mtime` to determine age and eviction order.

### Prefetch (`media/prefetch.py`)

**Startup warmup** (`warm_startup_cache`): queries the most recent `CACHE_MAX_ITEMS` items by `pub_date` and fires a background task for each, staggered by 100 ms with a semaphore of 10. The stagger avoids a burst of concurrent requests on startup.

**Ahead-of-cursor** (`prefetch_ahead`): given a current `item_id`, queries `PREFETCH_AHEAD` items with an earlier `pub_date`. Called from the `/api/prefetch/hint` endpoint, which the browser fires as a fire-and-forget POST whenever it loads a new page of items.

### Streaming Proxy (`api/media.py`)

`GET /api/media/proxy?url=<encoded>`:
1. Check `cache_read(url)` — if hit, return `FileResponse` (zero-copy via sendfile)
2. On miss: fetch via the shared `httpx.AsyncClient`, write to cache, stream to the browser

The full response body is read into memory once (`aread()`) to write to cache, then streamed to the browser. Media is never buffered twice.

---

## API Layer

All routers live under `/api`. FastAPI's dependency injection (`Depends(get_db)`) provides each handler with a fresh aiosqlite connection.

### `GET /api/items`

Uses a window-function query to interleave items from multiple feeds:

```sql
WITH ranked AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY feed_id ORDER BY pub_date ASC) AS rn
    FROM items [WHERE ...]
)
SELECT ... FROM ranked ORDER BY rn ASC, feed_id ASC
LIMIT ? OFFSET ?
```

`rn=1` contains the oldest unseen item from each feed, `rn=2` the second-oldest from each, and so on. Ordering by `rn` then `feed_id` interleaves feeds evenly rather than draining one feed at a time.

### `POST /api/items/{id}/seen`

Sets `seen_at = datetime('now')` and returns the timestamp. The browser sets `item.seen_at` on success, which prevents a double-POST on the same item.

### `GET /api/status`

Aggregates counts from both tables and computes cache directory size in MB. Used for health checks and operator dashboards.

---

## Frontend

`app.js` is ~540 lines split into 14 labelled sections. No framework, no build step.

### State Model

```js
items[]          // all items loaded from the API
currentIndex     // index of the item currently in view
page             // next API page to request
loading          // prevents concurrent in-flight fetches
hasMore          // false when API returns an empty page
autoScroll       // bool — RAF loop running?
autoScrollPaused // bool — paused waiting for a video/GIF to finish?
slideshowMode    // bool — slideshow or scroll view?
showSeen         // bool — include seen items in the feed?
fetchGeneration  // incremented on reset to discard stale responses
```

### IntersectionObserver Roles

Three observers are created at module load and reused for every item:

| Observer | Threshold | Purpose |
|----------|-----------|---------|
| `seenObserver` | 0 | Fires `POST /seen` when an item's bottom edge exits through the top of the viewport (i.e. fully scrolled past) |
| `viewObserver` | 0.5 | Keeps `currentIndex` in sync during native scroll; triggers pagination when near the end |
| `mediaObserver` | 0–1 (21 steps) | Plays/pauses videos and GIFs based on visibility; pauses auto-scroll when a media element's top edge reaches the viewport top |

`seenObserver` is attached only after the media element fires `load` / `loadeddata` — this avoids false positives from zero-height elements before the image dimensions are known.

### View Modes

**Scroll mode** (default): items live in `#feed-list` inside `#scroll-view`. Navigation scrolls via `scrollIntoView`. Auto-scroll drives `scrollBy(0, AUTO_SCROLL_SPEED)` on every animation frame.

**Slideshow mode**: `#scroll-view` is hidden; `#slideshow-view` is shown. Two absolutely-positioned layers (`#slide-a`, `#slide-b`) swap `active` class on each advance, producing a CSS `opacity` crossfade. Duration is `--slideshow-transition-ms` injected at serve time. The active layer name rotates between `"a"` and `"b"` via `activeSlide`.

### CSS Variable Injection

`main.py:_build_html()` replaces the `<!-- SLIDESHOW_TRANSITION -->` comment in `index.html` with a `<style>` block:

```html
<style>:root{
  --slideshow-transition-ms:400ms;
  --image-display-delay-ms:5000ms;
  --prefetch-ahead:5;
  --auto-scroll-speed:1.5
}</style>
```

`app.js` reads these at module load via `getComputedStyle(document.documentElement).getPropertyValue(...)`. This avoids a separate API call from JS and ensures the values are available synchronously before any rendering occurs.

### Auto-Scroll RAF Loop

`rafAutoScroll()` calls `scrollBy(0, AUTO_SCROLL_SPEED)` and schedules itself with `requestAnimationFrame`. The loop is started by `startAutoScroll()` and stopped by `stopAutoScroll()` (which cancels the pending frame). `autoScrollPaused` gates the `scrollBy` call without cancelling the RAF handle, allowing instant resume when the pause condition clears.

### GIF Duration Byte-Scan

`getGifDuration(url)` fetches the GIF bytes and scans for Graphic Control Extension blocks (`0x21 0xF9 0x04`). Each block contains a 2-byte delay in 1/100 s units. The function sums all frame delays to get the total animation duration, clamped to [50 ms, 60 s]. The duration is cached on the item object after the first fetch. Auto-scroll uses this duration to pause for exactly one full GIF cycle before continuing.

---

## Configuration

`config.py` defines a single `Settings` class extending Pydantic's `BaseSettings`. Every field maps directly to an environment variable of the same name (uppercased). No `.env` file parsing at the Python level — that is handled by Docker/the shell. `settings` is a module-level singleton imported wherever config values are needed.

Frontend-visible values (`image_display_delay_ms`, `slideshow_transition_ms`, `prefetch_ahead`, `auto_scroll_speed`) travel to the browser as CSS custom properties injected into the HTML at startup — see [CSS Variable Injection](#css-variable-injection) above.

---

## Testing

Tests live in `tests/`. The `conftest.py` provides three fixtures:

| Fixture | What it is |
|---------|-----------|
| `db` | In-memory aiosqlite connection with schema applied |
| `client` | `httpx.AsyncClient` wrapping the FastAPI app with `get_db` overridden to use the in-memory DB |
| `mock_http` | `respx.MockRouter` for intercepting external HTTP requests |

Coverage target: **90 %** (enforced by `--cov-fail-under=90`).

```bash
uv run pytest                    # run all tests with coverage
uv run pytest tests/test_api.py  # run one file
open htmlcov/index.html          # view HTML report
```
