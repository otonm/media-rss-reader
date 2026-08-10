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
│  PWA service worker  ·  gallery (←/→ slide navigation)       │
└──────────────────┬───────────────────────────────────────────┘
                │  HTTPS  (X-Forwarded-Proto from proxy)
┌───────────────▼─────────────────────────────────────────┐
│  AuthMiddleware                                         │
│  HTTPS enforcement  ·  session cookie validation        │
│  pass-through: /health  /login  /setup  /static/*       │
└───────────────┬─────────────────────────────────────────┘
                │
┌───────────────▼─────────────────────────────────────────────┐
│  FastAPI  (Uvicorn, async)                                  │
│  /health  /login  /setup  /logout                           │
│  /api/feeds  /api/items  /api/media/proxy                   │
│  /api/prefetch/hint  /api/reddit-feeds/status                  │
└───────────┬─────────────────────┬───────────────────────────┘
            │  aiosqlite          │  filesystem
┌───────────▼──────────┐  ┌───────▼──────────────────────┐
│  SQLite (WAL mode)   │  │  /cache  (sha256-named files)│
│  feeds · items       │  │  evict by age + count        │
└──────────────────────┘  └──────────────────────────────┘
            ▲
┌───────────┴─────────────────────────────────────────────┐
│  APScheduler  (AsyncIO, in-process)                     │
│  sync_feeds  every OPML_SYNC_INTERVAL s                 │
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
│   ├── items.py        GET /api/items, POST /api/items/{id}/seen
│   ├── media.py        GET /api/media/proxy, POST /api/prefetch/hint
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
    ├── zoom-controller.js       Zoom an image to 100%, pan it, animate in/out
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

6. **`warm_startup_cache()`** — fired as a background `asyncio.Task` (does not block startup). Queries the most recent `CACHE_MAX_ITEMS` media URLs and downloads them with a semaphore of 10 concurrent fetches, to avoid thundering-herd on the upstream servers. Failed downloads call `mark_url_dead_and_maybe_drop` (see [Dead-URL Tracking](#dead-url-tracking-mediaavailabilitypy)).

---

## Background Scheduler

`scheduler.py` owns a `_State` singleton holding the `AsyncIOScheduler` instance and the shared `httpx.AsyncClient`. Both are created in `start_scheduler()` and torn down in `stop_scheduler()`.

**Job 1 — `sync_feeds`** (every `OPML_SYNC_INTERVAL` seconds, default 1 h):
- Parse the OPML file with `listparser`
- `INSERT OR IGNORE` new feeds into the `feeds` table
- `DELETE FROM feeds WHERE url NOT IN (...)` — removes feeds no longer in the file; `ON DELETE CASCADE` drops their items automatically. Skipped when the union of FEEDS_DIR and OPML is **empty**: that means the sources are unreadable (missing mount, companion service mid-restart) rather than genuinely empty, and the cascade would take every item and tombstone with it
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

**The insert statement** (`_INSERT_ITEM` in `sync.py`) carries two extra guards, both keyed on `media_key`:

| Guard | Rejects |
|---|---|
| `NOT EXISTS (... FROM items ...)` | a picture already stored under any feed — stops one image appearing once per feed carrying it |
| `NOT EXISTS (... FROM seen_media ...)` | a picture the user already saw — stops a pruned item being re-inserted from a feed that still lists it |

Both the HTTP path (`_refresh_feed`) and the local-file path (`local_xml_sync`) share this one statement. That matters: the guard it replaced lived only in `_refresh_feed`, and since `refresh_all_feeds` skips non-HTTP feed urls, FEEDS_DIR feeds never reached it and their seen posts came back on every sync.

---

## Database

### Schema

```sql
feeds              (id PK, url UNIQUE, title, last_fetched_at, created_at)
items              (id PK, feed_id FK→feeds CASCADE, guid, title,
                    media_url, media_type, media_json, pub_date,
                    fetched_at, seen_at)
seen_guids         (feed_id, guid PK, seen_at)     -- legacy seen tombstone (read-only)
seen_media         (media_key PK, seen_at)          -- durable seen record, no FK
dead_urls          (url PK, marked_at)              -- media URLs that returned 404
unavailable_guids  (feed_id, guid PK, marked_at)    -- guids whose media is all dead
auth_config        (key PK, value)                  -- stores TOTP secret
```

`media_json` is a JSON array of `{url, type}` objects for gallery items (migration v5). Rows without gallery data fall back to the single `media_url`/`media_type` columns. `dead_urls` (v6) and `unavailable_guids` (v7) track dead media for auto-removal.

`seen_media` (v14) is what keeps a seen post from coming back. `items.seen_at` dies with the row, and `prune_items` deletes seen rows first, so the next sync would re-insert the item straight out of a feed that still lists it. Keying on `media_key` rather than `(feed_id, guid)` means a cross-posted picture stays seen no matter which feed carries it, and it deliberately has **no foreign key to `feeds`** — its predecessor `seen_guids` was cascaded away whenever `sync_feeds` dropped a feed row. `seen_guids` (v2/v3) is still read once at startup by `backfill_seen_media()` to migrate its history; nothing writes it any more.

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

Current migrations (v1–v14): `fetched_at` index, `seen_guids` table + backfill, `auth_config` table, `media_json` column, `dead_urls` table, `unavailable_guids` table, `site_link` column, `media_url`/`media_key` indexes, `media_key` column, `media_hashes` table + index, `seen_media` table.

`backfill_seen_media()` runs after the list, on every startup. It is not a plain SQL migration because it needs `media_key()`, which is Python. It is idempotent: it tops up `seen_media` from `items.seen_at` and from the `seen_guids` join, then deletes any unseen row whose picture is already recorded as seen.

---

## Media Subsystem

### Cache (`media/cache.py`)

Files are stored at `{CACHE_DIR}/{sha256(url)}` — no subdirectories, no extension. The sha256 filename makes lookup O(1) and avoids filesystem issues with special characters in URLs.

**Write**: `cache_stream_tee(url, byte_iterator, content_type)` is the primitive — it streams chunks to a temp file *and yields each one onward*, so the proxy can serve the browser and fill the cache in a single pass. `cache_stream_write(...)` simply drains it and returns `(path, sha256)` for callers that only want the file. Neither buffers the full file in memory.

Two details that matter, both fixes for cache entries that failed permanently:

- The temp file name is unique per writer (`tempfile.mkstemp`). Two writers racing on the same URL is the *normal* case — the browser's proxy GET routinely overlaps the background warm for the same item — and with a shared temp name the second writer's `open("wb")` truncated the first's in-flight file, while the loser's failed rename deleted the winner's `.meta` sidecar.
- The sidecar is written *before* the data rename, so a file visible to `cache_read` always has its content type. Without one the proxy falls back to `text/plain` (the filename is a bare sha256, so `mimetypes` cannot guess), which no browser will decode as video.

`download_claim(url)` is an advisory in-flight registry: the prefetcher skips a URL another download already holds, so the same file is not pulled from the origin several times at once.

**Read**: `cache_read(url)` — returns the `Path` if the file exists, else `None`. `cache_read_meta(url)` reads the stored content-type.

**Evict**: `evict()` — called after each feed refresh. Deletes files older than `CACHE_MAX_AGE_HOURS` first, then trims by count from the oldest if still over `CACHE_MAX_ITEMS`. Files are sorted by `mtime` to determine age and eviction order. `.meta` and `.tmp` entries are skipped: sidecars are not cache entries in their own right, and a `.tmp` is an in-flight download that must not be counted or unlinked.

### Upstream Fetch (`media/fetch.py`)

Shared by the proxy and the prefetcher so the two cannot drift apart. `open_upstream(url, item_id, client)` returns an unread streaming response, or marks the URL dead and raises `UpstreamError` on a non-success status. `tee_to_cache(url, response)` yields the body onward while `cache_stream_tee` writes it, then records the content digest for dedup. `fetch_to_cache(...)` drains the tee for callers that only want the cache filled.

Every DB write here runs on its own connection via `run_with_own_db`, because a streaming response body executes *after* the route function returned and its request-scoped connection was closed.

An abandoned stream (the user scrolled past) caches nothing: `cache_stream_tee` only publishes a file it finished writing, and `tee_to_cache` wraps it in `contextlib.aclosing` so the temp file is cleaned up immediately rather than whenever the generator is finalised.

### Prefetch (`media/prefetch.py`)

**Startup warmup** (`warm_startup_cache`): queries the most recent `CACHE_MAX_ITEMS` items by `pub_date` and fires a background task for each, with a semaphore of 10 to avoid a thundering herd on the upstream servers.

**Ahead-of-cursor** (`prefetch_ahead`): given a current `item_id`, queries `PREFETCH_AHEAD` items with an earlier `pub_date`. Called from the `/api/prefetch/hint` endpoint, which the browser fires as a fire-and-forget POST whenever it loads a new page of items.

### Streaming Proxy (`api/media.py`)

`GET /api/media/proxy?url=<encoded>&item_id=<optional>`:
1. Check `cache_read(url)` — if hit, return `FileResponse` (zero-copy via sendfile, and Range-capable, which is what makes a cached video seekable)
2. On miss: `open_upstream` + `StreamingResponse(tee_to_cache(...))` — upstream bytes go to the browser and into the cache in the same pass, so the browser starts painting on the first chunk
3. On upstream non-success: `mark_url_dead_and_maybe_drop(url, item_id, db)` records the failure and potentially drops the item, then the proxy returns 502

Step 2 previously downloaded the whole file to disk and only then replied, which meant the browser saw *nothing* — a full-screen spinner on a black background — for the entire upstream transfer. That was the single largest contributor to the "black screen while loading" symptom. A miss is served as a non-Range `200`, so an uncached video is not seekable until it has been cached; a cache hit is. Documented trade-off (F7): the miss path intentionally does not honour `Range` — seeking an uncached video, or Safari's initial byte-range probe for `<video>`, restarts from zero — because streaming the miss through is what prevents the black-screen stall on first paint.

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

`offset` is a raw row offset, not a page number. With `unseen=true` the result set shrinks as the client marks items seen, so `page * size` over-shoots and silently skips items. The client sends how many matching items it already holds (`item-store.js: nextOffset()`).

### `POST /api/items/{id}/seen`

Sets `seen_at = datetime('now')` and writes through to `seen_media`, keyed on the normalised media URL. Returns the timestamp.

The browser marks the item locally *before* firing the request and sends it with `navigator.sendBeacon`, not `fetch`: the browser cancels in-flight fetches when the tab closes, which used to lose the marks made in the last moments of a session. Beacons are queued and delivered regardless, and there is no response to wait for. `item.seen_at` still prevents a double-POST on the same item.

### `GET /api/reddit-feeds/status`

Backend proxy for the [Reddit Feeds](https://github.com/otonm/reddit-feeds) companion service. Uses the shared `httpx.AsyncClient` to fetch `GET /status` from the URL configured in `REDDIT_FEEDS_API_URL`, forwards the JSON response on success, returns 502 if the upstream is unreachable, or passes through the upstream error status. The frontend renders the response in a modal accessible from the controls bar (📊 button).

---

## Frontend

Eight vanilla JS modules, no framework, no build step. Each module attaches to `window.MRR` and exposes a public API consumed by other modules.

### Module Map

| Module | Lines | Responsibility |
|--------|-------|---------------|
| `app.js` | ~179 | Startup, config reading from CSS vars, keymap, module wiring, service worker registration |
| `item-store.js` | ~114 | Owns the item array; handles paginated fetch + count from API |
| `feed-view.js` | ~324 | DOM rendering: placeholders, media wraps, gallery slides, seen badges |
| `scroll-controller.js` | ~107 | IntersectionObservers for currentIndex and seen marking |
| `autoscroll-controller.js` | ~134 | Per-item dwell timer: auto-advance on image delay, GIF duration, or video `ended` |
| `cache-queue.js` | ~121 | Single-worker priority download queue; emits `item-loaded` / `item-failed` |
| `zoom-controller.js` | ~200 | Double-tap / double-click / `z` zooms an image to 1:1; cursor-follow pan on desktop, finger drag on mobile; animates over `ZOOM_TRANSITION_MS` |
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

A priority download queue drained by **3 concurrent workers**. On `rebuild(currentIndex, lookaheadN, items)`, the queue is rebuilt with the current item first, then forward lookahead items, then backward items, then the rest. Items already loaded this session are skipped. When a worker finishes a download, it emits `item-loaded(id, el, ms)`; `feed-view.js` replaces the corresponding `.placeholder` with a `.media-item`.

Within each band, items the server reported as `cached: true` are queued **ahead** of uncached ones. A cached item decodes in milliseconds while a miss waits on the origin, so this is what makes a scroll through warm items feel instant. The current item is exempt — it is what the user is looking at and must load either way.

Each download carries a **10 s deadline**; on expiry the element's `src` is cleared to drop the connection and `item-failed(id, reason)` fires. Three workers plus a deadline is what stops one stalled origin from freezing every placeholder behind it: with a single worker and no timeout, a screen of spinners could sit there indefinitely — including items already on disk that would have painted immediately. Three also stays inside the browser's ~6-connections-per-host budget alongside gallery slides.

The frontend also fires a fire-and-forget `POST /api/prefetch/hint` after each rebuild, which triggers server-side prewarming of the disk cache ahead of the browser-side workers.

### Failed items

`feed-view.js:onItemFailed(id, reason)` replaces the placeholder with a visible `.media-item.failed` tile naming the item and why it failed, and removes the item from the store so it does not come back on the next reload. It previously deleted the node outright, which made these failures invisible — the feed simply had fewer items in it than it should have.

### CSS Variable Injection

`main.py:_build_html()` replaces `<!-- CONFIG_VARS -->` in `index.html` with a `<style>` block:

```html
<style>:root{
  --feed-initial-count:10;
  --image-autoscroll-delay-s:2;
  --zoom-transition-ms:200;
  --ui-debug:0
}</style>
```

`app.js:readConfig()` reads these at module load via `getComputedStyle(document.documentElement).getPropertyValue(...)`. This avoids a separate API call and ensures values are available synchronously before any rendering.

Additionally, `{{VERSION}}` in static asset URLs (`style.css?v={{VERSION}}`, `app.js?v={{VERSION}}`, etc.) is replaced with `int(time.time())` at startup, forcing browsers and the service worker cache to re-fetch assets after a restart.

---

## Configuration

`config.py` defines a `Settings` dataclass. Every field maps to an environment variable of the same name (uppercased). `_load_settings()` reads `os.environ` at import time and returns a singleton `settings` object. No `.env` file parsing at the Python level — that is handled by Docker/the shell.

Frontend-visible values (`feed_initial_count`, `image_autoscroll_delay_s`, `zoom_transition_ms`, `ui_debug`) travel to the browser as CSS custom properties injected into the HTML at startup — see [CSS Variable Injection](#css-variable-injection) above.

`_load_settings()` parses only `int` and `str`, so flags are declared as ints (`ui_debug: int = 0`, `dedup_similarity: int = 0`) rather than bools.

### `UI_DEBUG` overlay

`UI_DEBUG=1` makes `controls.js` build a fixed overlay in the top-right corner describing the item the feed is currently snapped to: feed name, title, media type and file extension, publish date, cache `HIT`/`MISS` with the measured load time, and the download queue depth. It is `pointer-events: none` so it never intercepts a tap.

The feed *name* comes from a one-off `GET /api/feeds` mapping `feed_id → title`, because `/api/items` carries only `feed_id`. It lives in `controls.js` rather than its own file so no new `<script>` tag and no new entry in the service worker's hardcoded precache list are needed.

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