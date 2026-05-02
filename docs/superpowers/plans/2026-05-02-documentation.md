# Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Write README.md (end-user deployment guide), ARCHITECTURE.md (developer reference), and add inline comments throughout all Python source files and app.js.

**Architecture:** Three independent deliverables — two new Markdown files at the repo root, plus comment additions scattered across 14 existing source files. No code logic changes, no tests needed.

**Tech Stack:** Markdown, Python, JavaScript, Docker, Cloudflare Tunnel + Zero Trust Access.

---

## File Map

| Action | File | Purpose |
|--------|------|---------|
| Create | `README.md` | End-user deployment guide |
| Create | `ARCHITECTURE.md` | Developer reference |
| Modify | `src/config.py` | Module docstring + field comments |
| Modify | `src/main.py` | Module docstring + block comments |
| Modify | `src/scheduler.py` | Module docstring + block comments |
| Modify | `src/db/connection.py` | Module docstring + inline comments |
| Modify | `src/db/schema.py` | Module docstring + inline comments |
| Modify | `src/db/migrations.py` | Module docstring + inline comments |
| Modify | `src/feeds/opml.py` | Module docstring + inline comments |
| Modify | `src/feeds/fetcher.py` | Module docstring + inline comments |
| Modify | `src/feeds/sync.py` | Module docstring + inline comments |
| Modify | `src/media/detector.py` | Module docstring + inline comments |
| Modify | `src/media/cache.py` | Module docstring + inline comments |
| Modify | `src/media/prefetch.py` | Module docstring + inline comments |
| Modify | `src/api/feeds.py` | Module docstring + inline comments |
| Modify | `src/api/items.py` | Module docstring + inline comments |
| Modify | `src/api/media.py` | Module docstring + inline comments |
| Modify | `src/static/app.js` | Block comments inside complex functions |

---

## Task 1: README.md

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write README.md**

Write the following content verbatim to `README.md` at the repo root:

```markdown
# Media RSS Reader

A self-hosted media viewer that turns RSS feeds containing images, GIFs, or videos into a smooth, fullscreen browsing experience — like a private feed you control.

The backend continuously fetches feeds in the background (no browser session required), stores media items in SQLite, and serves a lightweight browser UI over HTTP. All configuration is done through environment variables; no accounts, no external services, no build step.

## Features

- **Media-first** — only images, GIFs, and videos are shown; text content is ignored
- **Scroll mode** — continuous vertical feed with keyboard/swipe navigation and auto-scroll
- **Slideshow mode** — fullscreen single-item view with CSS crossfade transitions
- **Dark / light theme** — toggle with `d`, persisted across sessions
- **Auto-scroll** — continuous pixel-level drift; pauses automatically for videos and GIFs
- **Pre-fetch cache** — upcoming media is downloaded before you reach it, eliminating load stalls
- **Persistent storage** — feed items survive restarts; seen state tracked per item
- **OPML-driven** — manage your feed list with any RSS reader's export format
- **Docker-native** — single container, volume-mounted data, no external database service

## Prerequisites

- Docker ≥ 24.0
- Docker Compose v2 (`docker compose`, not `docker-compose`)

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/yourname/media-rss-reader.git
cd media-rss-reader

# 2. Copy and edit the environment file
cp .env.example .env
$EDITOR .env        # defaults work for most setups

# 3. Edit feeds.opml with your feed URLs, then start
docker compose up -d
```

Open http://localhost:8082 in your browser. The first fetch runs immediately on startup; media appears within a few seconds.

## OPML Feed List

