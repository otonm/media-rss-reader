# Feed Refresh Fix + Seen Items Toggle + Video Autoscroll Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix background feed refresh, add a seen-items toggle with ✓ badge, and improve video autoscroll to trigger at 50% visibility and scroll to top before playing.

**Architecture:** Three independent changes: (1) one-line scheduler fix, (2) frontend-only seen toggle wiring the existing `unseen` API param to a new button + CSS badge, (3) rework of the `mediaObserver` video path in `app.js` to scroll-then-play.

**Tech Stack:** Python 3.14, APScheduler 3.x (`AsyncIOScheduler`), FastAPI, Vanilla JS, SQLite

---

## File Map

| File | Change |
|------|--------|
| `src/scheduler.py` | Pass `event_loop` to `AsyncIOScheduler` |
| `tests/test_scheduler.py` | New — verify event loop is passed |
| `src/static/index.html` | Add eye button to `#controls` and `#fab-menu` |
| `src/static/style.css` | Add seen-badge CSS |
| `src/static/app.js` | Add `showSeen` state, `toggleShowSeen()`, update `fetchItems`, `createMediaEl`, `seenObserver`, `updateControls`, startup; change `mediaObserver` threshold + video scroll-to-top logic |

---

## Task 1: Fix Background Feed Refresh

**Files:**
- Modify: `src/scheduler.py:39`
- Create: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_scheduler.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_start_scheduler_passes_running_loop():
    """AsyncIOScheduler must receive the running event loop to fire jobs in FastAPI."""
    mock_instance = MagicMock()
    mock_instance.start = MagicMock()
    mock_instance.add_job = MagicMock()

    with (
        patch("src.scheduler.AsyncIOScheduler", return_value=mock_instance) as mock_cls,
        patch("src.scheduler.httpx.AsyncClient"),
        patch("src.scheduler.opml_sync", new=AsyncMock()),
        patch("src.scheduler.refresh_all_feeds", new=AsyncMock()),
        patch("src.scheduler.warm_startup_cache", return_value=asyncio.sleep(0)),
    ):
        from src.scheduler import start_scheduler

        mock_db = MagicMock()
        await start_scheduler(mock_db)

        loop = asyncio.get_running_loop()
        mock_cls.assert_called_once_with(event_loop=loop)
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
uv run pytest tests/test_scheduler.py -v
```

Expected: `FAILED — AssertionError: expected call with event_loop=...`

- [ ] **Step 3: Apply the fix**

In `src/scheduler.py`, change line 39:

```python
# before
_scheduler = AsyncIOScheduler()

# after
_scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop())
```

No other changes needed.

- [ ] **Step 4: Run the test to confirm it passes**

```bash
uv run pytest tests/test_scheduler.py -v
```

Expected: `PASSED`

- [ ] **Step 5: Run the full test suite**

```bash
uv run pytest
```

Expected: all tests pass, coverage ≥ 90%.

- [ ] **Step 6: Lint**

```bash
uv run ruff check src/scheduler.py tests/test_scheduler.py
```

Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/scheduler.py tests/test_scheduler.py
git commit -m "fix: pass running event loop to AsyncIOScheduler so background jobs fire"
```

---

## Task 2: Seen Items Toggle

**Files:**
- Modify: `src/static/index.html`
- Modify: `src/static/style.css`
- Modify: `src/static/app.js`

### 2a — Add the button to the HTML

- [ ] **Step 1: Add eye button to `#controls` and `#fab-menu`**

In `src/static/index.html`, replace the entire `<div id="controls">` block:

```html
  <div id="controls">
    <button class="ctrl-btn" id="ctrl-autoscroll" title="Auto-scroll [a]" onclick="toggleAutoScroll();updateControls()">⟳</button>
    <button class="ctrl-btn" id="ctrl-slideshow"  title="Slideshow [s]"   onclick="toggleSlideshow();updateControls()">⊞</button>
    <button class="ctrl-btn" id="ctrl-mute"       title="Mute [m]"        onclick="toggleMute();updateControls()">🔇</button>
    <button class="ctrl-btn" id="ctrl-show-seen"  title="Show seen"       onclick="toggleShowSeen()">👁</button>
    <button class="ctrl-btn" id="ctrl-theme"      title="Theme [d]"       onclick="toggleTheme();updateControls()">🌙</button>
  </div>
```

And replace the `<div id="fab-menu">` block:

```html
    <div id="fab-menu" class="hidden">
      <button class="fab-btn" id="fab-theme"      title="Theme"       onclick="toggleTheme();updateControls()">🌙</button>
      <button class="fab-btn" id="fab-mute"       title="Mute"        onclick="toggleMute();updateControls()">🔇</button>
      <button class="fab-btn" id="fab-show-seen"  title="Show seen"   onclick="toggleShowSeen()">👁</button>
      <button class="fab-btn" id="fab-slideshow"  title="Slideshow"   onclick="toggleSlideshow();updateControls()">⊞</button>
      <button class="fab-btn" id="fab-autoscroll" title="Auto-scroll" onclick="toggleAutoScroll();updateControls()">⟳</button>
    </div>
```

- [ ] **Step 2: Open the app and confirm the eye button is visible**

```bash
uv run uvicorn src.main:app --reload --port 8080
```

Open http://localhost:8080. Confirm 👁 button appears in the control bar (desktop) and FAB menu (mobile). No functionality yet.

### 2b — Add the seen badge CSS

- [ ] **Step 3: Add badge styles to `style.css`**

Append to the end of `src/static/style.css`:

```css
/* -----------------------------------------------------------------------
   Seen badge (visible only in show-all mode)
   ----------------------------------------------------------------------- */
#feed-list.feed-list--show-all .media-item.seen::after {
  content: "✓";
  position: absolute;
  top: 4px;
  right: 4px;
  width: 18px;
  height: 18px;
  background: rgba(0, 0, 0, 0.6);
  color: #fff;
  border-radius: 50%;
  font-size: 11px;
  line-height: 18px;
  text-align: center;
  z-index: 2;
  pointer-events: none;
}
```

### 2c — Wire up the JS

All changes in this step are to `src/static/app.js`.

- [ ] **Step 4: Add `showSeen` state variable**

In the state block (after line 19, `let muted = true;`), add:

```javascript
let showSeen = localStorage.getItem("showSeen") === "true";
```

- [ ] **Step 5: Change `fetchItems` to use the `showSeen` flag**

Replace line 81:

```javascript
// before
    const resp = await fetch(`/api/items?unseen=false&page=${page}&size=50`);

// after
    const resp = await fetch(`/api/items?unseen=${showSeen ? "false" : "true"}&page=${page}&size=50`);
```

- [ ] **Step 6: Apply `seen` class when creating media elements**

In `createMediaEl()`, after `wrap.dataset.id = item.id;` (line 37), add:

```javascript
  if (item.seen_at) wrap.classList.add("seen");
```

- [ ] **Step 7: Apply `seen` class when the seenObserver marks an item**

In the `seenObserver` callback, replace the `.then(data => ...)` chain (lines 149-151):

```javascript
    .then(r => r.json())
    .then(data => {
      item.seen_at = data.seen_at;
      entry.target.classList.add("seen");
    })
```

- [ ] **Step 8: Add `toggleShowSeen()` function**

Add this function after `toggleTheme()` (after line 335):

```javascript
// ---------------------------------------------------------------------------
// 10b. Show-seen toggle
// ---------------------------------------------------------------------------

function toggleShowSeen() {
  showSeen = !showSeen;
  localStorage.setItem("showSeen", showSeen ? "true" : "false");
  document.getElementById("feed-list").classList.toggle("feed-list--show-all", showSeen);
  items = [];
  currentIndex = 0;
  page = 0;
  document.getElementById("feed-list").innerHTML = "";
  document.getElementById("empty-state").classList.add("hidden");
  fetchItems();
  updateControls();
}
```

- [ ] **Step 9: Update `updateControls()` to sync the new buttons**

In `updateControls()`, add after the existing `ctrl-theme` line (after line 389):

```javascript
  document.getElementById("fab-show-seen").classList.toggle("active", showSeen);
  document.getElementById("ctrl-show-seen").classList.toggle("active", showSeen);
```

- [ ] **Step 10: Apply `feed-list--show-all` class on startup if `showSeen` is already true**

In the startup section (after `updateControls();` on line 441), add:

```javascript
// Restore show-all badge visibility if showSeen was persisted
if (showSeen) document.getElementById("feed-list").classList.add("feed-list--show-all");
```

- [ ] **Step 11: Manual verification**

With the dev server running:

1. Open http://localhost:8080. Confirm items load (unseen only — default).
2. Scroll past a few items. Reload page — confirm seen items are gone.
3. Click 👁 — confirm button highlights blue, page re-fetches and shows all items.
4. Confirm seen items show a ✓ badge in the top-right corner.
5. Click 👁 again — confirm button unhighlights, page re-fetches unseen-only, badges disappear.
6. Reload page with 👁 active — confirm state is restored from localStorage.

- [ ] **Step 12: Lint**

```bash
uv run ruff check src/static/
```

Expected: no errors (ruff skips JS/HTML — this is just a sanity check; no linting needed for static files).

```bash
uv run ruff check src/ tests/
```

Expected: no Python errors.

