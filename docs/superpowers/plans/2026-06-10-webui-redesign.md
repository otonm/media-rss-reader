# WebUI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current paginated WebUI with a vertical, full-screen snap-scroll feed backed by a strict one-at-a-time media cache queue with priority-based prefetch.

**Architecture:** Three independent JS modules (`itemStore`, `cacheQueue`, `feedView`) wired by two controllers (`scrollController`, `autoscrollController`) plus a `controls`/`keymap` layer. CSS scroll-snap drives the snap-scroll layout. One new backend endpoint (`/api/items/count`) for the `N / total` counter. All other backend code stays as-is.

**Tech Stack:** Python 3.14, FastAPI, aiosqlite, pytest, respx. Vanilla JS, no build step. CSS with `scroll-snap-type`. HTML5 `<video>` and `<img>` elements.

**Spec reference:** `docs/superpowers/specs/2026-06-10-webui-redesign-design.md`

---

## File structure

**New files:**
- `src/static/item-store.js` — data layer, fetch + currentIndex + events
- `src/static/cache-queue.js` — priority queue + single-worker downloader
- `src/static/feed-view.js` — render placeholders, swap to media, snap helpers
- `src/static/scroll-controller.js` — IntersectionObserver → currentIndex
- `src/static/autoscroll-controller.js` — dwell/video-end → snap-to-next
- `src/static/controls.js` — autoscroll + mute button wiring
- `tests/test_items_count.py` — pytest tests for new endpoint

**Modified files:**
- `src/config.py` — add 4 new settings, remove 4 unused
- `src/main.py` — update `_build_html` to inject new CSS variables
- `src/api/items.py` — add `/items/count` route
- `tests/test_api.py` — add count tests (alternatively a new file; we use a new file to keep the existing file focused on its existing tests)
- `src/static/index.html` — full rewrite
- `src/static/style.css` — full rewrite
- `src/static/app.js` — full rewrite (startup + keymap + wiring only; logic is in the modules)

**Unchanged:** `src/static/favicon.svg`, `src/static/login.html`, `src/static/setup.html`, `src/static/qrcode.min.js`, all of `src/api/feeds.py`, `src/api/media.py`, all of `src/db/`, `src/feeds/`, `src/media/`, `src/auth/`, `src/scheduler.py`, all `tests/test_*.py` except as noted.

---

## Task 1: Update `src/config.py` — add new settings, remove unused

**Files:**
- Modify: `src/config.py`

- [ ] **Step 1: Replace `src/config.py` with the new settings**

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

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Paths ---
    opml_path: str = "/data/feeds.opml"  # OPML file inside the container
    db_path: str = "/data/db/reader.db"  # SQLite database file

    # --- Feed refresh schedule ---
    opml_sync_interval: int = 3600  # seconds between OPML re-reads
    feed_refresh_interval: int = 900  # seconds between feed refresh cycles

    # --- Media cache ---
    cache_dir: str = "/cache"
    cache_max_items: int = 500  # max files on disk
    cache_max_age_hours: int = 48  # evict files older than this

    # --- Item retention ---
    keep_items: int = 1000  # max rows in the items table
    items_max_age_hours: int = 168  # delete seen items older than 7 days

    # --- WebUI behaviour (injected as CSS variables at startup) ---
    feed_initial_count: int = 10  # initial placeholders + lookahead window
    video_buffer_threshold_pct: int = 10  # % of video buffered before playback starts
    video_buffer_threshold_min_s: int = 2  # minimum seconds buffered (overrides pct if larger)
    image_autoscroll_delay_s: int = 2  # image dwell time in autoscroll mode

    # --- Server ---
    port: int = 8080
    log_level: str = "info"  # uvicorn log level

    # --- Authentication ---
    auth_username: str
    auth_password: SecretStr
    auth_secret_key: SecretStr
    auth_lockout_attempts: int = 5
    auth_lockout_minutes: int = 15


settings = Settings()
```

- [ ] **Step 2: Verify the file is valid**

Run: `uv run python -c "from src.config import settings; print(settings.feed_initial_count)"`
Expected: `10`

- [ ] **Step 3: Run lint**

Run: `uv run ruff check src/config.py`
Expected: `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git add src/config.py
git commit -m "feat(config): add webui feed settings, remove unused"
```

---

## Task 2: Add `GET /api/items/count` endpoint (TDD)

**Files:**
- Modify: `src/api/items.py`
- Create: `tests/test_items_count.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_items_count.py` with:

```python
import aiosqlite
from httpx import AsyncClient


async def _insert_feed(db: aiosqlite.Connection, feed_id: str = "feed1") -> None:
    await db.execute(
        "INSERT INTO feeds(id, url, title) VALUES (?, ?, ?)",
        (feed_id, f"http://example.com/{feed_id}.xml", "Test Feed"),
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
        (item_id, feed_id, item_id, "Title", f"http://example.com/{item_id}.jpg", "image", seen_at),
    )
    await db.commit()


async def test_count_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/items/count")
    assert resp.status_code == 200
    assert resp.json() == {"count": 0}