The reader is driven by an [OPML](https://opml.org/) file — the same export format used by RSS readers like Feedly, NetNewsWire, and Reeder.

Create `feeds.opml` in the project directory (the default path the container mounts):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>My Feeds</title></head>
  <body>
    <outline type="rss" text="Hubble Images"
             xmlUrl="https://www.nasa.gov/rss/dyn/hubble_news.rss"/>
    <outline type="rss" text="Astronomy Picture of the Day"
             xmlUrl="https://apod.nasa.gov/apod.rss"/>
  </body>
</opml>
```

The file is re-read on the interval set by `OPML_SYNC_INTERVAL`. Adding or removing a feed takes effect on the next sync. Removing a feed cascades — all its stored items are deleted from the database.

## Configuration

All settings are environment variables. Copy `.env.example` to `.env` and adjust as needed. Defaults work for most setups.

| Variable | Default | Description |
|---|---|---|
| `OPML_PATH` | `/data/feeds.opml` | Path to the OPML file inside the container |
| `DB_PATH` | `/data/db/reader.db` | SQLite database path inside the container |
| `CACHE_DIR` | `/cache` | Directory for cached media files |
| `OPML_SYNC_INTERVAL` | `3600` | Seconds between OPML re-reads |
| `FEED_REFRESH_INTERVAL` | `900` | Seconds between feed refresh cycles |
| `CACHE_MAX_ITEMS` | `500` | Max number of media files kept on disk |
| `CACHE_MAX_AGE_HOURS` | `48` | Max age of cached files before eviction |
| `KEEP_ITEMS` | `1000` | Max items kept in the database |
| `ITEMS_MAX_AGE_HOURS` | `168` | Delete seen items older than this (hours; 168 = 7 days) |
| `PREFETCH_AHEAD` | `5` | Items to pre-fetch ahead of current scroll position |
| `IMAGE_DISPLAY_DELAY_MS` | `5000` | Dwell time per image/GIF in auto-scroll / slideshow (ms) |
| `SLIDESHOW_TRANSITION_MS` | `400` | CSS crossfade duration between slideshow items (ms) |
| `AUTO_SCROLL_SPEED` | `1.5` | Pixels scrolled per animation frame (~90 px/s at 60 fps) |
| `PORT` | `8080` | Port the server listens on inside the container |
| `LOG_LEVEL` | `info` | Uvicorn log level: `debug` \| `info` \| `warning` \| `error` |

## Deployment: Docker Only

Use this if you prefer plain `docker run` without Compose.

```bash
# Create named volumes for data persistence
docker volume create media-rss-data
docker volume create media-rss-cache

# Run the container
docker run -d \
  --name media-rss \
  --restart unless-stopped \
  -p 8082:8080 \
  -v ./feeds.opml:/data/feeds.opml:ro \
  -v media-rss-data:/data/db \
  -v media-rss-cache:/cache \
  --env-file .env \
  -e TZ=Europe/Berlin \
  $(docker build -q .)
```

- `-v ./feeds.opml:/data/feeds.opml:ro` — mounts your local OPML file read-only into the container
- `-v media-rss-data:/data/db` — persists the SQLite database across container restarts
- `-v media-rss-cache:/cache` — persists the media disk cache across restarts
- `--env-file .env` — loads all configuration from your `.env` file

## Deployment: Docker Compose

The included `docker-compose.yml` wires everything up:

```yaml
services:
  media-rss:
    build: .
    ports:
      - "8082:8080"           # host:container — change 8082 to your preferred port
    volumes:
      - ./feeds.opml:/data/feeds.opml:ro   # OPML feed list (read-only)
      - reader_data:/data/db               # SQLite database
      - media_cache:/cache                 # media disk cache
    env_file:
      - .env                  # load all variables from .env
    environment:
      - TZ=Europe/Berlin      # timezone
    restart: unless-stopped

volumes:
  reader_data:   # survives docker compose down
  media_cache:
```

```bash
docker compose up -d          # start in background
docker compose logs -f        # follow logs
docker compose down           # stop (volumes are preserved)
docker compose down -v        # stop AND delete all data
```

## Deployment: Cloudflare Tunnel + Access

This setup exposes the reader securely to the internet without opening firewall ports, and locks it behind Cloudflare Access email authentication so only authorised users can reach it.

**What you need:**
- A domain managed by Cloudflare (free account is sufficient)
- A Cloudflare Zero Trust account (free tier covers personal use; visit [one.dash.cloudflare.com](https://one.dash.cloudflare.com))

---

### Step 1: Create a Cloudflare Tunnel

Install `cloudflared` on your Docker host or local machine:

```bash
# Debian / Ubuntu
curl -L https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg > /dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
  https://pkg.cloudflare.com/cloudflared bookworm main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared

# macOS
brew install cloudflare/cloudflare/cloudflared
```

Log in and create a named tunnel:

```bash
cloudflared tunnel login              # opens browser — authorise in Cloudflare dashboard
cloudflared tunnel create media-reader  # creates tunnel; note the Tunnel ID in the output
```

---

### Step 2: Get a Tunnel Token

The easiest Docker deployment uses a single token rather than a credentials file:

1. Go to [one.dash.cloudflare.com](https://one.dash.cloudflare.com) → **Zero Trust** → **Networks** → **Tunnels**
2. Click the tunnel named `media-reader`
3. Open the **Configure** tab → select **Docker** in the connector instructions
4. Copy the `--token` value shown

Add it to your `.env` file:

```bash
CLOUDFLARE_TUNNEL_TOKEN=eyJhI...   # paste the full token here
```

---

### Step 3: Configure DNS

Point a subdomain at the tunnel. Either use the CLI:

```bash
cloudflared tunnel route dns media-reader reader.example.com
```

Or add it manually in the Cloudflare DNS dashboard:

| Type | Name | Target | Proxy |
|------|------|--------|-------|
| CNAME | `reader` | `<TUNNEL-ID>.cfargotunnel.com` | Proxied (orange cloud) |

Replace `<TUNNEL-ID>` with the ID printed during tunnel creation and `example.com` with your domain.

---

### Step 4: Add cloudflared as a Docker Compose Sidecar

Use this `docker-compose.yml` (note: the `ports:` mapping on `media-rss` is removed — all traffic arrives through the tunnel):

```yaml
services:
  media-rss:
    build: .
    # No host port binding — cloudflared connects to the container directly
    volumes:
      - ./feeds.opml:/data/feeds.opml:ro
      - reader_data:/data/db
      - media_cache:/cache
    env_file: .env
    environment:
      - TZ=Europe/Berlin
    restart: unless-stopped

  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel --no-autoupdate run
    environment:
      - TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}
    depends_on:
      - media-rss
    restart: unless-stopped

volumes:
  reader_data:
  media_cache:
```

Start both services:

```bash
docker compose up -d
docker compose logs cloudflared   # should show "Registered tunnel connection"
```

Visit `https://reader.example.com` — the app is accessible (unauthenticated at this point). Continue to Step 5 to add the login gate.

---

### Step 5: Enable Cloudflare Access Authentication

This adds a login page in front of the tunnel. Only users whose email address matches the policy can get in.

1. Go to **Zero Trust** → **Access** → **Applications** → **Add an application**
2. Choose **Self-hosted**
3. Fill in the application details:
   - **Application name**: `Media RSS Reader`
   - **Subdomain**: `reader`
   - **Domain**: `example.com` (your domain)
   - Leave **Session duration** at `24 hours`
4. Click **Next**
5. Under **Policies**, create a new policy:
   - **Policy name**: `Owner`
   - **Action**: `Allow`
   - **Configure rules → Include**: selector `Emails`, value `your@email.com`
6. Click **Next**, then **Add application**

Now visiting `https://reader.example.com` shows a Cloudflare login page. Enter your email address, receive a one-time code, and get a 24-hour session. No password or account setup required on your side.

**Optional: bypass the login from your home network**

Add a second Include rule to the policy:
- Selector: `IP ranges`
- Value: your home IP address or CIDR (e.g. `203.0.113.0/24`)

Requests from that IP range bypass the email check entirely.

---

## Key Bindings

| Key | Action |
|---|---|
| `j` / `↓` | Next item |
| `k` / `↑` | Previous item |
| `a` | Toggle auto-scroll |
| `s` | Toggle slideshow mode |
| `m` | Toggle mute |
| `d` | Toggle dark / light theme |

On mobile, swipe up/down to navigate. Tap ☰ to open the control menu.

## Updating

```bash
git pull
docker compose build --no-cache
docker compose up -d
```

Schema migrations run automatically on startup — no manual steps required.
```

- [ ] **Step 2: Verify the file renders correctly**

Open `README.md` in a Markdown preview. Check:
- All code blocks close properly (no runaway fences)
- The YAML blocks inside the Cloudflare section render as code (not parsed as YAML)
- The configuration table has correct column alignment
- All internal cross-references (e.g. "see section below") resolve

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add README with deployment guide and cloudflared tunnel/Access walkthrough"
```

---

## Task 2: ARCHITECTURE.md

**Files:**
- Create: `ARCHITECTURE.md`

- [ ] **Step 1: Write ARCHITECTURE.md**

Write the following content verbatim to `ARCHITECTURE.md` at the repo root:

```markdown
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

5. **`start_scheduler()`** — creates the shared `httpx.AsyncClient`, registers two APScheduler interval jobs, starts the scheduler, then immediately fires both jobs (OPML sync + feed refresh) so the reader is populated on first boot without waiting for the first interval. Startup errors are caught and logged as warnings — the scheduler will retry on schedule.

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

`migrations.py` holds a flat list of SQL strings (`MIGRATIONS[]`). `PRAGMA user_version` stores the number of applied migrations. On startup, any items from `MIGRATIONS[current_version:]` are applied in order, with the version incremented after each one. Adding a migration = appending one string to the list.

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
2. On miss: fetch via the shared `httpx.AsyncClient`, write to cache, yield bytes as a `StreamingResponse`

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
```

