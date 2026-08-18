# Architecture

Developer reference for media-rss-reader: how *this* codebase is put together,
and why the load-bearing decisions are the way they are.

For the behaviour contract on its own — algorithms, invariants and data model
with no Python in them, written for porting to another platform — see
[spec.md](spec.md). This document assumes it and describes the implementation.

---

## System Overview

Three planes interact at runtime:

```
┌──────────────────────────────────────────────────────────────┐
│  Browser (Vanilla JS — 8 modules, no build step)             │
│  item-store · cache-queue · feed-view · scroll-controller    │
│  autoscroll-controller · controls · zoom-controller · app    │
│  IntersectionObserver × 2  ·  PWA service worker             │
│  galleries (←/→, dots, arrows)  ·  double-tap zoom           │
└──────────────────┬───────────────────────────────────────────┘
                   │  HTTPS  (X-Forwarded-Proto from the proxy)
┌──────────────────▼──────────────────────────────────────────┐
│  RequestIDMiddleware   — correlation id + X-Content-Type-    │
│                          Options: nosniff                    │
│  AuthMiddleware        — HTTPS enforcement, session cookie   │
│    pass-through: /health  /login  /setup  /static/*          │
└──────────────────┬──────────────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────────────┐
│  FastAPI (uvicorn, async)                                    │
│  /health  /  /login  /setup  /logout                         │
│  /api/items          /api/items/{id}/seen                    │
│  /api/media/proxy    /api/media/failed                       │
│  /api/prefetch/hint  /api/reddit-feeds/status                │
└───────────┬─────────────────────┬───────────────────────────┘
            │  aiosqlite          │  filesystem
┌───────────▼──────────┐  ┌───────▼──────────────────────────┐
│  SQLite (WAL)        │  │  CACHE_DIR (sha256-named files   │
│  feeds · items       │  │  + .meta sidecars)               │
│  tombstones · hashes │  │  evict by age, count, bytes      │
└──────────────────────┘  └──────────────────────────────────┘
            ▲
┌───────────┴─────────────────────────────────────────────────┐
│  asyncio background loops (src/scheduler.py)                 │
│  sync_feeds        every OPML_SYNC_INTERVAL s                │
│  refresh_all_feeds every FEED_REFRESH_INTERVAL s             │
│  startup sync + cache warm, fired once at boot               │
└──────────────────────────────────────────────────────────────┘
```

