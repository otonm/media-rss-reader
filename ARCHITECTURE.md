# Architecture

Developer reference for media-rss-reader. Covers system structure, data flows, and the reasoning behind key design decisions.

---

## System Overview

Three planes interact at runtime:

```
┌──────────────────────────────────────────────────────────────┐
│  Browser (Vanilla JS — 7 modules)                            │
│  item-store · feed-view · scroll-controller                  │
│  autoscroll-controller · cache-queue · controls · app        │
│  IntersectionObserver × 2  ·  scroll-event fallback          │
│  PWA service worker  ·  gallery (←/→ slide navigation)      │
└──────────────────┬───────────────────────────────────────────┘
                │  HTTPS  (X-Forwarded-Proto from proxy)
┌───────────────▼─────────────────────────────────────────┐
│  AuthMiddleware                                         │
│  HTTPS enforcement  ·  session cookie validation        │
│  pass-through: /health  /login  /setup  /static/*       │
└───────────────┬─────────────────────────────────────────┘
                │
┌───────────────▼─────────────────────────────────────────┐
│  FastAPI  (Uvicorn, async)                              │
│  /health  /login  /setup  /logout                       │
│  /api/feeds  /api/items  /api/media/proxy               │
│  /api/prefetch/hint  /api/status  /api/reddit-feeds/status │
└───────────┬─────────────────────┬───────────────────────┘
            │  aiosqlite          │  filesystem
┌───────────▼──────────┐  ┌───────▼──────────────────────┐
│  SQLite (WAL mode)   │  │  /cache  (sha256-named files)│
│  feeds · items       │  │  evict by age + count        │
└──────────────────────┘  └──────────────────────────────┘
            ▲
┌───────────┴─────────────────────────────────────────────┐
│  APScheduler  (AsyncIO, in-process)                     │
│  opml_sync  every OPML_SYNC_INTERVAL s                  │
│  refresh_all_feeds  every FEED_REFRESH_INTERVAL s       │
└─────────────────────────────────────────────────────────┘
```

The scheduler holds a **persistent aiosqlite connection** (`app.state.db`) for its process lifetime. API endpoints open a fresh connection per request via `get_db()`. SQLite WAL mode allows concurrent reads while the scheduler writes.

---

## Directory Map

```
src/
├── main.py          FastAPI app; lifespan hook; HTML injection
├── config.py        Settings dataclass — all config from env vars
├── scheduler.py     APScheduler setup; HTTP client singleton; startup tasks
│
├── auth/
│   ├── middleware.py  HTTPS enforcement + session cookie validation
│   ├── routes.py      GET/POST /login, GET/POST /setup, POST /logout
│   ├── session.py     Sign/verify session and setup cookies (itsdangerous)
│   ├── totp.py        Generate secrets, build otpauth:// URIs, verify codes
│   └── lockout.py     In-process IP lockout tracker
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
│   ├── detector.py      Detect media URL + type; gallery extraction via detect_all_media()
│   ├── cache.py         Filesystem cache: write, read, evict, stream_write
│   ├── prefetch.py      Background warm tasks: startup warmup + ahead-of-cursor
│   └── availability.py  Track dead URLs (upstream 404); drop items whose media is gone
│
├── api/
│   ├── feeds.py        GET /api/feeds
│   ├── items.py        GET /api/items, GET /api/items/count, POST /api/items/{id}/seen
│   ├── media.py        GET /api/media/proxy, POST /api/prefetch/hint, GET /api/status
│   └── reddit_feeds.py GET /api/reddit-feeds/status (proxies Reddit Feeds API)
│
└── static/
    ├── index.html               App shell; <!-- CONFIG_VARS --> injection point
    ├── login.html               Standalone login form (no SPA dependency)
    ├── setup.html               First-time TOTP setup with client-side QR rendering
    ├── qrcode.min.js            Bundled node-qrcode browser build (no CDN)
    ├── manifest.json            PWA manifest (standalone display, icons)
    ├── sw.js                    Service worker — caches UI assets on install
    ├── favicon.svg              SVG favicon
    ├── icon-192.png             PWA icon 192×192
    ├── icon-512.png             PWA icon 512×512
    ├── icon-512-maskable.png    PWA maskable icon 512×512
    ├── style.css                Layout + theming via CSS custom properties
    ├── app.js                   Startup, config reading, keymap, module wiring
    ├── item-store.js            Item list, pagination, seen state
    ├── feed-view.js             DOM rendering: placeholders, media items, galleries
    ├── scroll-controller.js     IntersectionObserver + scroll event → currentIndex, seen marking
    ├── autoscroll-controller.js Per-item dwell timer; image/GIF/video advance
    ├── cache-queue.js           Priority download queue (single-worker)
    └── controls.js              FAB menu, mute, autoscroll, show-seen, status modal
```