- [ ] **Step 2: Verify the file renders correctly**

Open `ARCHITECTURE.md` in a Markdown preview. Check:
- ASCII diagram box characters render correctly (not escaped)
- All code blocks (SQL, YAML, JS, bash) close properly
- The three-column observer table renders
- Internal anchor links (`#css-variable-injection`) would resolve on GitHub

- [ ] **Step 3: Commit**

```bash
git add ARCHITECTURE.md
git commit -m "docs: add ARCHITECTURE.md developer reference"
```

---

## Task 3: Comments — db/ files

**Files:**
- Modify: `src/db/connection.py`
- Modify: `src/db/schema.py`
- Modify: `src/db/migrations.py`

- [ ] **Step 1: Update `src/db/connection.py`**

Replace the entire file with:

```python
"""Database connection factory.

open_db() is used by the scheduler (persistent connection held for the process lifetime).
get_db() is a FastAPI dependency that opens and closes a connection per request.
"""
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite

from src.config import settings


async def open_db(path: str | None = None) -> aiosqlite.Connection:
    """Open an aiosqlite connection with WAL mode and foreign keys enabled.

    Creates the parent directory if it does not yet exist so the container
    can start cleanly even when the data volume is empty.
    """
    path_str = path or settings.db_path
    Path(path_str).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(path_str)
    # Row objects behave like dicts — access columns by name throughout the codebase.
    db.row_factory = aiosqlite.Row
    # WAL allows concurrent readers while the scheduler is writing.
    await db.execute("PRAGMA journal_mode=WAL")
    # Enforce ON DELETE CASCADE on the items → feeds foreign key.
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """FastAPI dependency: yield a short-lived connection, close on request teardown."""
    db = await open_db()
    try:
        yield db
    finally:
        await db.close()
```

- [ ] **Step 2: Update `src/db/schema.py`**

Replace the entire file with:

```python
"""Initial database schema.

All statements use IF NOT EXISTS so this module is safe to call on every
startup without checking whether the schema already exists.
"""
import aiosqlite

# feeds stores one row per RSS feed URL found in the OPML file.
# id is sha256(url) so it is stable across restarts without a sequence counter.
_CREATE_FEEDS = """
CREATE TABLE IF NOT EXISTS feeds (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL UNIQUE,
    title           TEXT,
    last_fetched_at TIMESTAMP,
    created_at      TIMESTAMP DEFAULT (datetime('now'))
)
"""

# items stores every media entry extracted from feed content.
# ON DELETE CASCADE means removing a feed automatically removes all its items.
# The (feed_id, guid) unique constraint is the deduplication key used by INSERT OR IGNORE.
_CREATE_ITEMS = """
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,
    feed_id     TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid        TEXT NOT NULL,
    title       TEXT,
    media_url   TEXT NOT NULL,
    media_type  TEXT NOT NULL,              -- 'image' | 'gif' | 'video'
    pub_date    TIMESTAMP,
    fetched_at  TIMESTAMP DEFAULT (datetime('now')),
    seen_at     TIMESTAMP,                  -- NULL = unseen
    UNIQUE(feed_id, guid)
)
"""

# Indexes to support the common query patterns: filter by feed, sort by date,
# filter unseen, and prune by fetched_at.
_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_items_feed_id  ON items(feed_id)",
    "CREATE INDEX IF NOT EXISTS idx_items_pub_date ON items(pub_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_items_seen_at  ON items(seen_at)",
]


async def create_schema(db: aiosqlite.Connection) -> None:
    """Create tables and indexes if they do not already exist."""
    await db.execute(_CREATE_FEEDS)
    await db.execute(_CREATE_ITEMS)
    for sql in _CREATE_INDEXES:
        await db.execute(sql)
    await db.commit()
```

- [ ] **Step 3: Update `src/db/migrations.py`**

Replace the entire file with:

```python
"""Integer-versioned schema migrations.

MIGRATIONS is an ordered list of SQL statements. PRAGMA user_version stores
the count of applied migrations. On every startup, any statements from
MIGRATIONS[current_version:] are applied in sequence, with user_version
incremented after each one.

To add a migration: append one SQL string to MIGRATIONS. Never edit or
reorder existing entries — doing so would corrupt the version counter.
"""
import aiosqlite

MIGRATIONS: list[str] = [
    # v1: index on fetched_at to support age-based pruning queries
    "CREATE INDEX IF NOT EXISTS idx_items_fetched_at ON items(fetched_at)",
]


async def run_migrations(db: aiosqlite.Connection) -> None:
    """Apply any pending migrations and advance the version counter."""
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    current_version: int = row[0]

    pending = MIGRATIONS[current_version:]
    if not pending:
        return

    for i, sql in enumerate(pending, start=current_version + 1):
        await db.execute(sql)
        # Commit version update immediately so a crash mid-migration leaves a
        # consistent state — partially applied migrations are not retried.
        await db.execute(f"PRAGMA user_version = {i}")
        await db.commit()
```

- [ ] **Step 4: Verify no ruff errors**

```bash
uv run ruff check src/db/
```

Expected: no output (clean).

- [ ] **Step 5: Commit**

```bash
git add src/db/connection.py src/db/schema.py src/db/migrations.py
git commit -m "docs: add module docstrings and inline comments to db/ package"
```

---

## Task 4: Comments — feeds/ files

**Files:**
- Modify: `src/feeds/opml.py`
- Modify: `src/feeds/fetcher.py`
- Modify: `src/feeds/sync.py`

- [ ] **Step 1: Update `src/feeds/opml.py`**

Replace the entire file with:

```python
"""OPML feed list parser.

Reads the OPML file at the configured path and returns a flat list of
{url, title} dicts. Only entries with a non-empty URL are included.
The title falls back to the URL when the OPML entry has no title attribute.
"""
import logging

import listparser

logger = logging.getLogger(__name__)


def parse_opml(path: str) -> list[dict[str, str]]:
    """Parse an OPML file and return a list of feed descriptors.

    Returns an empty list if the file exists but contains no feed entries.
    Raises FileNotFoundError if the path does not exist.
    """
    with open(path, encoding="utf-8") as f:
        result = listparser.parse(f.read())
    logger.debug(f"Parsed OPML file {path} with {len(result.feeds)} feeds")

    return [
        {"url": feed.url, "title": feed.title or feed.url}
        for feed in result.feeds
        if feed.url  # skip entries with no URL (e.g. category folders)
    ]
```