**Connections.** `main.lifespan` opens **two** long-lived connections: one the
request handlers share through `get_db()`, and one the background loops keep to
themselves. Both live for the process lifetime. Writers that have no connection
to borrow — background warm tasks and streaming response bodies, which run after
the route function returned — open their own via `run_with_own_db()`. See
[Connection strategy](#connection-strategy).

**HTTP clients.** Also two: `app.state.http` for the media proxy, the feed
scheduler and the prefetcher, and `app.state.http_status` (capped at 2
connections) for the reddit-feeds poll alone. The status modal polls at 1 Hz with
no in-flight guard, and httpx's read timeout is the gap *between* reads rather
than a whole-request budget, so a trickling companion on the shared pool would
hold slots until every media request failed on pool timeout.

---

## Directory Map

```
src/
├── main.py            FastAPI app, lifespan, HTML config injection, 422 logging
├── config.py          Settings dataclass — all config from env vars, validated at import
├── scheduler.py       The two background loops + the startup sync task
├── http_client.py     The two app-scoped httpx clients, as FastAPI dependencies
├── request_id.py      Per-request correlation id (contextvar + X-Request-ID + nosniff)
├── logging_utils.py   loggable() — escape/bound any string from outside the process
├── timing.py          timer() — millisecond spans for the API boundary log lines
│
├── auth/
│   ├── middleware.py  HTTPS enforcement + session cookie validation
│   ├── routes.py      GET/POST /login, GET/POST /setup, POST /logout (pyotp used directly)
│   ├── session.py     Sign/verify the session and setup cookies (itsdangerous)
│   └── lockout.py     In-process per-IP lockout tracker (monotonic clock)
│
├── db/
│   ├── connection.py  open_db, get_db/DbDep, write_transaction, run_with_own_db
│   ├── schema.py      The frozen v1 shape: feeds, items, indexes
│   ├── migrations.py  Ordered migration list + PRAGMA user_version counter
│   └── queries.py     The ranked-items CTE and cursor fragments, shared by API + prefetch
│
├── feeds/
│   ├── opml.py        Parse an OPML file → [{url, title}] (stdlib ElementTree)
│   ├── fetcher.py     Fetch + parse one feed; entry → item row dict
│   └── sync.py        Feed-list reconciliation, per-feed refresh, insert guard, pruning
│
├── media/
│   ├── detector.py      Media URL + type detection; galleries via detect_all_media()
│   ├── normalize.py     media_key() canonical identity; item_slides() accessor
│   ├── cache.py         Filesystem cache: tee-write, read, in-flight claims, eviction
│   ├── fetch.py         Upstream fetching: SSRF gate, redirects, tee, size caps
│   ├── prefetch.py      Background warms: startup + ahead-of-cursor
│   ├── availability.py  Dead-URL tracking; drop items whose media is entirely gone
│   └── dedup.py         Content dedup: sha256 twins + optional perceptual hash
│
├── api/
│   ├── items.py        GET /api/items, POST /api/items/{id}/seen
│   ├── media.py        GET /api/media/proxy, POST /api/media/failed, POST /api/prefetch/hint
│   └── reddit_feeds.py GET /api/reddit-feeds/status (proxies the companion service)
│
└── static/
    ├── index.html               App shell; <!-- CONFIG_VARS --> injection point
    ├── login.html               Standalone login form (no SPA dependency)
    ├── setup.html               First-time TOTP setup, client-side QR rendering
    ├── qrcode.min.js            Bundled node-qrcode browser build (no CDN)
    ├── manifest.json            PWA manifest
    ├── sw.js                    Service worker — app shell precache
    ├── favicon.svg  icon-192.png  icon-512.png  icon-512-maskable.png
    ├── style.css                Layout + theming via CSS custom properties
    ├── app.js                   Startup, config, keymap, pointer swipe, module wiring
    ├── item-store.js            Item list, keyset cursor, seen + unusable reporting
    ├── media-el.js              Shared <img>/<video> + proxy-URL factory
    ├── feed-view.js             DOM rendering: placeholders, media wraps, galleries
    ├── scroll-controller.js     Two IntersectionObservers; seen beacons; page top-up
    ├── autoscroll-controller.js Per-item dwell timer; image/GIF/video advance
    ├── cache-queue.js           3-worker priority download queue; prefetch hints
    ├── zoom-controller.js       Zoom an image to 1:1, pan it, animate in/out
    └── controls.js              FAB menu, toggles, status modal, UI_DEBUG overlay
```

---

## Startup Sequence

`main.py` uses FastAPI's `lifespan` context manager.

1. **`_build_html()`** — reads `index.html`, replaces `<!-- CONFIG_VARS -->` with a
   `<script>window.MRR_CONFIG = {...}</script>` block carrying the browser-visible
   settings (`feedInitialCount`, `imageAutoscrollDelayS`, `mediaLoadTimeoutS`,
   `zoomTransitionMs`, `uiDebug`), and replaces `{{VERSION}}` in asset URLs with
   `int(time.time())`. The result is cached in `app.state.html` for the process
   lifetime — see [Client Config Injection](#client-config-injection).

2. **`open_db()`** — the request-side connection. Creates parent directories,
   opens with a **30 s busy timeout** (the 5 s default is too tight when many
   `run_with_own_db` writers contend), sets the row factory so rows index by
   column name, and enables `journal_mode=WAL` and `foreign_keys=ON`. None of
   those four is a default.

3. **`create_schema()`** — `CREATE TABLE/INDEX IF NOT EXISTS` for `feeds` and
   `items`. This is the **frozen v1 shape**; every later table and column belongs
   in `migrations.py`.

4. **`run_migrations()`** — applies anything pending (see [Migrations](#migrations)).

5. **A second `open_db()`** for the background loops. sqlite3's implicit
   transaction is per *connection*, not per coroutine, and `sync.py` writes many
   rows before committing, so a shared connection could commit or roll back a
   partial refresh mid-write.

6. **Two `httpx.AsyncClient`s** — media/scheduler, and the capped status client.

7. **`start_scheduler()`** — creates three tasks: the OPML sync loop, the refresh
   loop, and a one-shot startup sync that runs both jobs immediately (so a fresh
   install is populated within seconds) and then fires `warm_startup_cache()` as
   a background task. Startup errors are caught and logged; the loops retry.

**Shutdown** runs in reverse and in a specific order: `stop_scheduler()` cancels
the loops, the tracked startup tasks, and every in-flight prefetch warm — all of
which use the shared client — **before** the caller closes that client, then the
connections.

---

## Background Loops

`scheduler.py` holds no scheduler library: two `while _running: await sleep(...)`
coroutines, plus module-level task sets so shutdown can cancel them. Each loop
body is wrapped in its own try/except so one bad cycle never ends the loop.

**Job 1 — `sync_feeds`** (every `OPML_SYNC_INTERVAL`, default 1 h) reconciles the
*feed list*, from two sources used together:

- `local_xml_sync()` scans `FEEDS_DIR` for `*.xml`. Each file becomes a feed row
  whose `url` is the **bare filename** and whose `id` is `sha256(filename)`. Its
  entries are ingested here, in the same pass. A stored `source_mtime` short-
  circuits an unchanged file: no read, no parse, no detection. The mtime lives on
  the feeds row on purpose — a wiped database or a hard-delete drops it together
  with the items it stands for, so it can never claim "unchanged" while the items
  are gone.
- The OPML file is parsed with stdlib `ElementTree` and each `xmlUrl` inserted if
  absent. An entry whose basename matches a folder file is skipped — the folder
  wins.
- Finally a hard delete: `DELETE FROM feeds WHERE url NOT IN (union)`, cascading
  to items. **Skipped when the union is empty**, because that almost always means
  the sources are unreadable (unmounted volume, companion mid-restart) rather
  than genuinely empty, and the cascade would take every item with it.

Job 1 does **not** fetch remote feed content. That is Job 2.

**Job 2 — `refresh_all_feeds`** (every `FEED_REFRESH_INTERVAL`, default 15 min):

- `SELECT id, url FROM feeds`, skipping rows whose url is not `http(s)` (those
  are the local files, already ingested by Job 1).
- Per feed: `_refresh_feed()` → conditional GET → feedparser → detection → insert.
  A failure is logged and the loop moves on.
- Then `prune_items()` and `evict()` — **always**, regardless of how many feeds
  failed, so retention limits are enforced unconditionally.

---

## Feed Pipeline

```
FEEDS_DIR/*.xml                    OPML file
   │ mtime unchanged? skip            │ ElementTree
   ▼                                  ▼
feedparser.parse(text)          [{url, title}, ...]
                                      │ httpx GET + If-None-Match / If-Modified-Since
                                      ▼  (304 → no parse, no detection, validators kept)
                                feedparser.parse(response.text)
   └──────────────┬───────────────────┘
                  ▼  per entry
        guid = entry.id ?? entry.link
        guid ∈ skip set? ──yes──► drop (BEFORE detection)
                  │ no
                  ▼
        detect_all_media(entry)          ←── detector.py
           ├─ Tier 1: enclosures[] + media:content[]   (RSS order)
           ├─ Tier 2: <img src> in the summary HTML     (only if tier 1 hit)
           └─ Tier 3: media:thumbnail / og:image        (only if tier 1 empty)
                  │  [(url, type), ...] or []
                  ▼
        _insert_item()  → items, or a resolved_guids tombstone
```

**The skip set** (`_skip_guids`) is the union of two "already resolved" sources,
loaded **once per feed**: guids already in `items`, and guids in `resolved_guids`
(the insert guard rejected them, a prune evicted them, or their media went
entirely dead). The check runs before detection because detection is the
expensive part — HTML parsing per entry, per poll, per feed.

**Tier 2 fires only when tier 1 produced at least one slide.** That is what stops
a text feed full of inline thumbnails and tracking pixels being promoted to a
gallery. The summary HTML is **entity-unescaped before parsing**: many feeds emit
`&lt;img src=...&gt;` rather than real tags.

**Media type** comes from the file extension alone, with the query string
stripped. `.svg` is excluded — an active document, not a picture. Unrecognised
extensions are skipped.

**IDs** are SHA-256: `feed_id = sha256(url)`, `item_id = sha256(feed_id + guid)`.
Stable across restarts, no sequence counter, and deduplication falls out of the
`UNIQUE(feed_id, guid)` constraint.

**Dates** are normalised to a sortable `YYYY-MM-DD HH:MM:SS` string. The raw RSS
2.0 `published` string is RFC-822, which a TEXT comparison sorts alphabetically by
weekday name.

### The insert guard

`_INSERT_ITEM` in `sync.py` is an `INSERT ... SELECT ... WHERE NOT EXISTS` rather
than a plain `VALUES`, carrying two guards keyed on `media_key`:

| Guard | Rejects |
|---|---|
| `NOT EXISTS (… FROM items …)` | a picture already stored under **any** feed — one image appearing once per feed that carries it |
| `NOT EXISTS (… FROM seen_media …)` | a picture the user already saw — a pruned item re-inserted from a feed that still lists it |

Both ingest paths share this one statement. That matters: the guard it replaced
lived only in `_refresh_feed`, and since `refresh_all_feeds` skips non-HTTP feed
urls, `FEEDS_DIR` feeds never reached it and their seen posts came back on every
sync.

`_insert_item()` wraps it: when the guard rejects a row (`rowcount == 0`) it
writes a `resolved_guids` tombstone. Without that, the rejection leaves no trace
in `items`, so the entry is re-parsed and re-detected on every single poll for as
long as the feed lists it.

### Pruning

`prune_items()`, two phases, everything routed through `_evict_items()`:

1. **Age.** Delete seen items older than `ITEMS_MAX_AGE_HOURS` (by `fetched_at`);
   delete unseen items older than **4×** that (by `pub_date`). Unseen items get
   the longer budget because the user has not had a chance to see them yet.
2. **Count.** If still over `KEEP_ITEMS`, delete the oldest *seen* items first,
   then the oldest unseen as a last resort.

`_evict_items()` uses `DELETE … RETURNING feed_id, guid` and writes a
`resolved_guids` tombstone for every row it takes. This is not bookkeeping:
`/api/items` serves oldest-first and this evicts oldest-first, so a prune deletes
exactly the rows a reader is currently looking at. Their seen beacons then 404
against the deleted row, `seen_media` never records them, and without the
tombstone they return to the front of the feed on every cycle.

---

## Database

### Schema

```sql
-- schema.py (frozen v1)
feeds   (id PK, url UNIQUE, title, last_fetched_at, created_at)
items   (id PK, feed_id FK→feeds CASCADE, guid, title,
         media_url, media_type, pub_date, fetched_at, seen_at,
         UNIQUE(feed_id, guid))

-- migrations.py adds
feeds.site_link, feeds.etag, feeds.last_modified, feeds.source_mtime
items.media_json      -- JSON array of {url, type}: all slides of a gallery
items.media_key       -- canonical identity of media_url; the cross-feed dedup key

seen_media        (media_key PK, seen_at)              -- the durable seen record
dead_urls         (url PK, marked_at)                  -- permanently gone media
media_urls        (url, item_id FK→items CASCADE, PK(url, item_id)) -- known-URL gate index (§8.3)
resolved_guids    (feed_id, guid PK, resolved_at)      -- examined, deliberately not stored
media_hashes      (url PK, sha256, phash, hashed_at)   -- content identity
auth_config       (key PK, value)                      -- stores the TOTP secret
```

Rows without gallery data fall back to `media_url`/`media_type`; `item_slides()`
in `normalize.py` is the single accessor and also survives unparseable
`media_json`.

**Indexes on `items`:** `feed_id`, `pub_date DESC`, `seen_at`, `fetched_at`,
`media_url`, `media_key`, and `(feed_id, pub_date, id)`. The last one matches
`RANKED_ITEMS_CTE`'s window exactly, so `ROW_NUMBER` reads it in order instead of
sorting the table — and `/api/items` materialises that CTE **twice** per page
(once for the cursor anchor, once for the page), on the endpoint every scroll
hits. `idx_items_media_url` exists because `_candidate_items` looks items up by
`media_url` *inside a write transaction*; without it that is a full scan holding
the writer lock.

### The two tombstone tables

They look redundant. Each answers a different question, and collapsing them
re-introduces a specific bug:

| Table | Written when | Read by | Bug prevented |
|---|---|---|---|
| `seen_media` | the user scrolls past an item | the insert guard | A seen item is pruned, the feed still lists it, the next poll re-inserts it **unseen** — forever. |
| `resolved_guids` | the insert guard rejects an entry; every prune eviction; **and** every item dropped because all its media went dead | `_skip_guids` | The guard keys on `media_key`, which only exists after detection — so a rejected entry is re-detected on every poll. Plus the prune/serve-order collision described above, and a 404'd post re-inserting and failing again, forever. |

The distinction that survives is `seen_media`'s: it is keyed on `media_key`, not
`(feed_id, guid)`, so a cross-posted picture stays seen whichever feed carries it,
and it deliberately has **no foreign key to `feeds`**: its predecessor `seen_guids`
cascaded away whenever `sync_feeds` dropped a feed row, erasing the seen history.
`seen_guids` was read exactly once, by the v19 migration that drained it into
`seen_media`, and dropped entirely at v25 — it no longer exists.

`resolved_guids` *does* cascade — dropping a feed drops its items too, so a
re-added feed should start clean.

### Migrations

`MIGRATIONS` is a flat, append-only list of SQL strings **and callables taking the
connection**. `PRAGMA user_version` stores the count applied. On startup,
`MIGRATIONS[current_version:]` runs in sequence with the version incremented
**after each step** — not once at the end — so a failure part-way keeps what
succeeded and the next startup resumes at the step that failed.

Each entry must be idempotent: SQLite runs DDL outside any transaction, so a
statement takes effect before the version bump that records it, and a crash in
between replays it. That is what the `IF NOT EXISTS` clauses and
`run_migrations`' duplicate-column handling are for.

Current list (v1–v25): `fetched_at` index · `seen_guids` + backfill ·
`auth_config` · `media_json` · `dead_urls` · `unavailable_guids` · `site_link` ·
`media_url` index · `media_key` column · `media_key` index · `media_hashes` +
`sha256` index · `seen_media` · `etag` · `last_modified` · `source_mtime` ·
`resolved_guids` · `_backfill_seen_media` · merge `unavailable_guids` into
`resolved_guids` · drop `unavailable_guids` · `media_urls` table + index ·
`media_urls` backfill · drop `seen_guids`.

That last one is a callable, not SQL, because it needs `media_key()`. It is also
the reason the list accepts callables at all: it originally ran unconditionally on
every startup, outside the version gate, and was folded in as v19 so it runs once.

### WAL Mode

`PRAGMA journal_mode=WAL` on every connection. WAL allows concurrent readers
while one writer is active — essential because the background loops write
continuously while the API serves reads.

### Connection strategy

| User | Connection | Lifetime |
|---|---|---|
| Request handlers | `app.state.db` via `get_db()`/`DbDep` | Process |
| Background loops | their own connection | Process |
| Streaming bodies, warm tasks | `run_with_own_db()` | One write |

`get_db()` returns the **process-wide** connection, not a fresh one. aiosqlite
serialises statements on the connection's own worker thread, so every request
queues behind every other; with WAL and queries this small that is cheaper than
starting a thread and running two PRAGMAs per request.

The cost is a shared implicit transaction — sqlite3 opens one per connection, not
per coroutine. **Every write on that connection, single statement or not, must go
through `write_transaction()`**, which takes a module-level lock, commits on
success, and rolls back on any other exit. Two coroutines sharing one transaction
otherwise commit each other's in-flight statements, and either one's rollback
discards the other's work. The rollback catches `BaseException`, not `Exception`:
a `CancelledError` arriving at any `await` inside the block would otherwise unwind
past it and leave the connection holding a RESERVED lock, with every
`run_with_own_db` writer then waiting out the 30 s busy timeout and WAL unable to
checkpoint.

`run_with_own_db()` exists for writers with no connection to borrow: background
warm tasks, and streaming response bodies, which execute *after* the route
function returned. A private connection also keeps those writes out of the shared
implicit transaction. Failures are logged and swallowed — every caller is
fire-and-forget bookkeeping (cache digests, dead-URL marks) that must not fail the
media it belongs to. The known cost is one open/close per call.

---

## Media Subsystem

### Cache (`media/cache.py`)

Files live at `{CACHE_DIR}/{sha256(url)}` — flat, no extension — beside a
`{sha256(url)}.meta` sidecar holding the upstream Content-Type. The hash filename
makes lookup O(1) and handles any characters in a URL.

**Write.** `cache_stream_tee(url, chunks, content_type)` is the primitive: it
writes each chunk to a temp file *and yields it onward*, so the proxy can serve
the browser and fill the cache in one pass, buffering nothing beyond one chunk.

Two details, both fixes for permanently-broken cache entries:

- The temp name is unique per writer (`tempfile.mkstemp`). Two writers racing on
  the same URL is the **normal** case — the browser's proxy GET routinely overlaps
  the background warm for the same item. With a shared temp name the second
  writer's `open("wb")` truncated the first's in-flight file, and the loser's
  failed rename deleted the winner's `.meta`. With unique names both fill their
  own file and both rename onto the same destination: atomic, last-one-wins.
- The sidecar is written **before** the data rename, so a file visible to
  `cache_read` always has its content type. Without one the proxy falls back to a
  generic type (the filename is a bare sha256, so `mimetypes` cannot guess), which
  no browser will decode as video.

Cleanup catches `BaseException`, not `Exception`: a browser scrolling past
mid-download throws `GeneratorExit` in here, and that partial file still has to go.

`download_claim(url)` is an advisory in-flight registry (URL → concurrent
downloader count). The prefetcher **skips** a URL another download already holds;
the proxy proceeds regardless, because a user is waiting for those bytes.

**Read.** `cache_read` / `cache_read_meta`, plus two batching helpers:
`cache_lookup()` does the hit path's stat and meta read in one blocking call, and
`cache_names_present()` answers "which of these names are on disk" in one thread
hop, so `/api/items`' `cached` hint scales with the page rather than the cache.

**Evict.** `evict()` runs after every refresh cycle, in a worker thread, sorted by
mtime: delete over `CACHE_MAX_AGE_HOURS`, then trim from the oldest while over
`CACHE_MAX_ITEMS`, then trim again while over `CACHE_MAX_BYTES`. The third pass
exists because counting files cannot bound a directory of multi-gigabyte videos.
`.meta` entries are not counted (they are not cache entries in their own right,
and are unlinked with their data file); `.tmp` entries are skipped entirely —
they are in-flight downloads, and unlinking one breaks its writer.

### Upstream fetch (`media/fetch.py`)

Shared by the proxy and the prefetcher so the two cannot drift apart.

`open_upstream(url, item_id, client)` returns an **unread** streaming response
plus the validated content type. Every fetch target — the original URL and each
redirect hop — passes `_check_url()` first, which is why redirects are followed
manually (max 5) rather than by httpx:

- scheme must be `http`/`https`, host must be present;
- the host is resolved and, unless `ALLOW_PRIVATE_MEDIA_HOSTS`, every resolved
  address must be public (private, loopback, link-local, multicast, reserved and
  unspecified are refused; IPv4-mapped IPv6 is unwrapped first — `::ffff:127.0.0.1`
  is loopback in a hat);
- the request is then **pinned to a validated IP** with the original hostname as
  `Host` and TLS SNI, so httpx cannot re-resolve and reach an address the check
  never saw (DNS-rebinding TOCTOU);
- a relative `Location` is joined against the **logical** URL, not the pinned one.

Response gating:

| Condition | Action |
|---|---|
| status ∈ `PERMANENT_STATUSES` = {403, 404, 410, 451} | mark the URL dead, drop a fully-dead item, raise `UpstreamError` |
| any other non-success (429, 5xx, timeouts, connection errors) | raise **without touching the database** |
| `image/svg+xml` | mark dead, raise `NonMediaUpstreamError` |
| content type not `image/*`, `video/*` or `application/octet-stream` | mark dead, raise `NonMediaUpstreamError` |
| declared `Content-Length` over `MEDIA_MAX_BYTES` | raise, do not mark dead |

403 is in the permanent set because removed and hotlink-protected media answers
403 far more often than 404 on the sites this reader is pointed at. The cost is
explicit: an origin that 403s every request without a `Referer` has its items
erased rather than merely failing to load.

`tee_to_cache(url, response, content_type)` yields the body onward while
`cache_stream_tee` writes it, enforces the running byte cap, and records the
content digest for dedup **only on a complete transfer** — half a file has the
wrong hash. It always closes the response, and wraps the inner generator in
`contextlib.aclosing` so an abandoned stream's temp file is cleaned up now rather
than whenever the generator is finalised. `fetch_to_cache()` drains the tee for
callers that only want the cache filled, and never raises.

Every DB write here runs on its own connection via `run_with_own_db`, because a
streaming response body executes after the route function returned and its
connection is out of scope.

### Prefetch (`media/prefetch.py`)

Two producers, one shared `asyncio.Semaphore(10)` so a fast scroll cannot pile up
unbounded outbound connections.

Both producers assemble their query through `src/db/queries.py`'s `resolve_anchor()`
and `ranked_page()` — the same functions `/api/items` uses — so the warm window
can never drift from what the API actually serves.

**`warm_startup_cache()`** calls `ranked_page()` for `FEED_INITIAL_COUNT + PREFETCH_AHEAD`
items in `UNSEEN_FIRST_ORDER_BY` — the interleave `/api/items` serves, with unseen rows
ahead of seen ones, because the client defaults to `showSeen: false`. Warming in
any other order fills the end of the library the reader reaches last and leaves
page one a guaranteed miss. Warming an already-cached item costs nothing (`_warm`
returns on a `cache_read` hit without opening a connection), so a restart with a
warm cache issues no upstream requests.

**`prefetch_ahead(item_id, …, unseen=…)`** resolves its anchor with `resolve_anchor()`
and calls `ranked_page()` for the next `PREFETCH_AHEAD` items strictly after it in the
`(rn, feed_id, id)` key. `unseen` has **no default** — the caller must state the
filter it paged with, so the warm window matches what is about to be displayed.
Returns `None` when the anchor names no row, which the hint endpoint turns into a
404 without a second lookup.

`_bg_tasks` holds a strong reference to every warm task (the event loop's is weak)
so shutdown can cancel them; `_hint_backlog`, incremented by `_track_hint`, counts
only the request-driven path, so `MAX_BACKLOG` (50) measures the hint backlog
alone — a draining startup warm must not spend the reader's cap.

### Dead-URL tracking (`media/availability.py`)

`mark_url_dead_and_maybe_drop(url, item_id, db)`:

1. record `url` in `dead_urls`;
2. collect candidate items: `_candidate_items` joins `items` to `media_urls` on
   `url`, no `item_id` narrowing — every slide is indexed, so this one join
   finds every item containing that URL, primary or gallery slide alike;
3. for each item whose every slide URL is now dead: `DELETE` the row and write a
   `resolved_guids` tombstone;
4. `_skip_guids` reads that tombstone on the next poll, so the guid never comes
   back.

`item_id` no longer narrows the candidate lookup — every slide URL is reachable
by the `media_urls` join regardless — it now only feeds the debug log line.

`is_known_media_url()` is the gate the proxy and the failure report share: one
indexed lookup against `media_urls`, which holds every media URL of every item —
primary and gallery slides alike, one row each, kept in step with `items` by an
`ON DELETE CASCADE` foreign key. See spec.md §8.3.

### Content dedup (`media/dedup.py`)

`normalize.media_key()` catches the same picture behind cosmetically different
URLs. It cannot catch a genuine re-upload: two distinct CDN asset IDs holding the
same image. `record_media_hash(url, digest, db)` — called from the tee, when the
bytes are already in hand, so it costs no extra traffic — does:

1. compute a perceptual hash if `DEDUP_SIMILARITY > 0` and the file is a decodable
   image (video and truncated downloads simply have none);
2. upsert into `media_hashes`;
3. find twins: exact `sha256` matches at other URLs first, then — only if none —
   perceptual matches within the threshold;
4. if this URL's item is **newer** than every twin's, delete it and write a
   `resolved_guids` tombstone. If it is the oldest, it is canonical and the
   duplicates are somebody else's problem to drop.

The tombstone is what makes the drop stick across the next poll. This deliberately
mirrors `availability.py`'s record-fact / find-items / delete-and-tombstone dance.

**The perceptual hash** is a 256-bit block mean: convert to ITU-R 601-2 luma,
centre-crop to 80% (dropping watermarks and letterboxing), resize to 64×64
bilinear then 16×16 BOX (exactly the 4×4 block average the hash needs), and take
one bit per cell for "brighter than the image average". Matching is
`(256 − hamming) × 100 // 256 > DEDUP_SIMILARITY`; at 97 that drops an image
differing by ≤5 bits. Comparison is an O(n) scan over at most `KEEP_ITEMS` hashes
— a few hundred microseconds; a BK-tree is the upgrade path if it ever shows up in
a profile.

---

## API Layer

All routers mount under `/api`. Handlers take `db: DbDep` and, where needed,
`client: HttpDep` / `StatusDep`.

### `GET /api/items`

Interleaves feeds with a window function (`db/queries.py`, shared with the
prefetcher so the two orderings cannot drift):

```sql
WITH ranked AS (
    SELECT …, ROW_NUMBER() OVER (PARTITION BY feed_id
                                 ORDER BY pub_date ASC, id ASC) AS rn
    FROM items                       -- the FULL item set
)
SELECT … FROM ranked
[WHERE seen_at IS NULL] [AND (rn, feed_id, id) > (?, ?, ?)]
ORDER BY rn ASC, feed_id ASC, id ASC
LIMIT ?
```

`rn=1` is the oldest item of each feed, `rn=2` the second-oldest, and so on, so
ordering by `rn` then `feed_id` interleaves feeds evenly rather than draining one
at a time. The window runs over the full set with the **seen filter applied
outside** it, so marking an item seen drops it without renumbering anything else.
The `id` tiebreak is repeated in the window and in every `ORDER BY`: the anchor is
resolved by one statement and the page read by another, and two statements can
only agree on a rank if ties break deterministically. Change one, change all.

A NULL `pub_date` is harmless *because* rank comes from the window: `ROW_NUMBER`
sorts NULLs first and ranks them 1..k, while a row-value comparison with a NULL
member evaluates to NULL and so drops exactly those rows.

**Pagination is keyset, not offset.** Offset was tried and is silently lossy: with
`unseen=true` the result set shrinks as the client marks items seen, so
`page × size` overshoots. And `rn` alone is not a cursor either — it is recomputed
per request and moves under an outstanding cursor in both directions (a prune
lowers it, a row inserted with an older `pub_date` raises it).

So the cursor is **`after_id` + `after_rn`**: the anchor is re-resolved from the
same CTE that orders this page, and the page is bounded at
`min(after_rn, resolved_rank)`. Taking the lower bound means a raised rank
*reopens* the window instead of skipping every undelivered row between the two
ranks; the reopened rows come back as duplicates, which the client's known-set
guard drops. A vanished anchor answers **410** — resolving it to rank 0 would make
the comparison match everything, i.e. page one of the global interleave, which the
client discards, leaving a cursor that never advances.

Two things this does not recover, stated in the docstring: the anchor and the page
are two statements, so a refresh landing between them can still shift the rank;
and `min()` only reopens rows *ahead* of the cursor, so a row inserted behind it
(typically an undated entry) waits for a reload.

Each row carries `rn` (the client echoes it) and `cached` — a hint from one batched
`cache_names_present()` probe telling the browser which items are already on disk.
Only the primary `media_url` is checked, so a gallery counts as cached once its
first slide is on disk, matching what the browser queue actually prioritises.

### `POST /api/items/{id}/seen`

Sets `seen_at` and writes through to `seen_media`, keyed on the normalised media
URL, both under one `write_transaction` and both bound to one Python timestamp so
they cannot diverge. `UPDATE … RETURNING` does the update and reads back
`media_url` + `seen_at` in one statement.

The optional `media_url` query parameter is the browser's own copy. Pruning evicts
oldest-first while this endpoint serves oldest-first, so a refresh cycle routinely
deletes the row *between the page being served and the reader scrolling past it*.
Without the parameter the UPDATE matches nothing, the request 404s, and
`seen_media` — the record whose entire job is to outlive pruning — never learns.
The stored row always wins when it exists; the parameter is consulted only when
the row is gone, and only if it is a syntactically valid http(s) URL
(`_usable_media_url`), so a stray value cannot land a key that suppresses
unrelated media.

The browser marks locally *before* firing this, and sends it with
`navigator.sendBeacon`, not `fetch`: in-flight fetches are cancelled when the tab
closes, which used to lose the marks made in the last moments of a session.

### `GET /api/media/proxy`

```
1. cache_lookup(url) → HIT: FileResponse (zero-copy sendfile, Range-capable —
   what makes a cached video seekable), typed from the .meta sidecar
2. MISS: is_known_media_url gate, else 404
3. open_upstream + StreamingResponse(tee_to_cache(...)) — upstream bytes go to
   the browser and into the cache in the same pass
```

The cache lookup still goes **first**, but only because a hit needs no gate at
all — the key is `sha256(url)` so it cannot escape `CACHE_DIR`, and a URL can only
be in the cache because it passed the gate on an earlier request.

Step 3 previously downloaded the whole file to disk and only then replied, which
meant the browser saw *nothing* for the entire upstream transfer — the single
largest contributor to the "black screen while loading" symptom. **Documented
trade-off:** a miss is served as a non-Range `200`, so an uncached video is not
seekable (and Safari's initial byte-range probe restarts from zero) until it has
been cached. A hit is. Streaming the miss through is what prevents the stall.

`CacheFileResponse` answers **503 + Retry-After** if the cached file vanishes
between `cache_lookup` and Starlette's own stat (`evict()` runs after every refresh
cycle). Starlette stats and opens by path, so holding a descriptor would not close
the window, and the `RuntimeError` it raises surfaces after the handler returned —
outside every `except` it has — reaching the browser as a 500 that also logs the
container's cache path. The window is narrowed, not closed.

### `POST /api/media/failed`

The browser gives up on a download after `MEDIA_LOAD_TIMEOUT_S` and reports it
here; the URL is marked dead and fully-dead items dropped. Gated on
`is_known_media_url` for the same reason the proxy is — this deletes rows on the
client's say-so.

This policy is **stricter than `open_upstream`'s** on purpose: that path only marks
dead on a permanent answer, because dropping on transient failures erases posts
that would have loaded later. A timeout cannot tell gone from slow, so
`MEDIA_LOAD_TIMEOUT_S` is the knob to raise if usable posts start disappearing.

### `POST /api/prefetch/hint`

Fire-and-forget from the browser after each queue rebuild. The body model is
strict: `item_id` is length-bounded (it is a sha256 everywhere it is produced, and
neither uvicorn nor FastAPI caps body size by default), and `extra="forbid"` —
a misspelled `unseen_only=true` would otherwise be accepted silently and warm the
wrong window. `unseen` is a plain `bool`, not `StrictBool`, because `/api/items`
coerces `?unseen=1` and a hint that rejects what the page it mirrors accepts would
422 into the browser's silent `.catch()`.

The endpoint awaits only the two ranking queries; the warm tasks are not awaited.

### `GET /api/reddit-feeds/status`

Proxies the [Reddit Feeds](https://github.com/otonm/reddit-feeds) companion's
`/status` on the capped status client. The body is read under `MAX_STATUS_BYTES`
(1 MiB) and parsed once as a validity check — the parsed value is discarded and the
original bytes forwarded, so an HTML login page from a reverse proxy cannot reach
the browser labelled `application/json`. `asyncio.timeout(10)` bounds the **total**
duration separately, because httpx's read timeout is the gap between reads: a
companion emitting one byte every nine seconds would trip neither that nor the byte
cap. Failures return 502 with the reason in the detail.

`_log_outcome` logs *transitions*, not repeats: the modal polls at 1 Hz, so only
the change into failure warns, persistence drops to DEBUG, and recovery logs INFO.

---

## Cross-cutting

**Request correlation.** `RequestIDMiddleware` sets a contextvar from
`X-Request-ID` (or a fresh uuid) and echoes it on the response; `RequestIDFilter`
copies it onto every log record and `main.py`'s format string renders it, so a
DEBUG run can be reassembled by filtering on the id. Work that outlives the request
— warm tasks, streaming bodies — runs after the contextvar is reset, so those call
chains take an explicit `request_id` parameter. The middleware also sets
`X-Content-Type-Options: nosniff` (via `setdefault`), riding along here rather than
in a third `BaseHTTPMiddleware` that would put another task group around the
proxy's `StreamingResponse`.

**Log safety.** `loggable()` wraps every string that came from outside the process
— request parameters, and equally feed content (guids, titles, URLs), because a
feed is a trust boundary just like a request path. It `repr`s (escaping newlines
that would otherwise forge a whole log record) and truncates to 200 chars. Values
are escaped **once, at entry**, because the same values end up embedded in
exception messages that outer handlers re-render.

**Timing.** `timer()` returns a closure giving elapsed milliseconds, read at the
log site so the measured span always covers the call it reports.

**422 logging.** `main.py` installs a `RequestValidationError` handler, because
`cache-queue.js` attaches only `.catch(() => {})`, which does not fire for an HTTP
422 — a client serialising a field wrongly would disable prefetching with no trace
but uvicorn's access log. The offending `input` value is stripped before logging
(pydantic has no size limit of its own).

---

## Frontend

Eight vanilla JS modules, no framework, no build step. Each attaches to
`window.MRR` and exposes a small public API.

| Module | Responsibility |
|---|---|
| `app.js` | Startup, config from CSS vars, keymap, pointer-drag swipe, service worker registration, module wiring |
| `item-store.js` | The item array, the keyset cursor and its recovery loop, seen + unusable reporting |
| `feed-view.js` | DOM rendering: placeholders, media wraps, galleries, badges, snapping |
| `scroll-controller.js` | Two IntersectionObservers → current item, seen beacons, page top-up |
| `autoscroll-controller.js` | Per-item dwell timer: image delay, GIF duration, video `ended` |
| `cache-queue.js` | 3-worker priority download queue; debounced prefetch hints |
| `zoom-controller.js` | Double-tap / double-click / `z` zoom to 1:1, cursor-follow and finger pan |
| `controls.js` | FAB menu, autoscroll/mute/show-seen toggles, status modal, UI_DEBUG overlay |

### State model (`item-store.js`)

```js
items[]        // every item loaded from the API, in display order
currentIndex   // set by scroll-controller via setCurrentIndex()
hasMore        // false once the API returns an empty page or the cursor is exhausted
fetching       // gates concurrent fetches
showSeen       // include seen items?
generation     // bumped by resetForReload — see below
```

There is no `page`: the cursor is derived from the tail of `items`. Stale
responses are handled by the **generation counter**: `resetForReload()` clears the
list and bumps it, and a fetch that started under an older generation discards its
own result instead of merging two pages into one store — and instead of handing
`renderInitial` an item the top-up loop had already put on screen. Clearing
`fetching` is likewise only done by the generation that owns it.

### The cursor recovery loop (`fetchPage`)

Four protections, each for a real failure:

1. **Known-set filter** — a new feed appearing shifts the interleave and can hand
   back an item already held. Two copies make `findIndexById` ambiguous and desync
   `currentIndex` from the DOM.
2. **Re-anchor on the response's own tail** when a page comes back as *entirely*
   duplicates. Deriving the next cursor only from appended rows would leave
   `after_id`/`after_rn` unchanged and repeat the same request forever. The tail is
   the page's max `(rn, feed_id, id)`, the same ordering the predicate compares
   against, so the next request strictly advances. A correct server converges in
   one or two rounds; `MAX_REANCHOR_ATTEMPTS = 5` only stops a misbehaving one from
   hot-looping.
3. **Exponential walk-back on 410** — a feed leaving the OPML cascades its whole
   item set, so the run of dead anchors is not small; doubling finds a survivor in
   log(n) requests. Reloading from page one instead would clear the list and drop
   the user to the top of the scroll.
4. **`back` advances only on 410**, never on a re-anchor round — otherwise the
   stride is inflated before the walk-back even takes over, stepping past anchors
   that are still perfectly good.

`pub_date` is deliberately **not** sent as a cursor component: an undated item
serialises as the string `"null"`, which the server would compare as text against
real dates.

### IntersectionObservers (`scroll-controller.js`)

| Observer | Threshold | Purpose |
|---|---|---|
| `observer` | 0.6 | Most-visible item → `currentEl`, `currentIndex`, visible video, queue rebuild, autoscroll re-arm, page top-up |
| `seenObserver` | 0 | Binary enter/leave; fires the seen beacon when an item leaves **upward** |

**The observed element is authoritative for navigation**; the store index only
feeds the cache queue and the debug overlay. `findIndexById` returns the *first*
match, so deriving position from it cannot be trusted to point back at the element
the user is on. `feed-view.js` walks siblings from `currentEl` for the same reason
— the store splices out failed items, so the two are separate index spaces.

`seenObserver` skips placeholders (no `mediaType`); it fires again when the
placeholder is replaced by a `.media-item` with the new element's current
intersection state. The item on screen when the tab closes never leaves the
viewport, so `pagehide` marks it explicitly.

Page top-up is scroll-driven — an idle feed makes no requests.

### Cache queue (`cache-queue.js`)

A priority queue drained by **3 concurrent workers**. On
`rebuild(currentIndex, lookaheadN, items)`: the current item first, then the
forward lookahead, then not-yet-loaded items behind the cursor (nearest first),
then the rest. Within each band except the first, items the server reported as
`cached: true` are queued **ahead** of uncached ones — a cached item decodes in
milliseconds while a miss waits on the origin, and that is what makes a scroll
through warm items feel instant. The current item is exempt: it is what the user is
looking at and must load either way.

Each download carries a `MEDIA_LOAD_TIMEOUT_S` deadline; on expiry the element's
`src` is cleared to drop the connection and `item-failed` fires. Three workers plus
a deadline is what stops one stalled origin freezing every placeholder behind it —
including items already on disk that would have painted immediately. Three also
stays inside the browser's ~6-connections-per-host budget alongside gallery slides.

After each rebuild a **debounced** (250 ms) `POST /api/prefetch/hint` fires. Each
hint costs the server two `ROW_NUMBER` passes on the connection `/api/items`
shares, so undebounced hints would starve the endpoint the scroll depends on.

### Failed items

`onItemFailed(id, reason)` removes the node **and** the store entry together. They
must stay in sync: a node with no store entry makes `onIntersect`'s `findIndexById`
return −1 and bail before the queue is rebuilt or autoscroll re-armed. If the
failing node is the current one, the feed is scrolled onto a neighbour explicitly
first, rather than letting the browser decide where the scroll lands once it
vanishes. `item-store.reportUnusable(id)` must run **before** the splice —
`media_url` lives nowhere else — and beacons `/api/media/failed`.

### Galleries (`feed-view.js`)

Items with `media.length > 1` render as a horizontally snapping strip with a dot
row.

- `buildGallery()` — slide 0 reuses the element the cache queue already
  downloaded; the rest point at the proxy and defer to the browser's own lazy
  loading (`preload="none"` for video, `loading="lazy"` for images). A 20-slide
  gallery opening 20 connections at once exhausts the per-host budget and starves
  the queue and `/api/items` behind it.
- `paintDots()` runs on **every** scroll event, undebounced and with no CSS
  transition: each dot gets its closeness to the current slide as `--t` (1 centred,
  0 a slide away) and CSS interpolates size and brightness. The scroll position
  *is* the animation, so it tracks a finger exactly.
- `onGalleryScroll()` is debounced (60 ms): mark the active slide, drop any zoom,
  pause offscreen slide videos, and — when this wrap is the current item —
  re-point the visible-media rule and autoscroll at the new slide.
- Dot clicks are **delegated**, with the index computed at click time:
  `removeSlide` shifts indices, so a captured loop index would point at the wrong
  slide.
- A slide whose media errors is removed along with its dot; at one slide left the
  dots and arrows go; at zero the whole item fails.
- `advanceOrNext()` / `galleryNext()` / `galleryPrev()` step slides and fall
  through to the next/previous feed item at the boundaries.

### Videos

At most one plays at a time. `setCurrentMedia()` pauses **every** video in the feed
on each transition, not just the previous one: videos are created with
`autoplay` and start as soon as they land in the DOM, so pausing only the previous
leaves the rest playing — and unmuting any one of them via the global toggle would
leak audio from non-visible items.

A user pause is distinguished from a programmatic one by setting `_pausedByJs`
before every `pause()` call and clearing it in the handler; a `pause` event without
that flag is a real click and sets `userPaused`, which suppresses autoplay until a
subsequent `play` clears it. Intent is deliberately **not** inferred from
`volumechange` or `seeking` — browsers fire those for their own reasons (autoplay
policy adjustments, end-of-video seek-backs, visibility changes). A paused video's
`muted` state is never mutated during a transition, since on some implementations
that counts as user interaction and suppresses future autoplay.

### Autoscroll (`autoscroll-controller.js`)

Per-item timers, not a RAF pixel-scroll loop:

| Media type | Advance trigger |
|---|---|
| image | `setTimeout(IMAGE_AUTOSCROLL_DELAY_S)` |
| gif | `setTimeout(getGifDuration(src))` — a GIF byte-scan |
| video | `addEventListener('ended', …)`, once |

A **minimum dwell floor** (`IMAGE_AUTOSCROLL_DELAY_S`) applies to all three so
short GIFs, very short videos or scroll-snap overshoots don't read as skipped
items; videos longer than the floor advance immediately. "Advance" is
`advanceOrNext`, so galleries step slides before stepping items. Binding is per
wrap **and** per active slide, always unbinding first — binding for a lookahead
item would steal the `ended` listener from the item actually being watched. Videos
get `loop = !autoscroll`.

`getGifDuration(url)` fetches the GIF bytes and scans for Graphic Control Extension
blocks (`0x21 0xF9 0x04`), each carrying a 2-byte delay in 1/100 s; the sum is
clamped to [50 ms, 60 s]. Falls back to the image delay if the scan yields nothing
or the URL is not a proxy URL.

### Zoom (`zoom-controller.js`)

Double-tap, double-click or `z` scales the current **image** to 1 image pixel per
CSS pixel (`naturalWidth / renderedWidth`, and nothing at all if that is ≤1.01 — a
downscale is not what "zoom to 100%" means). It is a CSS transform, so nothing
reflows and the existing `overflow: hidden` clips it for free.

Geometry is snapshotted at zoom time and **never re-measured while zoomed**: the
element is transformed, so `getBoundingClientRect` would report the scaled box and
each pan would compound the error. Desktop panning follows the cursor (no button
held; cursor at the item's left edge shows the picture's left edge, so one sweep
reaches the whole image); mobile drags 1:1. **Panning is never animated** — the
transition is applied inline only for the length of the in/out step, because a
transform transition left on would lag behind the cursor and the finger.

While zoomed, `touchmove` is `preventDefault`ed and the wrap carries
`touch-action: none`, which holds **both** the feed's vertical scroll and the
gallery's horizontal swipe — on mobile the picture must be zoomed out before
navigation works. On desktop the wheel and the navigation keys reset the zoom and
move on. One `pointerup` double-tap detector serves mouse and touch alike
(`dblclick` does not fire reliably on iOS Safari, and two mechanisms would need a
dedupe guard between them); there is no collision with `app.js`'s swipe, which
needs 40 px of travel where this needs the two taps within 30 px.

Zoom is dropped by every navigation path through the single choke point
`setCurrentEl`, plus slide changes, wheel, keys, and `resize` (the snapshot is
stale after a rotate). Autoscroll is suspended on zoom-in and re-armed on zoom-out.
`prefers-reduced-motion` forces the transition to 0.

### Client Config Injection

`main._build_html()` replaces `<!-- CONFIG_VARS -->` with:

```html
<script>window.MRR_CONFIG = {"feedInitialCount":10,"imageAutoscrollDelayS":2,"mediaLoadTimeoutS":10,"zoomTransitionMs":200,"uiDebug":0};</script>
```

`/` is escaped in the JSON payload so a value containing `</script>` cannot close
the tag early. `app.js:readConfig()` reads `window.MRR_CONFIG` directly at module
load, so values are available synchronously before any rendering and no config
round-trip is needed.

`{{VERSION}}` in asset URLs is replaced with `int(time.time())` at startup, forcing
browsers and the service-worker cache to re-fetch assets after a restart.

### Service worker

`sw.js` precaches the app shell and icons on install, serves `/static/*`
cache-first and everything else network-first with a cache fallback, and skips
`/api/*` entirely.

---

## Authentication

Isolated in `src/auth/`.

### Middleware (`auth/middleware.py`)

Registered before all routers. Every request:

1. **`/health` passes unconditionally** — container liveness probes run from
   inside the container and have no proxy headers.
2. **HTTPS check** — reject unless `X-Forwarded-Proto == https` (403). The app
   assumes a trusted TLS-terminating reverse proxy; do not expose it directly.
3. **Auth-free paths** — `/login`, `/setup`, `/static/*` pass without a session.
4. **Session validation** — everything else needs a valid signed cookie, else a
   302 to `/login`.

Order matters: the HTTPS check runs **before** the auth-free check, so the login
page is never served over plain HTTP.

### Sessions (`auth/session.py`)

Stateless signed, timestamped tokens (`itsdangerous.URLSafeTimedSerializer`), keyed
by `AUTH_SECRET_KEY`. Cookies are HttpOnly, Secure, SameSite=Lax, 7-day max age.
Rotating the key invalidates every active session instantly. A separate 10-minute
setup cookie carries the pending TOTP secret during first login.

### Login flow (`auth/routes.py`)

```
POST /login → lockout check (429)
            → secrets.compare_digest on username and password (401)
            → no TOTP secret stored?  generate one, put it in the 10-min setup
                                      cookie only, 303 → /setup
            → pyotp verify (valid_window=1, ±30 s), 401 on failure
            → reset lockout, set the 7-day session cookie, 303 → /

GET  /setup → already enrolled? 302 → /login
            → verify the setup cookie (403 if missing/expired)
            → render setup.html with the otpauth:// URI (client-side QR via the
              bundled qrcode.min.js — no CDN) plus the base32 secret as copyable text

POST /setup → lockout check, already-enrolled check, setup-cookie check
            → verify against the PENDING secret
              · failure → record it, re-render with an error, and RE-ISSUE the
                setup cookie so a retry gets a fresh window instead of expiring
                mid-enrollment and forcing a new QR code
              · success → persist to auth_config (under write_transaction),
                clear the setup cookie, set the session cookie, 303 → /
```

The candidate secret lives **only** in the signed cookie until the user proves they
can generate a code from it. That is what makes an interrupted enrollment safe.
`pyotp` is used directly in `routes.py`; there is no separate `totp` module.

### IP lockout (`auth/lockout.py`)

An in-process dict keyed by the first `X-Forwarded-For` value, using a
**monotonic** clock so it is immune to system clock changes. After
`AUTH_LOCKOUT_ATTEMPTS` failures the IP is locked for `AUTH_LOCKOUT_MINUTES`; the
counter resets on success, and resets itself once a window elapses so failures
cannot accumulate forever. `/login` and `/setup` share one tracker. State is lost
on restart — acceptable for a single-process deployment.

---

## Configuration

`config.py` defines a `Settings` dataclass; `_load_settings()` reads `os.environ`
at import time and returns a singleton. Every field maps to the uppercased env var
of the same name. Only `int` and `str` are parsed, which is why flags are declared
as ints (`ui_debug: int = 0`, `dedup_similarity: int = 97`,
`allow_private_media_hosts: int = 0`) rather than bools.

Four checks **fail fast at startup** rather than clamping:

- `AUTH_SECRET_KEY` must be non-empty — an empty session signer is forgeable.
- `AUTH_USERNAME` and `AUTH_PASSWORD` must both be set. Both-empty is *not* a safe
  "no-auth mode": `compare_digest("", "")` becomes a free login, `/login` then
  redirects to `/setup` with a setup cookie, and any visitor becomes admin.
- `FEED_INITIAL_COUNT` ∈ [1, 200] — the browser sends it as `size` to
  `/api/items`, which caps `size` at 200; above that every request 422s before the
  handler runs and the feed renders empty, retrying forever with nothing in the
  application log. A silent clamp is how those two bounds drifted apart.
- `MEDIA_LOAD_TIMEOUT_S` ∈ [1, 300] — a timeout **deletes** the item it fires on,
  so 0 would empty the library on the first scroll.

Frontend-visible values travel to the browser via the `window.MRR_CONFIG` script
injection — see [Client Config Injection](#client-config-injection).

### `UI_DEBUG` overlay

`UI_DEBUG=1` makes `controls.js` build a fixed top-right overlay describing the
item the feed is snapped to: feed id, title, media type and file extension, slide
count, publish date, cache `HIT`/`MISS` with the measured load time, and the queue
depth. It is `pointer-events: none`, so it never intercepts a tap.

---

## Testing

`tests/conftest.py` fixtures:

| Fixture | What it is |
|---|---|
| `db` | A **file-backed** temp DB with schema + migrations applied, with `settings.db_path` pointed at it |
| `http_client` | The `httpx.AsyncClient` the routes receive in place of `app.state.http` |
| `client` | A test app with the API routers, `get_db` / `get_http` / `get_status_http` overridden |
| `mock_http` | `respx.MockRouter` for intercepting external HTTP |
| `reddit_api_url` | Pins the companion URL and resets its transition state |
| `auth_settings` | Test credentials and a fixed signing key |
| `auth_client` | Auth routes + `AuthMiddleware`, a fresh lockout tracker per test, `X-Forwarded-Proto: https` by default |
| `authed_client` | `auth_client` pre-loaded with a valid session cookie |

The `db` fixture is deliberately **not** `:memory:`: the proxy and the prefetcher
record dead URLs and content digests on their *own* connection, because that work
outlives the request that started it, and a second connection cannot see another
connection's in-memory database — those writes would silently land nowhere. It also
gets its own directory so tests that point `cache_dir` at `tmp_path` do not count
the DB file as a cache entry.

Three autouse fixtures handle module state that would otherwise make results
depend on test ordering:

- `_stub_dns` keeps the SSRF guard off real DNS — hostnames resolve to a fixed
  public address so respx-mocked fetches pass the guard offline, while literal IPs
  still resolve to themselves so tests aiming the proxy at `127.0.0.1` or `10.x`
  still exercise the rejection.
- `_clear_prefetch_tasks` clears `prefetch._bg_tasks` around each test.
- `_reset_write_lock` replaces the module-level `asyncio.Lock`. A lock only binds
  to a running loop the first time it is *contended*, and pytest-asyncio hands each
  test its own loop, so the first test to contend it binds it and any later one
  raises "bound to a different event loop".

Coverage target: **90 %** (`--cov-fail-under=90`).

```bash
uv run pytest                    # all tests with coverage
uv run pytest tests/test_api.py  # one file
open htmlcov/index.html          # HTML report
```
