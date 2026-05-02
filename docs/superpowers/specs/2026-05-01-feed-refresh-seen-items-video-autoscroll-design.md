# Design: Feed Refresh Fix, Seen Items Toggle, Video Autoscroll

Date: 2026-05-01

## Overview

Three independent improvements to the media RSS reader:

1. Fix background feed refresh (scheduler bug)
2. Seen items tracking with show/hide toggle
3. Video autoscroll: trigger at 50% visibility, scroll to top, then play

---

## Feature 1: Background Feed Refresh Fix

### Problem

Feeds never refresh automatically after the initial startup sync. The scheduler appears to run (no errors) but jobs silently do nothing.

### Root Cause

`AsyncIOScheduler()` is constructed without an explicit event loop reference. In Python 3.14, the scheduler fails to attach to FastAPI's running asyncio event loop, so interval jobs are registered but never fire.

### Fix

In `src/scheduler.py`, pass the running loop when constructing the scheduler:

```python
# before
_scheduler = AsyncIOScheduler()

# after
_scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop())
```

`asyncio.get_running_loop()` is called from within `start_scheduler()` (an async function), so the running loop is always available at that point.

### Files Changed

- `src/scheduler.py` — one-line fix in `start_scheduler()`

---

## Feature 2: Seen Items Toggle

### Behavior

**Default (unseen-only mode):**
- Fetch `/api/items?unseen=true` — only items with `seen_at IS NULL` are returned.
- Items are marked seen automatically as the user scrolls past them (existing IntersectionObserver at 80% threshold, existing `POST /api/items/{id}/seen` endpoint — no changes needed).
- On page reload, seen items are not shown again.

**Toggle (show-all mode):**
- A 5th button (SVG eye icon, same style as existing control buttons) is added to the desktop control bar and mobile FAB menu.
- When activated: fetch `/api/items?unseen=false`, show everything including already-seen items.
- When deactivated: revert to `unseen=true`.
- Toggle state persists in `localStorage("showSeen")` and is restored on page load.

**On toggle transition:**
1. Flip the `showSeen` boolean.
2. Persist to `localStorage`.
3. Clear the `items` array, reset `page = 0`.
4. Re-fetch from page 0 with the updated `unseen` param.
5. Re-render the feed list.

### Seen Badge (show-all mode only)

When show-all mode is active, items that have a non-null `seen_at` value get a `seen` CSS class on their `.media-item` wrapper.

The badge is rendered via CSS only:
- Small circle (16×16px), positioned absolute at top-right of the media thumbnail.
- Semi-transparent dark background, white ✓.
- Hidden when show-all mode is off. `#feed-list` gets class `feed-list--show-all` while the toggle is active; the badge is only shown via `.feed-list--show-all .media-item.seen::after`.

### Seen State: How It Works

- **Persistent storage:** SQLite `items.seen_at` column. `NULL` = unseen; a datetime = seen.
- **Marking seen:** Existing `IntersectionObserver` at 80% viewport visibility fires `POST /api/items/{id}/seen` once per item. The timestamp is stored locally on the item object to prevent re-posting.
- **No changes** to the seen-marking mechanism — only the fetch default and toggle UI are new.

### Files Changed

- `src/static/app.js` — change default fetch param, add toggle handler, add seen badge class logic
- `src/static/style.css` — add `.media-item.seen::after` badge styles, add `.feed-list--show-all` modifier
- `src/static/index.html` — add eye icon button to desktop control bar and mobile FAB

---

## Feature 3: Video Autoscroll Improvements

### Current Behavior

- `mediaObserver` threshold: `0.85` — video must be 85% visible before triggering.
- On trigger during autoscroll: RAF loop stops, video plays in place (wherever it happens to be in the viewport — typically mid-screen).
- On `ended`: `advance(1)`, resume autoscroll.

### Desired Behavior

1. **Trigger at 50% visibility** — video starts the sequence sooner.
2. **Scroll video to top of viewport** — after triggering, scroll the video's `.media-item` to `block: "start"` before playing.
3. **Wait for scroll to settle** — use `scrollend` event on `#scroll-view` if supported, otherwise `setTimeout(300)` fallback.
4. **Play video** — after scroll settles.
5. **Pause/resume** — unchanged: autoscroll pauses on trigger, resumes on `ended`.

### Implementation

In the `mediaObserver` callback, for the video-during-autoscroll path:

```
threshold: 0.5  (was 0.85)

on intersect (isVideo && autoScroll && !autoScrollPaused):
  autoScrollPaused = true
  stopAutoScroll()
  item.closest(".media-item").scrollIntoView({ behavior: "smooth", block: "start" })
  scrollView.addEventListener("scrollend", onScrollEnd, { once: true })
  // fallback:
  scrollFallbackTimer = setTimeout(onScrollEnd, 300)

onScrollEnd:
  clearTimeout(scrollFallbackTimer)
  el.play().catch(() => {})
  el.addEventListener("ended", onEnded, { once: true })

onEnded:
  advance(1)
  autoScrollPaused = false
  if (autoScroll) startAutoScroll()
```

GIF handling is unchanged.

### Files Changed

- `src/static/app.js` — modify `mediaObserver` threshold and video trigger logic

---

## Verification

1. **Scheduler:** Restart the app, wait >15 minutes (or reduce `FEED_REFRESH_INTERVAL` to 60s via env var), confirm feed `last_fetched_at` timestamps update in `/api/feeds`.
2. **Seen items:** Scroll through items — confirm they disappear on reload. Toggle "show all" — confirm seen items reappear with ✓ badge. Toggle off — confirm badge and seen items hide.
3. **Video autoscroll:** Enable autoscroll, scroll to a feed with videos. Confirm video triggers at ~50% visibility, scrolls to top, plays, then autoscroll resumes.