- [ ] **Step 13: Commit**

```bash
git add src/static/index.html src/static/style.css src/static/app.js
git commit -m "feat: add seen-items toggle with checkmark badge (default unseen-only feed)"
```

---

## Task 3: Video Autoscroll Improvements

**Files:**
- Modify: `src/static/app.js`

The `mediaObserver` currently triggers at 85% visibility and plays the video in place. This task: trigger at 50%, scroll the video wrapper to the top of `#scroll-view`, wait for the scroll to settle, then play.

- [ ] **Step 1: Replace the `mediaObserver` block**

In `src/static/app.js`, replace the entire `mediaObserver` block (lines 169–223) with:

```javascript
// ---------------------------------------------------------------------------
// 5c. Media observer — autoplay videos; pause/resume auto-scroll for media
// ---------------------------------------------------------------------------
const mediaObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    const el = entry.target;
    const isVideo = el.tagName === "VIDEO";
    const isGif = el.tagName === "IMG" && el.dataset.type === "gif";

    if (entry.isIntersecting) {
      // Auto-scroll: pause drift for video or GIF, then handle each type
      if (autoScroll && !autoScrollPaused && (isVideo || isGif)) {
        autoScrollPaused = true;
        stopAutoScroll();

        if (isVideo) {
          // Scroll video to top of viewport, then play once scroll settles
          const wrap = el.closest(".media-item");
          const scrollView = document.getElementById("scroll-view");
          wrap.scrollIntoView({ behavior: "smooth", block: "start" });

          let scrollFallbackTimer;
          const onScrollEnd = () => {
            clearTimeout(scrollFallbackTimer);
            el.play().catch(() => {});
            el.addEventListener("ended", () => {
              advance(1);
              autoScrollPaused = false;
              if (autoScroll) startAutoScroll();
            }, { once: true });
          };
          scrollFallbackTimer = setTimeout(onScrollEnd, 300);
          scrollView.addEventListener("scrollend", onScrollEnd, { once: true });
        } else {
          // GIF: parse duration (cached on item object) then advance
          const item = items.find(
            i => el.getAttribute("src") === `/api/media/proxy?url=${encodeURIComponent(i.media_url)}`
          );
          const resume = (duration) => {
            setTimeout(() => {
              advance(1);
              autoScrollPaused = false;
              if (autoScroll) startAutoScroll();
            }, duration);
          };
          if (!item) {
            resume(IMAGE_DELAY_MS);
          } else if (item.gifDuration) {
            resume(item.gifDuration);
          } else {
            const fetchUrl = `/api/media/proxy?url=${encodeURIComponent(item.media_url)}`;
            getGifDuration(fetchUrl).then(duration => {
              item.gifDuration = duration;
              resume(duration);
            });
          }
        }
      } else if (isVideo) {
        // Not in autoscroll mode — play normally when in view
        el.play().catch(() => {});
      }
    } else {
      // Pause videos when out of view
      if (isVideo) el.pause();
    }
  });
}, { threshold: 0.5 });
```

Key changes from the original:
- Threshold: `0.85` → `0.5`
- Video in autoscroll: `scrollIntoView({ behavior: "smooth", block: "start" })` + `scrollend`/timeout gate before `el.play()`
- Non-autoscroll video play moved to `else if (isVideo)` branch (removes the redundant double-play)

- [ ] **Step 2: Manual verification**

With the dev server running and a feed containing videos:

1. Enable autoscroll (`a` key).
2. Scroll until a video is encountered.
3. Confirm: video triggers when approximately half-visible (not 85%).
4. Confirm: the page scrolls the video to the top of the viewport before it starts playing.
5. Confirm: autoscroll pauses while the video plays.
6. Confirm: autoscroll resumes automatically after the video ends.
7. Disable autoscroll and confirm videos still play when scrolled into view normally.

- [ ] **Step 3: Lint**

```bash
uv run ruff check src/ tests/
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add src/static/app.js
git commit -m "feat: video autoscroll triggers at 50% visibility and scrolls to top before playing"
```

---

## Verification Summary

| Feature | How to verify |
|---------|--------------|
| Feed refresh | Set `FEED_REFRESH_INTERVAL=60` in `.env`, restart, wait 1 min, check `last_fetched_at` in `/api/feeds` |
| Seen toggle default | Load app fresh, scroll, reload — seen items gone |
| Seen toggle on | Click 👁 — all items appear with ✓ badges on seen ones |
| Seen toggle persistence | Toggle on, reload — still on |
| Video autoscroll trigger | Enable autoscroll, observe video triggers at ~50% not 85% |
| Video scroll to top | Confirm video snaps to viewport top before playing |
| Video resume | Autoscroll resumes after video ends |