- [ ] **Step 2: Update `src/feeds/fetcher.py`**

Replace the entire file with:

```python
"""RSS feed fetcher.

Fetches a single feed URL over HTTP, parses it with feedparser, and
returns a list of item dicts ready to INSERT into the database.
Items without a detectable media URL are silently skipped.
"""
import hashlib
import logging

import feedparser
import httpx

from src.media.detector import detect_media

logger = logging.getLogger(__name__)


def _feed_id(url: str) -> str:
    """Stable, collision-resistant ID derived from the feed URL."""
    return hashlib.sha256(url.encode()).hexdigest()


def _item_id(feed_id: str, guid: str) -> str:
    """Stable item ID derived from the feed ID and the entry's GUID."""
    return hashlib.sha256((feed_id + guid).encode()).hexdigest()


async def fetch_feed(url: str, client: httpx.AsyncClient) -> list[dict]:
    """Fetch and parse one RSS feed; return media items as a list of dicts.

    Each dict matches the columns of the items table.
    Entries without a recognisable media URL are excluded.
    """
    response = await client.get(url, follow_redirects=True, timeout=30)
    logger.debug(f"Fetched feed {url} with status code {response.status_code}")

    feed = feedparser.parse(response.text)
    feed_id = _feed_id(url)

    items = []
    for entry in feed.entries:
        result = detect_media(entry)
        if result is None:
            logger.debug(f"No media detected in entry {entry.get('title')}")
            continue

        media_url, media_type = result
        logger.debug(f"Detected media in entry {entry.get('title')}: {media_url} ({media_type})")

        # Use entry.id as the canonical GUID; fall back to link, then media URL.
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

- [ ] **Step 3: Update `src/feeds/sync.py`**

Replace the entire file with:

```python
"""Feed synchronisation: OPML sync and per-feed item refresh.

opml_sync()         — reconcile the feeds table against the OPML file
refresh_all_feeds() — fetch new items for every known feed, then prune
prune_items()       — enforce KEEP_ITEMS and ITEMS_MAX_AGE_HOURS limits
"""
import logging

import aiosqlite
import httpx

from src.config import settings
from src.feeds.fetcher import _feed_id, fetch_feed
from src.feeds.opml import parse_opml

logger = logging.getLogger(__name__)


async def opml_sync(
    db: aiosqlite.Connection, opml_path: str, client: httpx.AsyncClient
) -> None:
    """Reconcile the feeds table with the current OPML file.

    New feeds are inserted; feeds no longer in the file are deleted.
    Deletion cascades automatically to the items table via the FK constraint.
    The HTTP client is accepted as a parameter but not used here — it is
    forwarded to allow callers to trigger an immediate fetch after sync if needed.
    """
    feeds = parse_opml(opml_path)
    logger.debug(f"Syncing {len(feeds)} feeds from OPML file {opml_path}")

    feed_ids = []
    for feed in feeds:
        fid = _feed_id(feed["url"])
        feed_ids.append(fid)
        logger.debug(f"Storing feed {feed['title']} with URL {feed['url']} and ID {fid}")

        # INSERT OR IGNORE preserves existing rows (title, last_fetched_at, etc.)
        await db.execute(
            "INSERT OR IGNORE INTO feeds (id, url, title) VALUES (?, ?, ?)",
            (fid, feed["url"], feed["title"]),
        )

    # Delete feeds whose IDs are not in the current OPML set.
    if feed_ids:
        placeholders = ",".join("?" * len(feed_ids))
        await db.execute(
            f"DELETE FROM feeds WHERE id NOT IN ({placeholders})", feed_ids
        )
    else:
        # OPML is empty — remove everything.
        await db.execute("DELETE FROM feeds")

    await db.commit()


async def _refresh_feed(
    db: aiosqlite.Connection,
    feed_id: str,
    url: str,
    client: httpx.AsyncClient,
) -> None:
    """Fetch new items for one feed and write them to the database.

    INSERT OR IGNORE on (feed_id, guid) silently skips items that are
    already in the database, so this function is safe to call repeatedly.
    """
    items = await fetch_feed(url, client)
    for item in items:
        logger.debug(f"Storing item {item['title']} with media URL {item['media_url']} and ID {item['id']}")

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


async def prune_items(db: aiosqlite.Connection) -> None:
    """Enforce item retention limits.

    Two-phase strategy:
    1. Delete seen items older than ITEMS_MAX_AGE_HOURS (unseen items are never aged out).
    2. If the total count still exceeds KEEP_ITEMS, delete oldest seen items first,
       then oldest unseen as a last resort.
    """
    # Phase 1: age-based eviction (seen items only)
    logger.debug(f"Pruning items older than {settings.items_max_age_hours} hours")
    await db.execute(
        "DELETE FROM items WHERE seen_at IS NOT NULL "
        "AND fetched_at < datetime('now', ? || ' hours')",
        (f"-{settings.items_max_age_hours}",),
    )

    # Phase 2: count-based eviction
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        row = await cur.fetchone()
    total: int = row[0]
    logger.debug(f"Total items after age pruning: {total}")

    if total <= settings.keep_items:
        await db.commit()
        return

    excess = total - settings.keep_items

    # Prefer deleting seen items over unseen ones.
    async with db.execute("SELECT COUNT(*) FROM items WHERE seen_at IS NOT NULL") as cur:
        row = await cur.fetchone()
    seen_count: int = row[0]

    to_delete_seen = min(excess, seen_count)
    logger.debug(f"Pruning {to_delete_seen} seen items to reduce total to {settings.keep_items}")

    if to_delete_seen > 0:
        await db.execute(
            "DELETE FROM items WHERE id IN "
            "(SELECT id FROM items WHERE seen_at IS NOT NULL "
            " ORDER BY fetched_at ASC LIMIT ?)",
            (to_delete_seen,),
        )
        excess -= to_delete_seen

    # Last resort: delete the oldest unseen items.
    if excess > 0:
        logger.debug(f"Pruning {excess} unseen items to reduce total to {settings.keep_items}")
        await db.execute(
            "DELETE FROM items WHERE id IN "
            "(SELECT id FROM items WHERE seen_at IS NULL "
            " ORDER BY fetched_at ASC LIMIT ?)",
            (excess,),
        )

    await db.commit()


async def refresh_all_feeds(
    db: aiosqlite.Connection, client: httpx.AsyncClient
) -> None:
    """Refresh every feed in the database and then prune old items."""
    logger.debug("Refreshing all feeds")
    async with db.execute("SELECT id, url FROM feeds") as cur:
        feeds = await cur.fetchall()
    for feed in feeds:
        await _refresh_feed(db, feed["id"], feed["url"], client)
    # Prune after all feeds are refreshed so the count limit accounts for the
    # full batch of new items rather than enforcing it feed-by-feed.
    await prune_items(db)