---

## Startup Sequence

`main.py` uses FastAPI's `lifespan` context manager. Steps run in order on `docker compose up`:

1. **`_build_html()`** — reads `index.html`, replaces `<!-- CONFIG_VARS -->` with a `<style>` block containing CSS variables derived from settings (`--feed-initial-count`, `--image-autoscroll-delay-s`), and replaces `{{VERSION}}` in asset URLs with `int(time.time())` for cache-busting across restarts. The result is cached in `app.state.html` for the lifetime of the process.

2. **`open_db()`** — opens the SQLite file, sets `row_factory = aiosqlite.Row` (so rows behave like dicts), enables `PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON`. Creates parent directories if needed.

3. **`create_schema()`** — runs `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` for `feeds` and `items`. Safe to run on every startup.

4. **`run_migrations()`** — reads `PRAGMA user_version`, applies any pending SQL statements from `MIGRATIONS[]`, increments the version after each one.

5. **`start_scheduler()`** — creates the shared `httpx.AsyncClient`, registers two APScheduler interval jobs, starts the scheduler, then immediately fires both jobs (OPML sync + feed refresh) so the reader is populated on first boot without waiting for the first scheduled interval. Startup errors are caught and logged as warnings — the scheduler will retry on the next interval.

6. **`warm_startup_cache()`** — fired as a background `asyncio.Task` (does not block startup). Queries the most recent `CACHE_MAX_ITEMS` media URLs and downloads them with a semaphore of 10 concurrent fetches and a 100 ms stagger to avoid thundering-herd on the upstream servers. Failed downloads call `mark_url_dead_and_maybe_drop` (see [Dead-URL Tracking](#dead-url-tracking-mediaavailabilitypy)).

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
detect_all_media(entry)  ←── detector.py
   │
   ├─ Tier 1: enclosures[] + media:content[]  (RSS order)
   ├─ Tier 2: <img src=...> in <description>  (if tier 1 produced ≥1 slide)
   └─ Tier 3: fallback — media:thumbnail / og:image  (single item)
   │
   ▼  [(url, media_type), ...] or []
store entry: media_json = slides[], media_url = first slide URL
INSERT OR IGNORE INTO items
```

**Gallery detection** (`detector.py:detect_all_media`): builds a slide list from three tiers. Tier 1 collects `<enclosure>` and `<media:content>` entries in RSS order. If tier 1 produced at least one hit, tier 2 also scans `<img src=...>` tags in the entry's `<description>` HTML (after unescaping HTML entities — covers Reddit-style feeds where only the first image is an enclosure). Tier 3 is a single-item fallback using `media:thumbnail` or `og:image` when structured media produced nothing. The full slide list is stored as `media_json`; `media_url` always holds the first slide's URL for backwards compatibility.

**Media type** is determined by file extension on the URL path (query string stripped). Extensions map to `image`, `gif`, or `video`. URLs with no recognised extension are skipped.

**IDs** are SHA-256 hashes:
- `feed_id = sha256(feed_url)`
- `item_id = sha256(feed_id + entry_guid)`

This makes IDs stable and collision-resistant without a sequence counter, and deduplication (`INSERT OR IGNORE` on the `(feed_id, guid)` unique constraint) is handled entirely by SQLite.

---

## Database

### Schema

```sql
feeds              (id PK, url UNIQUE, title, last_fetched_at, created_at)
items              (id PK, feed_id FK→feeds CASCADE, guid, title,
                    media_url, media_type, media_json, pub_date,
                    fetched_at, seen_at)
seen_guids         (feed_id, guid PK, seen_at)     -- tombstone for seen state
dead_urls          (url PK, marked_at)              -- media URLs that returned 404
unavailable_guids  (feed_id, guid PK, marked_at)    -- guids whose media is all dead
auth_config        (key PK, value)                  -- stores TOTP secret
```

`media_json` is a JSON array of `{url, type}` objects for gallery items (migration v5). Rows without gallery data fall back to the single `media_url`/`media_type` columns. `seen_guids` (migration v2/v3) preserves seen state across item pruning. `dead_urls` (v6) and `unavailable_guids` (v7) track dead media for auto-removal.

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

Current migrations (v1–v7): `fetched_at` index, `seen_guids` table + backfill, `auth_config` table, `media_json` column, `dead_urls` table, `unavailable_guids` table.

---

## Media Subsystem

### Cache (`media/cache.py`)

Files are stored at `{CACHE_DIR}/{sha256(url)}` — no subdirectories, no extension. The sha256 filename makes lookup O(1) and avoids filesystem issues with special characters in URLs.

**Write**: `cache_stream_write(url, byte_iterator, content_type)` — streams chunks to a temp file, then atomically renames. Writes content-type metadata alongside via `cache_write_meta`. Avoids buffering the full file in memory.

**Read**: `cache_read(url)` — returns the `Path` if the file exists, else `None`. `cache_read_meta(url)` reads the stored content-type.

**Evict**: `evict()` — called after each feed refresh. Deletes files older than `CACHE_MAX_AGE_HOURS` first, then trims by count from the oldest if still over `CACHE_MAX_ITEMS`. Files are sorted by `mtime` to determine age and eviction order.

### Prefetch (`media/prefetch.py`)

**Startup warmup** (`warm_startup_cache`): queries the most recent `CACHE_MAX_ITEMS` items by `pub_date` and fires a background task for each, staggered by 100 ms with a semaphore of 10. The stagger avoids a burst of concurrent requests on startup.

**Ahead-of-cursor** (`prefetch_ahead`): given a current `item_id`, queries `PREFETCH_AHEAD` items with an earlier `pub_date`. Called from the `/api/prefetch/hint` endpoint, which the browser fires as a fire-and-forget POST whenever it loads a new page of items.

### Streaming Proxy (`api/media.py`)

`GET /api/media/proxy?url=<encoded>&item_id=<optional>`:
1. Check `cache_read(url)` — if hit, return `FileResponse` (zero-copy via sendfile)
2. On miss: stream from upstream to a temp file via `cache_stream_write` (no in-memory buffer), then serve
3. On upstream non-success: call `mark_url_dead_and_maybe_drop(url, item_id, db)` to record the failure and potentially drop the item

The `item_id` parameter (added in `74e2b9e`) enables dead-URL tracking for gallery slides that aren't the primary `media_url`.

### Dead-URL Tracking (`media/availability.py`)

`mark_url_dead_and_maybe_drop(url, item_id, db)` is called by the proxy and prefetch warmer on every upstream non-success:

1. Records `url` in `dead_urls`
2. Finds all items containing that URL (by `item_id` if given, and by `media_url` matching)
3. For each item whose every URL is now in `dead_urls`: `DELETE` the item row and write a tombstone to `unavailable_guids`
4. On the next `_refresh_feed` run, `unavailable_guids` tombstones block re-insert of the same guid via `INSERT OR IGNORE`

This means a post whose media permanently 404s is silently removed from the feed without manual intervention.

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

Sets `seen_at = datetime('now')` and writes through to `seen_guids` (so seen state survives pruning). Returns the timestamp. The browser sets `item.seen_at` on success, which prevents a double-POST on the same item.

### `GET /api/items/count`

Returns `{"count": N}` for the current filter (unseen/seen, optional `feed_id`). Used by the WebUI to populate the total counter and detect end-of-feed without a separate count per page.

### `GET /api/status`

Aggregates counts from both tables and computes cache directory size in MB. Used for health checks and operator dashboards.

### `GET /api/reddit-feeds/status`

Backend proxy for the [Reddit Feeds](https://github.com/otonm/reddit-feeds) companion service. Uses the shared `httpx.AsyncClient` to fetch `GET /status` from the URL configured in `REDDIT_FEEDS_API_URL`, forwards the JSON response on success, returns 502 if the upstream is unreachable, or passes through the upstream error status. The frontend renders the response in a modal accessible from the controls bar (📊 button).

---

## Frontend

Seven vanilla JS modules, no framework, no build step. Each module attaches to `window.MRR` and exposes a public API consumed by other modules.

### Module Map

| Module | Lines | Responsibility |
|--------|-------|---------------|
| `app.js` | ~179 | Startup, config reading from CSS vars, keymap, module wiring, service worker registration |
| `item-store.js` | ~114 | Owns the item array; handles paginated fetch + count from API |
| `feed-view.js` | ~324 | DOM rendering: placeholders, media wraps, gallery slides, seen badges |
| `scroll-controller.js` | ~107 | IntersectionObservers for currentIndex and seen marking |
| `autoscroll-controller.js` | ~134 | Per-item dwell timer: auto-advance on image delay, GIF duration, or video `ended` |
| `cache-queue.js` | ~121 | Single-worker priority download queue; emits `item-loaded` / `item-failed` |
| `controls.js` | ~221 | FAB menu, autoscroll/mute/show-seen toggles, Reddit Feeds status modal with live polling |

### State Model (in `item-store.js`)

```js
items[]          // all items loaded from the API
currentIndex     // index of the item currently in view
page             // next API page to request
hasMore          // false when API returns an empty page
showSeen         // bool — include seen items in the feed?
```

`currentIndex` is set by `scroll-controller.js` via `setCurrentIndex()`. There is no local `loading` flag — concurrent fetches are gated by `fetching` inside `item-store.js`. Stale responses (from a previous show-seen toggle) are handled by the caller clearing the item list before refetching.

### IntersectionObservers (`scroll-controller.js`)

Two observers, created at init and reused for every item:

| Observer | Threshold | Purpose |
|----------|-----------|---------|
| `observer` | 0.6 | Tracks most-visible item — updates `currentIndex`, sets visible video, triggers cache rebuild |
| `seenObserver` | 0 | Binary enter/leave; fires `POST /api/items/{id}/seen` when an item leaves upward |

A debounced scroll event listener on `#feed` (200 ms) acts as a secondary seen trigger for platforms where IntersectionObserver misses edge cases (desktop scroll on overflow containers). Both mechanisms call `postSeen()` which deduplicates via `item.seen_at`.

### Gallery Support (`feed-view.js`)

Items with multiple slides (detected by `Array.isArray(item.media) && item.media.length > 1`) render as a horizontally scrollable gallery:

- **`buildGallery(wrap, mediaList, firstEl)`**: creates a `.gallery` container with one `.gallery-slide` per media entry, plus a `.gallery-dots` row of indicators. Slide 1 reuses the element already downloaded by the cache queue; remaining slides load directly from the media proxy.
- **`onGalleryScroll`**: debounced (60 ms) — marks the active slide + dot by `scrollLeft / clientWidth`, pauses offscreen videos, and rebinds autoscroll to the current slide.
- **`advanceOrNext(wrap)`**: autoscroll step — advances to the next gallery slide if not on the last, otherwise snaps to the next feed item.
- **`galleryNext()` / `galleryPrev()`**: called from keyboard `←`/`→` — step slides, or snap to next/prev feed item on boundary.

A `.count-badge` in the upper-left corner shows the number of slides (hidden when count ≤ 1). A `.seen-badge` checkmark is added by `tagAsSeen()` when the item has been marked seen.

### Autoscroll (`autoscroll-controller.js`)

Per-item timer-driven autoscroll (no RAF pixel-scroll loop):

| Media type | Advance trigger |
|------------|----------------|
| image | `setTimeout(IMAGE_AUTOSCROLL_DELAY_S)` |
| gif | `setTimeout(getGifDuration(src))` — computes via GIF byte-scan |
| video | `addEventListener('ended', ...)` — fires once |

A minimum dwell floor (`IMAGE_AUTOSCROLL_DELAY_S`) prevents too-rapid advances on short GIFs or videos. When the current item changes (scroll controller fires), the timer/video listener is reset for the new item. Videos play with `loop=false` when autoscroll is on.

### GIF Duration Byte-Scan

`getGifDuration(url)` fetches the GIF bytes and scans for Graphic Control Extension blocks (`0x21 0xF9 0x04`). Each block contains a 2-byte delay in 1/100 s units. Returns the sum of all frame delays, clamped to [50 ms, 60 s]. Falls back to `imageAutoscrollDelayMs` if the scan fails or the URL isn't a media proxy URL.

### Cache Queue (`cache-queue.js`)

A single-worker priority download queue — downloads one media file at a time. On `rebuild(currentIndex, lookaheadN, items)`, the queue is rebuilt with the current item first, then forward lookahead items, then backward items, then the rest. Already-cached items are skipped. When the worker finishes a download, it emits `item-loaded(id, el)`; `feed-view.js` replaces the corresponding `.placeholder` with a `.media-item`.

The frontend also fires a fire-and-forget `POST /api/prefetch/hint` after each rebuild, which triggers server-side prewarming of the disk cache ahead of the browser-side worker.

### CSS Variable Injection

`main.py:_build_html()` replaces `<!-- CONFIG_VARS -->` in `index.html` with a `<style>` block:

```html
<style>:root{
  --feed-initial-count:10;
  --image-autoscroll-delay-s:2
}</style>
```

`app.js:readConfig()` reads these at module load via `getComputedStyle(document.documentElement).getPropertyValue(...)`. This avoids a separate API call and ensures values are available synchronously before any rendering.

Additionally, `{{VERSION}}` in static asset URLs (`style.css?v={{VERSION}}`, `app.js?v={{VERSION}}`, etc.) is replaced with `int(time.time())` at startup, forcing browsers and the service worker cache to re-fetch assets after a restart.

---

## Configuration

`config.py` defines a `Settings` dataclass. Every field maps to an environment variable of the same name (uppercased). `_load_settings()` reads `os.environ` at import time and returns a singleton `settings` object. No `.env` file parsing at the Python level — that is handled by Docker/the shell.

Frontend-visible values (`feed_initial_count`, `image_autoscroll_delay_s`) travel to the browser as CSS custom properties injected into the HTML at startup — see [CSS Variable Injection](#css-variable-injection) above.

---

## Authentication

Authentication is handled by the `src/auth/` module. It is isolated from all other application logic.

### Middleware (`auth/middleware.py`)

`AuthMiddleware` is a Starlette `BaseHTTPMiddleware` registered on the FastAPI app before all routers. Every inbound request passes through it:

1. **Health bypass** — `/health` is passed through unconditionally (no HTTPS header required). This allows Docker / container orchestrators to run liveness probes from inside the container without needing proxy headers.
2. **HTTPS check** — rejects requests where `X-Forwarded-Proto != https` with `403`. The app assumes it is always behind a trusted TLS-terminating reverse proxy. Do not expose it directly to the internet.
3. **Auth-free paths** — `/login`, `/setup`, and all `/static/*` paths are passed through without a session check.
4. **Session validation** — all other paths require a valid signed session cookie. Invalid or missing → `302` redirect to `/login`.

### Session Cookies (`auth/session.py`)

Sessions are stateless signed tokens using `itsdangerous.URLSafeTimedSerializer`. The signing key is `AUTH_SECRET_KEY`. Cookies are `HttpOnly`, `Secure`, `SameSite=Lax`, `Max-Age=604800` (7 days). Rotating `AUTH_SECRET_KEY` immediately invalidates all active sessions.

A separate short-lived setup cookie (10-minute TTL) carries the TOTP secret during the first-login flow.

### Login Flow (`auth/routes.py`)

**Normal login** (TOTP already configured):
```
POST /login → IP lockout check (429 if locked)
           → secrets.compare_digest(password) — timing-safe, 401 on fail
           → pyotp TOTP verify (valid_window=1, ±30 s clock tolerance), 401 on fail
           → reset lockout counter
           → set 7-day session cookie → 303 redirect to /
```

**First login** (no TOTP secret in DB):
```
POST /login → check password → detect missing TOTP
           → generate base32 secret
           → set 10-min setup cookie → 303 redirect to /setup

GET  /setup → verify setup cookie (403 if missing/expired)
           → serve setup.html with otpauth:// URI (client-side QR) + copyable base32 secret

POST /setup → verify setup cookie
           → pyotp verify against temporary secret
           → persist secret to auth_config table
           → clear setup cookie, set 7-day session cookie → 303 redirect to /
```

### TOTP (`auth/totp.py`)

`pyotp.random_base32()` generates the secret. The `otpauth://` URI is embedded in `setup.html`; `qrcode.min.js` (bundled node-qrcode browser build, no CDN) renders it into a `<canvas>` QR code on the client. The base32 secret is also shown as copyable text for manual entry.

### IP Lockout (`auth/lockout.py`)

An in-process `LockoutTracker` dict keyed by `X-Forwarded-For` first value. After `AUTH_LOCKOUT_ATTEMPTS` failures the IP is locked for `AUTH_LOCKOUT_MINUTES` minutes. The lockout applies to both `POST /login` and `POST /setup`. The counter resets on successful login.

### Database (`auth_config` table)

Migration v4 adds:
```sql
CREATE TABLE IF NOT EXISTS auth_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)
```

A single row `('totp_secret', '<base32>')` is written on first successful TOTP setup.

---

## Testing

Tests live in `tests/`. The `conftest.py` provides fixtures:

| Fixture | What it is |
|---------|-----------|
| `db` | In-memory aiosqlite connection with schema applied |
| `client` | `httpx.AsyncClient` wrapping the FastAPI app with `get_db` overridden to use the in-memory DB |
| `mock_http` | `respx.MockRouter` for intercepting external HTTP requests |
| `auth_settings` | Monkeypatches `settings` with test credentials and a fixed secret key |
| `auth_client` | Test app with auth routes + `AuthMiddleware`; resets the lockout tracker between tests; sends `X-Forwarded-Proto: https` by default |
| `authed_client` | `auth_client` pre-loaded with a valid signed session cookie |

Coverage target: **90 %** (enforced by `--cov-fail-under=90`).

```bash
uv run pytest                    # run all tests with coverage
uv run pytest tests/test_api.py  # run one file
open htmlcov/index.html          # view HTML report
```