async def test_count_unseen_default(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """unseen defaults to true, so seen items are excluded."""
    await _insert_feed(db)
    await _insert_item(db, "seen_item", "feed1", seen_at="2024-01-01T00:00:00")
    await _insert_item(db, "unseen_item_1", "feed1", seen_at=None)
    await _insert_item(db, "unseen_item_2", "feed1", seen_at=None)
    resp = await client.get("/api/items/count")
    assert resp.status_code == 200
    assert resp.json() == {"count": 2}


async def test_count_unseen_true(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "seen_item", "feed1", seen_at="2024-01-01T00:00:00")
    await _insert_item(db, "unseen_item", "feed1", seen_at=None)
    resp = await client.get("/api/items/count", params={"unseen": "true"})
    assert resp.status_code == 200
    assert resp.json() == {"count": 1}


async def test_count_unseen_false(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "seen_item", "feed1", seen_at="2024-01-01T00:00:00")
    await _insert_item(db, "unseen_item", "feed1", seen_at=None)
    resp = await client.get("/api/items/count", params={"unseen": "false"})
    assert resp.status_code == 200
    assert resp.json() == {"count": 2}


async def test_count_feed_filter(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db, feed_id="feedA")
    await _insert_feed(db, feed_id="feedB")
    await _insert_item(db, "itemA", "feedA")
    await _insert_item(db, "itemB", "feedB")
    resp = await client.get("/api/items/count", params={"feed_id": "feedA"})
    assert resp.status_code == 200
    assert resp.json() == {"count": 1}
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_items_count.py -v`
Expected: FAIL with `404 Not Found` (route does not exist yet).

- [ ] **Step 3: Add the endpoint**

Add to `src/api/items.py` after the existing `list_items` route. Append:

```python
@router.get("/items/count")
async def count_items(
    unseen: bool = True,
    feed_id: str | None = None,
    db: _DbDep = None,  # type: ignore[assignment]
) -> dict[str, int]:
    """Return the total count of media items matching the filter.

    Defaults to unseen=true to match the frontend's default request. Used
    by the WebUI to populate the N / total counter and to detect "end of
    feed" without a separate count query per page.
    """
    conditions: list[str] = []
    params: list[Any] = []

    if unseen:
        conditions.append("seen_at IS NULL")
    if feed_id is not None:
        conditions.append("feed_id = ?")
        params.append(feed_id)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    query = f"SELECT COUNT(*) FROM items {where_clause}"
    async with db.execute(query, params) as cur:
        row = await cur.fetchone()
    return {"count": row[0]}
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/test_items_count.py -v`
Expected: 5 passed.

- [ ] **Step 5: Run lint**

Run: `uv run ruff check src/api/items.py tests/test_items_count.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/api/items.py tests/test_items_count.py
git commit -m "feat(api): add GET /api/items/count endpoint with tests"
```

---

## Task 3: Update `_build_html` in `src/main.py` to inject new CSS variables

**Files:**
- Modify: `src/main.py:30-41`

- [ ] **Step 1: Replace the `_build_html` function**

In `src/main.py`, replace the body of `_build_html` (lines 30–41) with:

```python
def _build_html() -> str:
    if not _index_path.exists():
        return ""
    style = (
        f"<style>:root{{"
        f"--feed-initial-count:{settings.feed_initial_count};"
        f"--video-buffer-threshold-pct:{settings.video_buffer_threshold_pct};"
        f"--video-buffer-threshold-min-s:{settings.video_buffer_threshold_min_s};"
        f"--image-autoscroll-delay-s:{settings.image_autoscroll_delay_s};"
        f"}}</style>"
    )
    return _index_path.read_text().replace("<!-- CONFIG_VARS -->", style)
```

Note: the placeholder marker changed from `<!-- SLIDESHOW_TRANSITION -->` to `<!-- CONFIG_VARS -->`. The new `index.html` (Task 4) will use the new marker.

- [ ] **Step 2: Verify the function compiles**

Run: `uv run python -c "from src.main import _build_html; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Run lint**

Run: `uv run ruff check src/main.py`
Expected: `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git add src/main.py
git commit -m "feat(main): inject webui config vars into html at startup"
```

---

## Task 4: Rewrite `src/static/index.html`

**Files:**
- Modify: `src/static/index.html`

- [ ] **Step 1: Replace `src/static/index.html`**

Overwrite the file with:

```html
<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <title>Media RSS Reader</title>
  <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
  <!-- CONFIG_VARS -->
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <div id="feed" aria-label="Media feed"></div>
  <div id="counter" aria-live="polite">— / —</div>
  <div id="controls">
    <button id="btn-autoscroll" type="button" aria-pressed="false" aria-label="Toggle autoscroll" title="Autoscroll [a]">⟳</button>
    <button id="btn-mute" type="button" aria-pressed="true" aria-label="Toggle mute" title="Mute [m]">🔇</button>
  </div>
  <script src="/static/item-store.js"></script>
  <script src="/static/cache-queue.js"></script>
  <script src="/static/feed-view.js"></script>
  <script src="/static/scroll-controller.js"></script>
  <script src="/static/autoscroll-controller.js"></script>
  <script src="/static/controls.js"></script>
  <script src="/static/app.js"></script>
</body>
</html>
```

Note: the JS files are loaded in dependency order. Each module attaches to `window.MRR` so the next module in the chain can reference it.

- [ ] **Step 2: Verify the file is well-formed**

Run: `uv run python -c "import html.parser; p = html.parser.HTMLParser(); p.feed(open('src/static/index.html').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add src/static/index.html
git commit -m "feat(webui): rewrite index.html for vertical-feed layout"
```

---

## Task 5: Rewrite `src/static/style.css`

**Files:**
- Modify: `src/static/style.css`

- [ ] **Step 1: Replace `src/static/style.css`**

Overwrite the file with:

```css
/* -----------------------------------------------------------------------
   Theme — dark only
   ----------------------------------------------------------------------- */
:root {
  --bg: #0d0d0f;
  --fg: #ececec;
  --fg-dim: #888;
  --accent: #6c8ebf;
  --spinner-track: #2a2a30;
  --spinner-head: #6c8ebf;
  --control-bg: rgba(20, 20, 28, 0.82);
  --control-border: rgba(255, 255, 255, 0.12);
  --control-icon: #d8d8e0;
  --control-active-bg: rgba(108, 142, 191, 0.55);
  --control-active-icon: #fff;
  --spinner-size: 36px;
  --spinner-border: 3px;
  --control-btn-size: 44px;
  --counter-size: 13px;
  --feed-initial-count: 10;
  --video-buffer-threshold-pct: 10;
  --video-buffer-threshold-min-s: 2;
  --image-autoscroll-delay-s: 2;
}

html[data-theme="dark"] {
  color-scheme: dark;
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  height: 100%;
  overflow: hidden;
}

/* -----------------------------------------------------------------------
   Feed — full-screen vertical scroll with snap
   ----------------------------------------------------------------------- */
#feed {
  height: 100vh;
  height: 100dvh;
  overflow-y: scroll;
  overflow-x: hidden;
  scroll-snap-type: y mandatory;
  scroll-behavior: smooth;
  -webkit-overflow-scrolling: touch;
}

@media (prefers-reduced-motion: reduce) {
  #feed { scroll-behavior: auto; }
}

.placeholder,
.media-item {
  height: 100vh;
  height: 100dvh;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  scroll-snap-align: start;
  scroll-snap-stop: always;
  position: relative;
  overflow: hidden;
}

.placeholder .spinner {
  width: var(--spinner-size);
  height: var(--spinner-size);
  border: var(--spinner-border) solid var(--spinner-track);
  border-top-color: var(--spinner-head);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

.media-item img,
.media-item video {
  max-width: 100%;
  max-height: 100%;
  width: auto;
  height: auto;
  object-fit: contain;
  display: block;
}

/* Hide broken media (e.g. transparent placeholder pixel). */
.media-item img[src=""],
.media-item video[src=""] { display: none; }

/* -----------------------------------------------------------------------
   Counter (N / total)
   ----------------------------------------------------------------------- */
#counter {
  position: fixed;
  left: 1rem;
  bottom: 1rem;
  z-index: 50;
  color: var(--fg-dim);
  font-size: var(--counter-size);
  font-variant-numeric: tabular-nums;
  user-select: none;
  pointer-events: none;
}

/* -----------------------------------------------------------------------
   Controls
   ----------------------------------------------------------------------- */
#controls {
  position: fixed;
  right: 1rem;
  bottom: 1rem;
  z-index: 50;
  display: flex;
  gap: 0.5rem;
  padding: 0.4rem;
  background: var(--control-bg);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--control-border);
  border-radius: 28px;
}

#controls button {
  width: var(--control-btn-size);
  height: var(--control-btn-size);
  border-radius: 50%;
  border: none;
  background: rgba(255, 255, 255, 0.08);
  color: var(--control-icon);
  font-size: 1.05rem;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s, color 0.15s;
}

#controls button[aria-pressed="true"] {
  background: var(--control-active-bg);
  color: var(--control-active-icon);
}

#controls button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
```

- [ ] **Step 2: Verify the file is valid CSS**

Run: `uv run python -c "import re; css = open('src/static/style.css').read(); n_open = css.count('{'); n_close = css.count('}'); assert n_open == n_close, f'{{={n_open} }}={n_close}'; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add src/static/style.css
git commit -m "feat(webui): rewrite style.css for vertical-feed layout"
```

---

## Task 6: Create `src/static/item-store.js`

**Files:**
- Create: `src/static/item-store.js`

- [ ] **Step 1: Write the module**

Create `src/static/item-store.js`:

```js
// ---------------------------------------------------------------------------
// itemStore
//
// Owns the list of items. Pulls metadata from /api/items (paginated) and
// /api/items/count. Exposes a small event-emitter API:
//   on('items-appended', cb)
//   on('currentindex-changed', cb)
//   getItems(), getCurrentIndex(), getTotal(), hasMoreItems(), getItemAt(idx),
//   findIndexById(id), setCurrentIndex(idx),
//   fetchPage(), fetchCount()
//
// No build step. Module attaches to window.MRR.itemStore.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    items: [],
    currentIndex: 0,
    total: 0,
    page: 0,
    hasMore: true,
    fetching: false,
  };

  const listeners = { "items-appended": [], "currentindex-changed": [] };

  function on(event, cb) {
    if (!listeners[event]) throw new Error("unknown event: " + event);
    listeners[event].push(cb);
  }
  function emit(event, ...args) {
    listeners[event].forEach((cb) => cb(...args));
  }

  async function fetchPage() {
    if (state.fetching || !state.hasMore) return;
    state.fetching = true;
    try {
      const cfg = MRR.config;
      const url = `/api/items?unseen=true&page=${state.page}&size=${cfg.feedInitialCount}`;
      const resp = await fetch(url);
      if (!resp.ok) return;
      const newItems = await resp.json();
      if (!newItems.length) {
        state.hasMore = false;
        return;
      }
      state.items = state.items.concat(newItems);
      state.page += 1;
      emit("items-appended", newItems);
    } finally {
      state.fetching = false;
    }
  }

  async function fetchCount() {
    const resp = await fetch("/api/items/count?unseen=true");
    if (!resp.ok) return 0;
    const data = await resp.json();
    state.total = data.count;
    return state.total;
  }

  function getItems() { return state.items; }
  function getCurrentIndex() { return state.currentIndex; }
  function getTotal() { return state.total; }
  function hasMoreItems() { return state.hasMore; }
  function getItemAt(idx) { return state.items[idx]; }
  function findIndexById(id) { return state.items.findIndex((i) => i.id === id); }

  function setCurrentIndex(idx) {
    if (idx === state.currentIndex) return;
    if (idx < 0 || idx >= state.items.length) return;
    state.currentIndex = idx;
    emit("currentindex-changed", idx);
  }

  MRR.itemStore = {
    on,
    getItems,
    getCurrentIndex,
    getTotal,
    hasMoreItems,
    getItemAt,
    findIndexById,
    setCurrentIndex,
    fetchPage,
    fetchCount,
  };
})();
```

- [ ] **Step 2: Manual smoke test (browser console)**

1. Start the server: `uv run uvicorn src.main:app --port 8080` (in a separate terminal).
2. Open `http://localhost:8080/` in a browser. Open DevTools console.
3. Type: `MRR.itemStore.getItems()` and press Enter. Expected: `[]` (no fetch yet).
4. Type: `MRR.itemStore.fetchPage()` and press Enter. Expected: a Promise.
5. After it resolves, type `MRR.itemStore.getItems()`. Expected: an array of items (or `[]` if the DB has none).
6. Verify no console errors.

- [ ] **Step 3: Commit**

```bash
git add src/static/item-store.js
git commit -m "feat(webui): add item-store module"
```

---

## Task 7: Create `src/static/cache-queue.js`

**Files:**
- Create: `src/static/cache-queue.js`

- [ ] **Step 1: Write the module**

Create `src/static/cache-queue.js`:

```js
// ---------------------------------------------------------------------------
// cacheQueue
//
// A priority-ordered queue of item IDs. A single worker downloads one media
// file at a time via /api/media/proxy?url=... and emits 'item-loaded' or
// 'item-failed' on completion. No concurrent downloads.
//
// Public API:
//   start(), stop()
//   rebuild(currentIndex, lookaheadN, items)
//   on('item-loaded', (id, el) => ...)
//   on('item-failed', (id) => ...)
//
// The worker is a single coroutine. We do not preempt in-flight downloads.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    queue: [],          // Array<string> item IDs in priority order
    loadingId: null,    // currently being downloaded
    running: false,
    cached: new Set(),  // IDs that have finished loading successfully
  };

  const listeners = { "item-loaded": [], "item-failed": [] };

  function on(event, cb) {
    if (!listeners[event]) throw new Error("unknown event: " + event);
    listeners[event].push(cb);
  }
  function emit(event, ...args) {
    listeners[event].forEach((cb) => cb(...args));
  }

  function priorityRebuild(currentIndex, lookaheadN, items) {
    const newQueue = [];
    if (currentIndex >= 0 && currentIndex < items.length) {
      newQueue.push(items[currentIndex].id);
    }
    const forward = items.slice(currentIndex + 1, currentIndex + 1 + lookaheadN);
    forward.forEach((it) => newQueue.push(it.id));
    const behind = items.slice(0, currentIndex).reverse();
    behind.forEach((it) => { if (!state.cached.has(it.id)) newQueue.push(it.id); });
    items.slice(currentIndex + 1 + lookaheadN).forEach((it) => newQueue.push(it.id));
    state.queue = newQueue.filter((id) => !state.cached.has(id));
  }

  async function processNext() {
    while (state.running && state.queue.length > 0) {
      const id = state.queue.shift();
      state.loadingId = id;
      try {
        const item = MRR.itemStore.getItems().find((i) => i.id === id);
        if (!item) { state.loadingId = null; continue; }
        const el = await downloadOne(item);
        state.cached.add(id);
        emit("item-loaded", id, el);
      } catch (err) {
        emit("item-failed", id);
      } finally {
        state.loadingId = null;
      }
    }
  }

  function downloadOne(item) {
    return new Promise((resolve, reject) => {
      const el = item.media_type === "video" ? document.createElement("video") : new Image();
      if (item.media_type === "video") {
        el.setAttribute("playsinline", "");
        el.setAttribute("webkit-playsinline", "");
        el.muted = true;
        el.preload = "auto";
      }
      el.addEventListener(item.media_type === "video" ? "loadeddata" : "load", () => resolve(el), { once: true });
      el.addEventListener("error", () => reject(new Error("media load failed")), { once: true });
      el.src = `/api/media/proxy?url=${encodeURIComponent(item.media_url)}`;
    });
  }

  function start() {
    if (state.running) return;
    state.running = true;
    processNext();
  }
  function stop() {
    state.running = false;
  }
  function rebuild(currentIndex, lookaheadN, items) {
    priorityRebuild(currentIndex, lookaheadN, items);
    if (state.running && state.loadingId === null) processNext();
  }
  function isCached(id) { return state.cached.has(id); }

  MRR.cacheQueue = { on, start, stop, rebuild, isCached };
})();
```

- [ ] **Step 2: Manual smoke test (browser console)**

1. Reload `http://localhost:8080/` in the browser. Open DevTools console.
2. Type: `MRR.itemStore.fetchCount().then(c => console.log('total', c))`. Expected: a number logged.
3. Type: `MRR.itemStore.fetchPage().then(() => { MRR.cacheQueue.rebuild(0, 10, MRR.itemStore.getItems()); MRR.cacheQueue.start(); })`. Expected: a Promise.
4. After it resolves, type `MRR.cacheQueue.isCached(MRR.itemStore.getItems()[0].id)`. Expected: `true` once the first media has loaded.
5. Verify no console errors.

- [ ] **Step 3: Commit**

```bash
git add src/static/cache-queue.js
git commit -m "feat(webui): add cache-queue module with single-worker downloader"
```

---

## Task 8: Create `src/static/feed-view.js`

**Files:**
- Create: `src/static/feed-view.js`

- [ ] **Step 1: Write the module**

Create `src/static/feed-view.js`:

```js
// ---------------------------------------------------------------------------
// feedView
//
// Renders the vertical scroll container. Each item in the feed is either
// a .placeholder (spinner) or a .media-item (img/video). On
// cacheQueue 'item-loaded', the placeholder is replaced with the loaded
// media element wrapped in a .media-item.
//
// The "visible media" rule: at most one <video> plays at a time. The visible
// video is the one with the highest intersectionRatio (set by the scroll
// controller). All other videos are paused and muted.
//
// Public API:
//   on('currentindex-changed', ...)  // forwards scroll-controller events
//   renderInitial(items)
//   createPlaceholder(item)            // exposed for app.js's "append more"
//   onItemLoaded(id, el)
//   onItemFailed(id)
//   snapToIndex(idx), snapToNext(), snapToPrev()
//   setCurrentMedia(el)
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    feed: null,
    currentVisibleEl: null,   // currently-playing <video> or null
    autoscrollBound: false,
  };

  const listeners = { "currentindex-changed": [] };
  function on(event, cb) {
    if (!listeners[event]) throw new Error("unknown event: " + event);
    listeners[event].push(cb);
  }
  function emit(event, ...args) { listeners[event].forEach((cb) => cb(...args)); }

  function createPlaceholder(item) {
    const wrap = document.createElement("div");
    wrap.className = "placeholder";
    wrap.dataset.id = item.id;
    const spinner = document.createElement("div");
    spinner.className = "spinner";
    wrap.appendChild(spinner);
    return wrap;
  }

  // Returns the forward-seconds buffered past the playhead, or null if the
  // buffer is empty / not yet reported. Used by the buffer-threshold logic.
  function forwardSeconds(video) {
    const b = video.buffered;
    if (!b.length) return 0;
    let total = 0;
    for (let i = 0; i < b.length; i++) {
      const start = b.start(i);
      const end = b.end(i);
      if (end >= video.currentTime) total += end - Math.max(start, video.currentTime);
    }
    return total;
  }

  function bufferedPct(video) {
    if (!video.duration || !isFinite(video.duration)) return 0;
    const b = video.buffered;
    if (!b.length) return 0;
    return (b.end(b.length - 1) / video.duration) * 100;
  }

  // Wait until the buffer reaches the configured threshold AND `video` is
  // the currently visible media, then play. The 'progress' event fires on
  // each buffer growth; we evaluate on each. On browsers where 'progress'
  // is sparse (notably iOS Safari), a 100ms setInterval re-evaluates the
  // same condition until the video starts. We also short-circuit if the
  // video stops being the visible one (e.g. the user scrolled away).
  function playWhenBufferedAndVisible(video) {
    const cfg = MRR.config;
    let intervalId = null;
    function clearAll() {
      if (intervalId !== null) { clearInterval(intervalId); intervalId = null; }
    }
    function evaluate() {
      if (state.currentVisibleEl !== video) { clearAll(); return; }
      const pct = bufferedPct(video);
      const fs = forwardSeconds(video);
      if (pct >= cfg.videoBufferThresholdPct || fs >= cfg.videoBufferThresholdMinS) {
        video.play().catch(() => {});
        clearAll();
        video.removeEventListener("progress", evaluate);
        video.removeEventListener("canplay", evaluate);
      }
    }
    video.addEventListener("progress", evaluate);
    video.addEventListener("canplay", evaluate);
    intervalId = setInterval(evaluate, 100);
    video.addEventListener("playing", clearAll, { once: true });
  }

  function createMediaWrap(item, el) {
    const wrap = document.createElement("div");
    wrap.className = "media-item";
    wrap.dataset.id = item.id;
    wrap.dataset.mediaType = item.media_type;
    if (item.media_type === "video") {
      el.setAttribute("playsinline", "");
      el.setAttribute("webkit-playsinline", "");
      el.muted = MRR.config.mutedDefault;
      el.loop = !MRR.config.autoscroll;
      // NOTE: do NOT set el.autoplay here. The visible-media rule drives
      // playback: setCurrentMedia calls playWhenBufferedAndVisible when
      // this video becomes the current visible one. Setting autoplay
      // would bypass that gate.
      el.addEventListener("error", () => onItemFailed(item.id));
    } else {
      el.addEventListener("error", () => onItemFailed(item.id));
    }
    wrap.appendChild(el);
    return wrap;
  }

  function renderInitial(items) {
    state.feed = document.getElementById("feed");
    items.forEach((it) => state.feed.appendChild(createPlaceholder(it)));
  }

  function onItemLoaded(id, el) {
    const placeholder = state.feed.querySelector(`.placeholder[data-id="${id}"]`);
    if (!placeholder) return; // placeholder already removed (e.g. scrolled past)
    const item = MRR.itemStore.getItems().find((i) => i.id === id);
    if (!item) return;
    const wrap = createMediaWrap(item, el);
    placeholder.replaceWith(wrap);
    MRR.scrollController.observe(wrap);
    MRR.autoscrollController.bindIfVisible(wrap);
  }

  function onItemFailed(id) {
    const el = state.feed.querySelector(`.placeholder[data-id="${id}"], .media-item[data-id="${id}"]`);
    if (!el) return;
    el.remove();
    const idx = MRR.itemStore.findIndexById(id);
    if (idx !== -1) MRR.itemStore.getItems().splice(idx, 1);
  }

  function snapToIndex(idx) {
    const items = state.feed.querySelectorAll(".placeholder, .media-item");
    if (items[idx]) items[idx].scrollIntoView({ block: "start" });
  }
  function snapToNext() {
    snapToIndex(MRR.itemStore.getCurrentIndex() + 1);
  }
  function snapToPrev() {
    snapToIndex(MRR.itemStore.getCurrentIndex() - 1);
  }

  function setCurrentMedia(el) {
    if (state.currentVisibleEl === el) return;
    if (state.currentVisibleEl && state.currentVisibleEl !== el) {
      state.currentVisibleEl.pause();
      state.currentVisibleEl.muted = true;
    }
    state.currentVisibleEl = el;
    if (el && el.tagName === "VIDEO") {
      el.muted = MRR.config.mutedDefault;
      playWhenBufferedAndVisible(el);
    }
  }

  MRR.feedView = {
    on,
    createPlaceholder,
    renderInitial,
    onItemLoaded,
    onItemFailed,
    snapToIndex,
    snapToNext,
    snapToPrev,
    setCurrentMedia,
  };
})();
```

- [ ] **Step 2: Manual smoke test**

1. Reload the browser page.
2. Verify the page renders 10 placeholder spinners (or fewer if the DB has fewer items).
3. As media loads, verify each placeholder is replaced with the actual image/video.
4. Scroll the page; verify the feed snaps to each item.
5. Check the DevTools console for errors.

- [ ] **Step 3: Commit**

```bash
git add src/static/feed-view.js
git commit -m "feat(webui): add feed-view module with placeholder/media swap"
```

---

## Task 9: Create `src/static/scroll-controller.js`

**Files:**
- Create: `src/static/scroll-controller.js`

- [ ] **Step 1: Write the module**

Create `src/static/scroll-controller.js`:

```js
// ---------------------------------------------------------------------------
// scrollController
//
// A single IntersectionObserver with threshold 0.6 tracks which item is
// currently most visible. On change, emits 'currentindex-changed' (which
// itemStore.setCurrentIndex and feedView.setCurrentMedia respond to) and
// triggers a cacheQueue.rebuild around the new position.
//
// Also owns the 'seen' observer: when an item scrolls fully past the top of
// the viewport, POST /api/items/{id}/seen so the scheduler can prune it.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    observer: null,
    seenObserver: null,
  };

  function init() {
    state.observer = new IntersectionObserver(onIntersect, {
      threshold: 0.6,
    });
    state.seenObserver = new IntersectionObserver(onSeen, { threshold: 0 });
  }

  function onIntersect(entries) {
    let best = null;
    let bestRatio = 0;
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      if (entry.intersectionRatio > bestRatio) {
        best = entry;
        bestRatio = entry.intersectionRatio;
      }
    });
    if (!best) return;
    const idx = MRR.itemStore.findIndexById(best.target.dataset.id);
    if (idx === -1) return;
    const prev = MRR.itemStore.getCurrentIndex();
    MRR.itemStore.setCurrentIndex(idx);
    if (idx !== prev) {
      MRR.feedView.setCurrentMedia(best.target.querySelector("video"));
      MRR.cacheQueue.rebuild(idx, MRR.config.feedInitialCount, MRR.itemStore.getItems());
      MRR.autoscrollController.reset(best.target);
    }
  }

  function onSeen(entries) {
    entries.forEach((entry) => {
      if (entry.isIntersecting) return;
      if (entry.boundingClientRect.bottom > 0) return;
      const id = entry.target.dataset.id;
      fetch(`/api/items/${id}/seen`, { method: "POST" }).catch(() => {});
    });
  }

  function observe(el) {
    state.observer.observe(el);
    state.seenObserver.observe(el);
  }

  MRR.scrollController = { init, observe };
})();
```

- [ ] **Step 2: Manual smoke test**

1. Reload the browser page.
2. Wait for media to start loading. After several items have loaded, scroll the page.
3. Verify `MRR.itemStore.getCurrentIndex()` updates as you scroll (type it in the console).
4. Verify the visible video changes (only one video plays at a time).
5. Verify the counter at the bottom-left updates `N / total` correctly.
6. Check the DevTools console for errors.

- [ ] **Step 3: Commit**

```bash
git add src/static/scroll-controller.js
git commit -m "feat(webui): add scroll-controller with intersectionobserver"
```

---

## Task 10: Create `src/static/autoscroll-controller.js`

**Files:**
- Create: `src/static/autoscroll-controller.js`

- [ ] **Step 1: Write the module**

Create `src/static/autoscroll-controller.js`:

```js
// ---------------------------------------------------------------------------
// autoscrollController
//
// When autoscroll is on, the visible item's media drives a snap-to-next:
//
//   - image:  setTimeout(IMAGE_AUTOSCROLL_DELAY_S)
//   - gif:    setTimeout(getGifDuration(src))
//   - video:  addEventListener('ended', ...) once; then snapToNext
//
// When the current item changes (scrollController fires), the timer is
// reset for the new visible item.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    autoscroll: false,
    boundItem: null,
    boundType: null,
    timerId: null,
    videoEndedHandler: null,
    onAutoscrollChanged: null,
  };

  function setAutoscroll(on) {
    state.autoscroll = on;
    document.querySelectorAll("#feed video").forEach((v) => { v.loop = !on; });
    if (on) {
      const current = currentVisibleWrap();
      if (current) bindIfVisible(current);
    } else {
      unbind();
    }
  }

  function currentVisibleWrap() {
    const idx = MRR.itemStore.getCurrentIndex();
    const items = document.querySelectorAll("#feed .media-item");
    return items[idx] || null;
  }

  function bindIfVisible(wrap) {
    if (!state.autoscroll) return;
    if (state.boundItem === wrap) return;
    unbind();
    state.boundItem = wrap;
    const type = wrap.dataset.mediaType;
    state.boundType = type;
    if (type === "video") {
      const v = wrap.querySelector("video");
      if (v) {
        state.videoEndedHandler = () => {
          if (state.boundItem === wrap) MRR.feedView.snapToNext();
        };
        v.addEventListener("ended", state.videoEndedHandler, { once: true });
      }
    } else if (type === "image") {
      const cfg = MRR.config;
      state.timerId = setTimeout(() => {
        if (state.boundItem === wrap) MRR.feedView.snapToNext();
      }, cfg.imageAutoscrollDelayMs);
    } else if (type === "gif") {
      getGifDuration(wrap.querySelector("img").src).then((ms) => {
        if (state.boundItem !== wrap) return;
        state.timerId = setTimeout(() => {
          if (state.boundItem === wrap) MRR.feedView.snapToNext();
        }, ms);
      });
    }
  }

  function reset(wrap) {
    if (state.autoscroll) bindIfVisible(wrap);
  }

  function unbind() {
    if (state.timerId !== null) { clearTimeout(state.timerId); state.timerId = null; }
    if (state.boundItem && state.boundType === "video" && state.videoEndedHandler) {
      const v = state.boundItem.querySelector("video");
      if (v) v.removeEventListener("ended", state.videoEndedHandler);
    }
    state.boundItem = null;
    state.boundType = null;
    state.videoEndedHandler = null;
  }

  function getGifDuration(url) {
    if (!url.startsWith("/api/media/proxy?")) return Promise.resolve(MRR.config.imageAutoscrollDelayMs);
    return fetch(url)
      .then((r) => r.arrayBuffer())
      .then((buf) => {
        const u = new Uint8Array(buf);
        let ms = 0;
        for (let i = 0; i + 5 < u.length; i++) {
          if (u[i] === 0x21 && u[i + 1] === 0xF9 && u[i + 2] === 0x04) {
            ms += (u[i + 4] + u[i + 5] * 256) * 10;
            i += 5;
          }
        }
        return ms > 0 ? Math.min(Math.max(ms, 50), 60000) : MRR.config.imageAutoscrollDelayMs;
      })
      .catch(() => MRR.config.imageAutoscrollDelayMs);
  }

  MRR.autoscrollController = { setAutoscroll, bindIfVisible, reset };
})();
```

- [ ] **Step 2: Manual smoke test**

1. Reload the browser page.
2. Press the autoscroll button (or `a` key). The button should become highlighted.
3. Verify the feed advances to the next item after the appropriate dwell (image: ~2s, video: on `ended`).
4. Press the autoscroll button again. The feed should stop advancing.
5. Check the DevTools console for errors.

- [ ] **Step 3: Commit**

```bash
git add src/static/autoscroll-controller.js
git commit -m "feat(webui): add autoscroll-controller with snap-to-next"
```

---

## Task 11: Create `src/static/controls.js`

**Files:**
- Create: `src/static/controls.js`

- [ ] **Step 1: Write the module**

Create `src/static/controls.js`:

```js
// ---------------------------------------------------------------------------
// controls
//
// Wires up the two control buttons (autoscroll, mute) and the counter.
//
// Mute is global: when toggled, every <video> in the feed gets el.muted
// set to the new value. The visible video continues to play (per the
// visible-media rule in feedView).
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    muted: true,
  };

  function setMuted(muted) {
    state.muted = muted;
    document.querySelectorAll("#feed video").forEach((v) => { v.muted = muted; });
    const btn = document.getElementById("btn-mute");
    btn.setAttribute("aria-pressed", String(muted));
    btn.textContent = muted ? "🔇" : "🔊";
  }

  function setAutoscroll(on) {
    MRR.autoscrollController.setAutoscroll(on);
    const btn = document.getElementById("btn-autoscroll");
    btn.setAttribute("aria-pressed", String(on));
  }

  function updateCounter() {
    const cur = MRR.itemStore.getCurrentIndex() + 1;
    const total = MRR.itemStore.getTotal();
    document.getElementById("counter").textContent = total > 0 ? `${cur} / ${total}` : "— / —";
  }

  function init() {
    document.getElementById("btn-autoscroll").addEventListener("click", () => {
      const next = document.getElementById("btn-autoscroll").getAttribute("aria-pressed") !== "true";
      setAutoscroll(next);
    });
    document.getElementById("btn-mute").addEventListener("click", () => {
      setMuted(!state.muted);
    });
    MRR.itemStore.on("items-appended", updateCounter);
    MRR.itemStore.on("currentindex-changed", updateCounter);
    setMuted(true);
    setAutoscroll(false);
    updateCounter();
  }

  MRR.controls = { init, setMuted, setAutoscroll, updateCounter };
})();
```

- [ ] **Step 2: Manual smoke test**

1. Reload the browser page.
2. Verify the mute button shows 🔇 and is highlighted.
3. Click mute. Expected: button changes to 🔊 and is no longer highlighted.
4. Click autoscroll. Expected: button highlights; feed starts advancing.
5. Verify the counter at bottom-left shows `N / total` and updates when you scroll.

- [ ] **Step 3: Commit**

```bash
git add src/static/controls.js
git commit -m "feat(webui): add controls module for autoscroll + mute"
```

---

## Task 12: Rewrite `src/static/app.js` — startup, keymap, wiring

**Files:**
- Modify: `src/static/app.js`

- [ ] **Step 1: Replace `src/static/app.js`**

Overwrite the file with:

```js
// ---------------------------------------------------------------------------
// app.js — startup, configuration, keymap, module wiring
//
// Reads config from CSS custom properties injected by the backend in
// src/main.py:_build_html. Initializes all modules in dependency order
// and kicks off the initial feed load.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  function readConfig() {
    const root = document.documentElement;
    const cs = getComputedStyle(root);
    const num = (name, fallback) => {
      const v = parseInt(cs.getPropertyValue(name).trim(), 10);
      return Number.isFinite(v) ? v : fallback;
    };
    MRR.config = {
      feedInitialCount: num("--feed-initial-count", 10),
      videoBufferThresholdPct: num("--video-buffer-threshold-pct", 10),
      videoBufferThresholdMinS: num("--video-buffer-threshold-min-s", 2),
      imageAutoscrollDelayMs: num("--image-autoscroll-delay-s", 2) * 1000,
      autoscroll: false,
      mutedDefault: true,
    };
  }

  function init() {
    readConfig();
    MRR.scrollController.init();
    MRR.controls.init();
    MRR.cacheQueue.on("item-loaded", (id, el) => MRR.feedView.onItemLoaded(id, el));
    MRR.cacheQueue.on("item-failed", (id) => MRR.feedView.onItemFailed(id));

    // Initial load: fetch total + first page, then render + start the queue.
    Promise.resolve()
      .then(() => MRR.itemStore.fetchCount())
      .then(() => MRR.itemStore.fetchPage())
      .then(() => {
        const items = MRR.itemStore.getItems();
        if (items.length === 0) {
          document.getElementById("counter").textContent = "0 / 0";
          return;
        }
        MRR.feedView.renderInitial(items);
        MRR.cacheQueue.rebuild(0, MRR.config.feedInitialCount, items);
        MRR.cacheQueue.start();
        MRR.controls.updateCounter();
      })
      .catch((err) => console.error("startup failed", err));

    // Observe placeholders so the very first currentIndex fires once they're mounted.
    const mo = new MutationObserver(() => {
      const placeholders = document.querySelectorAll("#feed .placeholder");
      if (placeholders.length === 0) return;
      placeholders.forEach((p) => MRR.scrollController.observe(p));
      mo.disconnect();
    });
    mo.observe(document.getElementById("feed"), { childList: true });

    // Keymap
    document.addEventListener("keydown", (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      switch (e.key) {
        case "j":
        case "ArrowDown":
          e.preventDefault();
          MRR.feedView.snapToNext();
          break;
        case "k":
        case "ArrowUp":
          e.preventDefault();
          MRR.feedView.snapToPrev();
          break;
        case "a":
          e.preventDefault();
          document.getElementById("btn-autoscroll").click();
          break;
        case "m":
          e.preventDefault();
          document.getElementById("btn-mute").click();
          break;
      }
    });

    // Periodic check to fetch more pages when nearing the end of the loaded list.
    setInterval(() => {
      const cur = MRR.itemStore.getCurrentIndex();
      const total = MRR.itemStore.getItems().length;
      if (MRR.itemStore.hasMoreItems() && total - cur < MRR.config.feedInitialCount) {
        MRR.itemStore.fetchPage().then(() => {
          const items = MRR.itemStore.getItems();
          const feed = document.getElementById("feed");
          const existing = new Set(
            Array.from(feed.children).map((el) => el.dataset.id)
          );
          items.forEach((it) => {
            if (existing.has(it.id)) return;
            const placeholder = MRR.feedView.createPlaceholder(it);
            feed.appendChild(placeholder);
            MRR.scrollController.observe(placeholder);
          });
        });
      }
    }, 2000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
```

- [ ] **Step 2: Manual end-to-end test**

1. Stop and restart the server (the old `_build_html` placeholder has changed).
2. Reload `http://localhost:8080/`. Verify:
   - Page loads with no console errors.
   - 10 placeholder spinners are visible.
   - As media loads, placeholders swap to images/videos.
   - Scrolling snaps to each item; counter updates.
   - `j`/`k`/arrow keys snap to next/prev.
   - `a` toggles autoscroll; feed advances on schedule.
   - `m` toggles mute; visible video audio responds.
   - When the end of loaded items approaches, more items are appended automatically.
3. Run lint: `uv run ruff check src/`. Expected: `All checks passed!` (ruff doesn't lint JS, but the Python should be clean).
4. Run the test suite: `uv run pytest tests/ -x`. Expected: all existing tests pass + the 5 new `/api/items/count` tests pass.

- [ ] **Step 3: Commit**

```bash
git add src/static/app.js
git commit -m "feat(webui): rewrite app.js with startup, keymap, and module wiring"
```

---

## Verification (after all tasks complete)

- [ ] **Run the full test suite**

Run: `uv run pytest`
Expected: all tests pass; coverage stays above 90%.

- [ ] **Run lint**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Run format**

Run: `uv run ruff format --check .`
Expected: no formatting changes needed (run `uv run ruff format .` to fix if not).

- [ ] **Manual browser smoke test**

1. Start: `uv run uvicorn src.main:app --port 8080`
2. Open `http://localhost:8080/` in a desktop browser and a mobile-sized browser window.
3. Verify all behaviors in the spec's behavior matrix.
4. Check the DevTools Network tab: confirm only one `/api/media/proxy` request is in flight at a time.
5. Check the DevTools Performance tab while scrolling: confirm scroll-snap fires cleanly.
6. Check the DevTools Console: no errors or warnings.

- [ ] **Final commit if any cleanup was needed**

```bash
git status
# If dirty:
git add -A
git commit -m "chore(webui): post-implementation cleanup"
```