```

- [ ] **Step 4: Verify no ruff errors**

```bash
uv run ruff check src/feeds/
```

Expected: no output (clean).

- [ ] **Step 5: Commit**

```bash
git add src/feeds/opml.py src/feeds/fetcher.py src/feeds/sync.py
git commit -m "docs: add module docstrings and inline comments to feeds/ package"
```

---

## Task 5: Comments — media/ files

**Files:**
- Modify: `src/media/detector.py`
- Modify: `src/media/cache.py`
- Modify: `src/media/prefetch.py`

- [ ] **Step 1: Update `src/media/detector.py`**

Replace the entire file with:

```python
"""Media type detection for RSS feed entries.

detect_media() probes four locations in a feedparser entry dict, in order
of reliability: enclosures, media:content, media:thumbnail, og:image in
the entry HTML summary. The first match wins.

Media type is determined by file extension only at ingest time. GIF vs image
is distinguished by extension; the proxy can confirm via Content-Type later.
"""
import logging
from html.parser import HTMLParser
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)

# Supported extensions per media type. Query strings are stripped before matching.
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".svg"}
_GIF_EXTS = {".gif"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi"}


def detect_type(url: str) -> str | None:
    """Return 'image', 'gif', or 'video' based on the URL file extension.

    Returns None if the extension is not in any of the supported sets,
    which causes the entry to be skipped at ingest time.
    """
    # Strip query string before extracting the suffix so ?v=1 doesn't hide .mp4
    suffix = PurePosixPath(url.split("?")[0]).suffix.lower()
    if suffix in _GIF_EXTS:
        return "gif"
    if suffix in _IMAGE_EXTS:
        return "image"
    if suffix in _VIDEO_EXTS:
        return "video"
    return None


class _OGParser(HTMLParser):
    """Minimal HTML parser that extracts the og:image meta content attribute."""

    def __init__(self) -> None:
        super().__init__()
        self.og_image: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            attr_dict = dict(attrs)
            if attr_dict.get("property") == "og:image":
                self.og_image = attr_dict.get("content")


def _extract_og_image(html: str) -> str | None:
    """Return the og:image URL from an HTML snippet, or None if absent."""
    parser = _OGParser()
    parser.feed(html)
    return parser.og_image


def detect_media(entry: dict) -> tuple[str, str] | None:
    """Return (media_url, media_type) for the first detectable media in an entry.

    Probe order (first match wins):
    1. entry.enclosures  — standard RSS enclosure
    2. entry.media_content  — media:content namespace
    3. entry.media_thumbnail  — media:thumbnail namespace
    4. og:image in entry.summary HTML

    Returns None if no media is found or no URL has a supported extension.
    """
    for enc in entry.get("enclosures", []):
        url = enc.get("url", "")
        media_type = detect_type(url)
        logger.debug(f"Checking enclosure URL {url} with detected media type {media_type}")
        if url and media_type:
            return url, media_type

    for mc in entry.get("media_content", []):
        url = mc.get("url", "")
        media_type = detect_type(url)
        logger.debug(f"Checking media_content URL {url} with detected media type {media_type}")
        if url and media_type:
            return url, media_type

    for mt in entry.get("media_thumbnail", []):
        url = mt.get("url", "")
        media_type = detect_type(url)
        logger.debug(f"Checking media_thumbnail URL {url} with detected media type {media_type}")
        if url and media_type:
            return url, media_type

    # Last resort: scrape og:image from the entry's HTML summary field.
    summary = entry.get("summary", "")
    if summary:
        og_url = _extract_og_image(summary)
        if og_url:
            media_type = detect_type(og_url)
            logger.debug(f"Checking og:image URL {og_url} with detected media type {media_type}")
            if media_type:
                return og_url, media_type

    return None
```

- [ ] **Step 2: Update `src/media/cache.py`**

Replace the entire file with:

```python
"""Filesystem media cache.

Files are stored as {CACHE_DIR}/{sha256(url)} — flat directory, no extension.
The sha256 filename makes lookup O(1) and handles any characters in the URL.

evict() is called after every feed refresh cycle. It removes files that are
too old first, then trims by count from the oldest end if the directory is
still over the limit.
"""
import asyncio
import hashlib
import logging
import time
from pathlib import Path

from src.config import settings

logger = logging.getLogger(__name__)


def _cache_path(url: str) -> Path:
    """Return the filesystem path for a cached URL (does not check existence)."""
    return Path(settings.cache_dir) / hashlib.sha256(url.encode()).hexdigest()


async def cache_write(url: str, data: bytes) -> Path:
    """Write media bytes to the cache and return the path.

    The write is performed off the event loop via asyncio.to_thread to
    avoid blocking the async executor on large file writes.
    """
    path = _cache_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, data)
    return path


def cache_read(url: str) -> Path | None:
    """Return the cached path for a URL, or None on a cache miss."""
    path = _cache_path(url)
    return path if path.exists() else None


async def evict() -> None:
    """Evict stale or excess cache entries.

    Step 1: delete files older than CACHE_MAX_AGE_HOURS.
    Step 2: if the surviving count still exceeds CACHE_MAX_ITEMS,
            delete the oldest files (by mtime) until under the limit.
    """
    cache_dir = Path(settings.cache_dir)
    if not cache_dir.exists():  # noqa: ASYNC240
        return
    now = time.time()
    max_age_secs = settings.cache_max_age_hours * 3600

    # Sort by mtime ascending so the oldest files are at index 0.
    files = sorted(cache_dir.iterdir(), key=lambda p: p.stat().st_mtime)  # noqa: ASYNC240
    surviving: list[Path] = []
    for f in files:
        if now - f.stat().st_mtime > max_age_secs:
            logger.debug(f"Evicting cache file {f} due to age")
            f.unlink(missing_ok=True)
        else:
            surviving.append(f)

    # Pop from the front (oldest) until under the count limit.
    while len(surviving) > settings.cache_max_items:
        logger.debug(f"Evicting cache file {surviving[0]} due to count limit")
        surviving.pop(0).unlink(missing_ok=True)
```

- [ ] **Step 3: Update `src/media/prefetch.py`**

Replace the entire file with:

```python
"""Background media pre-fetching.

Two entry points:

warm_startup_cache() — called once at startup; warms the cache with the most
    recent CACHE_MAX_ITEMS items. Uses a semaphore of 10 and a 100 ms stagger
    to avoid a burst of concurrent requests against upstream servers.

prefetch_ahead() — called from the /api/prefetch/hint endpoint; warms the
    next PREFETCH_AHEAD items older than the given item's pub_date. Intended
    to be fired as a background task ahead of the user's scroll position.
"""
import asyncio
import logging

import aiosqlite
import httpx

from src.config import settings
from src.media.cache import cache_read, cache_write

logger = logging.getLogger(__name__)


async def _warm(url: str, client: httpx.AsyncClient) -> None:
    """Fetch and cache one URL if it is not already cached. Silent on errors."""
    if cache_read(url) is not None:
        return  # already cached — nothing to do
    try:
        response = await client.get(url, follow_redirects=True, timeout=30)
        if response.is_success:
            await cache_write(url, await response.aread())
    except Exception as exc:  # pragma: no cover
        logger.debug("prefetch failed for %s: %s", url, exc)


async def warm_startup_cache(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    """Pre-warm the cache with the most recently published items.

    Runs as an asyncio background task (fire-and-forget from the lifespan hook).
    A semaphore of 10 and a 100 ms stagger between task creation prevents a
    thundering-herd of concurrent HTTP requests at container start.
    """
    async with db.execute(
        "SELECT media_url FROM items ORDER BY pub_date DESC LIMIT ?",
        (settings.cache_max_items,),
    ) as cur:
        rows = await cur.fetchall()

    sem = asyncio.Semaphore(10)

    async def _bounded_warm(url: str) -> None:
        async with sem:
            await _warm(url, client)

    for row in rows:
        asyncio.create_task(_bounded_warm(row["media_url"]))
        # Small sleep between task creation to spread the initial burst.
        await asyncio.sleep(0.1)


async def prefetch_ahead(
    item_id: str, db: aiosqlite.Connection, client: httpx.AsyncClient
) -> None:
    """Fire background warm tasks for the next PREFETCH_AHEAD items after item_id.

    Queries items with a pub_date strictly less than the given item's pub_date
    (i.e. items that come *after* it in reverse-chronological display order).
    Each warm task runs independently; errors are silently ignored.
    """
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

- [ ] **Step 4: Verify no ruff errors**

```bash
uv run ruff check src/media/
```

Expected: no output (clean).

- [ ] **Step 5: Commit**

```bash
git add src/media/detector.py src/media/cache.py src/media/prefetch.py
git commit -m "docs: add module docstrings and inline comments to media/ package"
```

---

## Task 6: Comments — api/ + top-level files

**Files:**
- Modify: `src/config.py`
- Modify: `src/main.py`
- Modify: `src/scheduler.py`
- Modify: `src/api/feeds.py`
- Modify: `src/api/items.py`
- Modify: `src/api/media.py`

- [ ] **Step 1: Update `src/config.py`**

Replace the entire file with:

```python
"""Application configuration.

All settings are read from environment variables (uppercase names).
pydantic-settings handles the env-var binding automatically — no .env
file parsing occurs at this level; that is handled by Docker / the shell.

The settings singleton is imported directly by modules that need config
values at call time. Frontend-visible values are injected into the HTML
as CSS custom properties by main._build_html().
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Paths ---
    opml_path: str = "/data/feeds.opml"        # OPML file inside the container
    db_path: str = "/data/db/reader.db"         # SQLite database file

    # --- Feed refresh schedule ---
    opml_sync_interval: int = 3600              # seconds between OPML re-reads
    feed_refresh_interval: int = 900            # seconds between feed refresh cycles

    # --- Media cache ---
    cache_dir: str = "/cache"
    cache_max_items: int = 500                  # max files on disk
    cache_max_age_hours: int = 48               # evict files older than this

    # --- Item retention ---
    prefetch_ahead: int = 5                     # items to pre-warm ahead of scroll
    keep_items: int = 1000                      # max rows in the items table
    items_max_age_hours: int = 168              # delete seen items older than 7 days

    # --- Frontend behaviour (injected as CSS variables at startup) ---
    image_display_delay_ms: int = 5000          # dwell time per image/GIF in auto-scroll (ms)
    slideshow_transition_ms: int = 400          # CSS crossfade duration (ms)
    auto_scroll_speed: float = 1.5             # px per animation frame (~90px/s at 60fps)

    # --- Server ---
    port: int = 8080
    log_level: str = "info"                     # uvicorn log level


settings = Settings()
```

- [ ] **Step 2: Update `src/main.py`**

Replace the entire file with:

```python
"""FastAPI application entry point.

The lifespan context manager runs startup and shutdown logic:
- Builds the HTML string with injected CSS variables (once, at startup)
- Opens the persistent database connection used by the scheduler
- Applies schema and pending migrations
- Starts the background scheduler (which fires an immediate OPML sync + feed refresh)
- On shutdown: stops the scheduler and closes the database connection

The index route serves the pre-built HTML string from app.state to avoid
re-reading the file on every request.
"""
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from src.api import feeds, items, media
from src.config import settings
from src.db.connection import open_db
from src.db.migrations import run_migrations
from src.db.schema import create_schema
from src.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=settings.log_level.upper())
# Suppress noisy third-party loggers that produce per-request output at INFO.
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_static_dir = Path(__file__).parent / "static"
_index_path = _static_dir / "index.html"


def _build_html() -> str:
    """Read index.html and inject backend config values as CSS custom properties.

    The <!-- SLIDESHOW_TRANSITION --> comment is replaced with a <style> block
    so the frontend can read these values synchronously via getComputedStyle()
    without a separate API call. The result is cached for the process lifetime.
    """
    if not _index_path.exists():
        return ""
    style = (
        f"<style>:root{{"
        f"--slideshow-transition-ms:{settings.slideshow_transition_ms}ms;"
        f"--image-display-delay-ms:{settings.image_display_delay_ms}ms;"
        f"--prefetch-ahead:{settings.prefetch_ahead};"
        f"--auto-scroll-speed:{settings.auto_scroll_speed}"
        f"}}</style>"
    )
    return _index_path.read_text().replace("<!-- SLIDESHOW_TRANSITION -->", style)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Build HTML once; store on app.state for the index route to serve.
    app.state.html = _build_html()
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
async def index(request: Request) -> str:
    """Serve the pre-built single-page app shell."""
    return request.app.state.html
```

- [ ] **Step 3: Update `src/scheduler.py`**

Replace the entire file with:

```python
"""APScheduler setup and shared HTTP client.

_State holds the scheduler and the httpx.AsyncClient as module-level
singletons. The client is shared across the scheduler jobs and API
handlers to reuse connection pools.

Both jobs fire immediately on startup (before the first scheduled
interval) so the reader is populated on first boot without waiting.
Startup failures are logged as warnings — the scheduler retries on
the next interval.
"""
import asyncio
import datetime
import logging

import aiosqlite
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.config import settings
from src.feeds.sync import opml_sync, refresh_all_feeds
from src.media.prefetch import warm_startup_cache

logger = logging.getLogger(__name__)


class _State:
    """Module-level singleton holding runtime objects that outlive a single request."""
    scheduler: AsyncIOScheduler | None = None
    client: httpx.AsyncClient | None = None
    last_opml_sync: datetime.datetime | None = None


_state = _State()


def get_http_client() -> httpx.AsyncClient:
    """Return the shared HTTP client. Raises if called before start_scheduler()."""
    if _state.client is None:
        raise RuntimeError("HTTP client not initialised — call start_scheduler first")
    return _state.client


def get_last_opml_sync() -> datetime.datetime | None:
    """Return the UTC timestamp of the most recent successful OPML sync, or None."""
    return _state.last_opml_sync


async def _opml_sync_job(db: aiosqlite.Connection, opml_path: str, client: httpx.AsyncClient) -> None:
    """Wrapper around opml_sync that updates the last-sync timestamp on success."""
    await opml_sync(db, opml_path, client)
    _state.last_opml_sync = datetime.datetime.now(datetime.UTC)


async def start_scheduler(db: aiosqlite.Connection) -> None:
    """Create the HTTP client, register scheduler jobs, and fire an initial sync.

    Job registration uses string IDs so APScheduler can de-duplicate if
    start_scheduler is somehow called twice in a test environment.
    """
    _state.client = httpx.AsyncClient()
    _state.scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop())

    # Job 1: re-read the OPML file and reconcile the feeds table.
    _state.scheduler.add_job(
        _opml_sync_job,
        "interval",
        seconds=settings.opml_sync_interval,
        args=[db, settings.opml_path, _state.client],
        id="opml_sync",
    )
    # Job 2: fetch new items for every feed and prune old ones.
    _state.scheduler.add_job(
        refresh_all_feeds,
        "interval",
        seconds=settings.feed_refresh_interval,
        args=[db, _state.client],
        id="refresh_feeds",
    )
    _state.scheduler.start()

    # Eager startup: run both jobs immediately so the reader is populated
    # on first boot without waiting for the first scheduled interval.
    try:
        await opml_sync(db, settings.opml_path, _state.client)
        _state.last_opml_sync = datetime.datetime.now(datetime.UTC)
    except Exception as exc:
        logger.warning("Initial OPML sync failed (will retry on schedule): %s", exc)
    try:
        await refresh_all_feeds(db, _state.client)
    except Exception as exc:
        logger.warning("Initial feed refresh failed (will retry on schedule): %s", exc)

    # Startup cache warmup runs as a background task — does not block the server
    # from accepting requests while it downloads media files.
    asyncio.create_task(warm_startup_cache(db, _state.client))


async def stop_scheduler() -> None:
    """Shut down the scheduler and close the HTTP client cleanly."""
    if _state.scheduler and _state.scheduler.running:
        _state.scheduler.shutdown(wait=False)
        _state.scheduler = None
    if _state.client:
        await _state.client.aclose()
        _state.client = None
```

- [ ] **Step 4: Update `src/api/feeds.py`**

Replace the entire file with:

```python
"""GET /api/feeds — list all feeds with item counts."""
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Depends

from src.db.connection import get_db

router = APIRouter()


@router.get("/feeds")
async def list_feeds(db: Annotated[aiosqlite.Connection, Depends(get_db)]) -> list[dict[str, Any]]:
    """Return all feeds with total and unseen item counts.

    The LEFT JOIN + conditional COUNT gives both counts in one query,
    avoiding a second round-trip per feed.
    """
    async with db.execute(
        """SELECT f.id, f.title, f.url, f.last_fetched_at,
                  COUNT(i.id)                                  AS item_count,
                  COUNT(CASE WHEN i.seen_at IS NULL THEN i.id END) AS unseen_count
           FROM feeds f
           LEFT JOIN items i ON i.feed_id = f.id
           GROUP BY f.id"""
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]
```

- [ ] **Step 5: Update `src/api/items.py`**

Replace the entire file with:

```python
"""GET /api/items and POST /api/items/{id}/seen."""
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from src.db.connection import get_db

router = APIRouter()

_DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]


@router.get("/items")
async def list_items(
    unseen: bool = False,
    feed_id: str | None = None,
    page: int = 0,
    size: int = 50,
    db: _DbDep = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """Return a paginated, interleaved list of media items.

    The window-function query assigns a rank (rn) per feed ordered by
    pub_date ASC, then sorts globally by rn then feed_id. This interleaves
    feeds evenly: all feeds contribute their oldest unseen item before any
    feed contributes its second item, preventing one prolific feed from
    dominating the top of the page.
    """
    conditions: list[str] = []
    params: list[Any] = []

    if unseen:
        conditions.append("seen_at IS NULL")
    if feed_id is not None:
        conditions.append("feed_id = ?")
        params.append(feed_id)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.extend([size, page * size])

    query = f"""
        WITH ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (PARTITION BY feed_id ORDER BY pub_date ASC) AS rn
            FROM items
            {where_clause}
        )
        SELECT id, feed_id, title, media_url, media_type, pub_date, fetched_at, seen_at
        FROM ranked
        ORDER BY rn ASC, feed_id ASC
        LIMIT ? OFFSET ?
    """
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


@router.post("/items/{item_id}/seen")
async def mark_seen(
    item_id: str,
    db: _DbDep = None,  # type: ignore[assignment]
) -> dict[str, str]:
    """Mark an item as seen and return the timestamp.

    The browser stores the returned seen_at value on the item object to
    prevent a second POST for the same item during the session.
    """
    await db.execute(
        "UPDATE items SET seen_at = datetime('now') WHERE id = ?",
        (item_id,),
    )
    await db.commit()

    async with db.execute("SELECT seen_at FROM items WHERE id = ?", (item_id,)) as cur:
        row = await cur.fetchone()

    if row is None or row[0] is None:
        raise HTTPException(status_code=404, detail="Not found")

    return {"seen_at": row[0]}
```

- [ ] **Step 6: Update `src/api/media.py`**

Replace the entire file with:

```python
"""Media proxy, prefetch hint, and status endpoints."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from src.config import settings
from src.db.connection import get_db
from src.media.cache import cache_read, cache_write
from src.media.prefetch import prefetch_ahead
from src.scheduler import get_http_client, get_last_opml_sync

router = APIRouter()

_DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]


@router.get("/media/proxy", response_model=None)
async def proxy_media(url: str = Query(...)) -> FileResponse | StreamingResponse:
    """Cache-through proxy for media files.

    On a cache hit: serve the file directly via FileResponse (zero-copy sendfile).
    On a cache miss: fetch from upstream, write to cache, stream to the browser.
    The full response body is read once (aread) to write to cache — it is not
    buffered a second time for the stream.
    """
    path = cache_read(url)
    if path is not None:
        return FileResponse(str(path))

    client = get_http_client()
    try:
        response = await client.get(url, follow_redirects=True, timeout=30)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="upstream fetch failed") from exc
    if not response.is_success:
        raise HTTPException(status_code=502, detail="upstream error")

    data = await response.aread()
    await cache_write(url, data)
    content_type = response.headers.get("content-type", "application/octet-stream")

    async def _stream() -> bytes:
        yield data

    return StreamingResponse(_stream(), media_type=content_type)


@router.post("/prefetch/hint")
async def prefetch_hint(
    body: dict[str, str],
    db: _DbDep = None,  # type: ignore[assignment]
) -> dict[str, str]:
    """Trigger background pre-fetching of items ahead of the given item.

    The browser calls this as a fire-and-forget POST whenever it loads a
    new page of items. The hint launches asyncio background tasks; the
    response returns immediately.
    """
    item_id = body.get("item_id", "")
    if not item_id:
        raise HTTPException(status_code=422, detail="item_id required")
    client = get_http_client()
    await prefetch_ahead(item_id, db, client)
    return {"status": "ok"}


@router.get("/status")
async def get_status(
    db: _DbDep = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Return a health/status snapshot: feed count, item counts, cache size, last sync."""
    async with db.execute("SELECT COUNT(*) FROM feeds") as cur:
        feeds_count: int = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM items") as cur:
        items_total: int = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM items WHERE seen_at IS NULL") as cur:
        items_unseen: int = (await cur.fetchone())[0]

    cache_dir = Path(settings.cache_dir)
    cache_size_mb = 0.0
    if cache_dir.exists():  # noqa: ASYNC240
        cache_size_mb = sum(f.stat().st_size for f in cache_dir.iterdir() if f.is_file()) / (1024 * 1024)  # noqa: ASYNC240

    last_sync = get_last_opml_sync()
    return {
        "feeds": feeds_count,
        "items_total": items_total,
        "items_unseen": items_unseen,
        "cache_size_mb": round(cache_size_mb, 2),
        "last_opml_sync": last_sync.isoformat() if last_sync else None,
    }
```

- [ ] **Step 7: Verify no ruff errors**

```bash
uv run ruff check src/
```

Expected: no output (clean).

- [ ] **Step 8: Run tests to confirm no regressions**

```bash
uv run pytest
```

Expected: all tests pass, coverage ≥ 90 %.

- [ ] **Step 9: Commit**

```bash
git add src/config.py src/main.py src/scheduler.py src/api/feeds.py src/api/items.py src/api/media.py
git commit -m "docs: add module docstrings and inline comments to api/ and top-level modules"
```

---

## Task 7: Comments — app.js

**Files:**
- Modify: `src/static/app.js`

- [ ] **Step 1: Add comments inside the complex functions in app.js**

Apply the following targeted edits (do not rewrite the whole file — only add the comment lines shown):

**Inside `getGifDuration` — explain the byte-scan loop:**

Find the line:
```js
  for (let i = 0; i + 5 < buf.length; i++) {
```

Replace with:
```js
  // Scan for GIF Graphic Control Extension blocks (0x21 0xF9 0x04).
  // Each block stores a frame delay in 1/100 s as a little-endian uint16
  // at bytes [i+4, i+5]. Sum all frame delays to get the total loop duration.
  for (let i = 0; i + 5 < buf.length; i++) {
```

**Inside `fetchItems` — explain the stale-generation guard:**

Find the line:
```js
    if (gen !== fetchGeneration) return;  // stale response, discard
```

Replace with:
```js
    // fetchGeneration is incremented when the view resets (e.g. showSeen toggle).
    // If the generation changed while this fetch was in-flight, discard the result
    // to avoid appending items from the old query into a fresh list.
    if (gen !== fetchGeneration) return;
```

**Inside `seenObserver` callback — explain the exit-via-top detection:**

Find the line:
```js
    if (entry.isIntersecting || entry.boundingClientRect.bottom > (entry.rootBounds?.top ?? 0)) return;
```

Replace with:
```js
    // Fire only when the item has fully exited through the *top* of the viewport
    // (i.e. the user has scrolled past it). isIntersecting covers the visible case;
    // the boundingClientRect check filters out items that exit through the bottom.
    if (entry.isIntersecting || entry.boundingClientRect.bottom > (entry.rootBounds?.top ?? 0)) return;
```

**Inside `mediaObserver` — explain the pause/resume auto-scroll block:**

Find the line:
```js
      if (topReached && autoScroll && !autoScrollPaused
```

Add the following comment on the line immediately above it:
```js
      // When the top edge of a video or GIF reaches the viewport top, pause
      // auto-scroll so the media can play for its full duration before the
      // feed continues scrolling. scrollPausedHere guards against re-entry;
      // scrollWaited prevents the same element from pausing scroll a second time.
```

**Inside `showSlide` — explain the A/B layer swap:**

Find the line:
```js
  // Clear incoming layer and populate with new media
```

Add a comment block before `const incoming = ...`:
```js
  // Slideshow uses two absolutely-positioned layers (A and B). On each advance,
  // the inactive layer is populated with the new item and given the 'active' class,
  // which triggers the CSS opacity transition. The previously active layer loses
  // 'active' and fades out. activeSlide tracks which layer is currently visible.
```

- [ ] **Step 2: Verify the file still runs**

Start the dev server:
```bash
uv run uvicorn src.main:app --reload --port 8080
```

Open http://localhost:8080 in a browser. Check:
- Page loads without JS errors in the console
- Items appear and scroll normally
- Slideshow mode (`s`) works with visible crossfade
- Auto-scroll (`a`) starts and stops correctly

Stop the server with Ctrl-C.

- [ ] **Step 3: Commit**

```bash
git add src/static/app.js
git commit -m "docs: add explanatory comments to complex app.js functions"
```

---

## Self-Review

**Spec coverage:**
- README.md with all 10 sections including full cloudflared + Access walkthrough ✓
- ARCHITECTURE.md with all 11 sections ✓
- Comments in all 14 source files ✓

**Placeholder scan:**
- No TBDs, TODOs, or "similar to Task N" patterns
- All code blocks contain complete, copy-pasteable content
- All file paths are exact

**Type consistency:**
- No function renaming between tasks — each task replaces a full file independently
- `_State`, `get_http_client`, `get_last_opml_sync` match their usage in api/media.py ✓
