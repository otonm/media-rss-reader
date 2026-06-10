# WebUI Redesign — Vertical Feed with Priority Cache Queue

**Date:** 2026-06-10
**Status:** Approved design, pending implementation
**Scope:** Full rewrite of the frontend (`src/static/*`). Backend, auth, DB, OPML sync, scheduler, and media proxy stay as-is, except for the additions noted in the "Configuration" and "API changes" sections below.

## Problem

The current WebUI is a generic paginated list with several view modes (scroll, slideshow, show-seen) and a desktop icon bar + mobile FAB. It is more configuration surface than the user wants and lacks the TikTok-style continuous vertical feed the new spec calls for. The user wants a simpler, more focused feed with a strict "one media file loading at a time" cache policy and a priority-based prefetch queue.

## Goals

- Vertical, full-screen snap-scroll feed: one item per viewport.
- Reorderable cache queue that loads media files one at a time in priority order: visible → forward lookahead → reverse-behind → remaining forward.
- Snap-to-next autoscroll with image / GIF / video-specific dwell rules.
- Global mute toggle, always bound to the visible video.
- Minimal controls: autoscroll toggle, mute toggle. No theme toggle, no slideshow, no show-seen toggle, no FAB.
- New configuration knobs: `FEED_INITIAL_COUNT`, `VIDEO_BUFFER_THRESHOLD_PCT`, `VIDEO_BUFFER_THRESHOLD_MIN_S`, `IMAGE_AUTOSCROLL_DELAY_S`.

## Non-Goals

- Backend, auth, DB, OPML sync, scheduler, media proxy — all stay unchanged in behavior.
- Theme / dark-light toggle. The new UI is dark-only.
- Slideshow mode (the A/B crossfade view).
- Show-seen toggle and seen badge UI. The DB still records `seen_at` via the existing endpoint, but the UI does not differentiate seen from unseen in this rewrite. (The unseen filter on `/api/items` remains the default.)
- **Seen tracking behavior.** The frontend still calls `POST /api/items/{id}/seen` for each item once it scrolls fully past the top of the viewport (preserving the existing item-retention contract: the scheduler prunes old `seen_at`-set rows after `items_max_age_hours`). No visible badge or seen/unseen state in the UI; the call is purely a backend hygiene signal.
- Eviction of items from the DOM. Per spec, "already-cached items are never evicted."
- JS unit tests. The project has no JS test framework; lint via `ruff` for any Python and manual browser testing for the frontend (consistent with prior WebUI work).
- Preempting an in-flight download. Strict "one at a time" means the worker finishes its current item before re-evaluating the queue.

## Design

### 1. Architecture

Three independent layers with narrow, event-based interfaces:

- **`itemStore`** — Owns the list of items. Pulls metadata from `/api/items?page=X&size=Y` and `/api/items/count`. Exposes `items[]`, `currentIndex`, `setCurrentIndex(idx)`, `appendNextPage()`. Emits `items-appended` and `currentindex-changed`.
- **`cacheQueue`** — A priority-ordered list of item IDs. A single worker downloads one media file at a time via `/api/media/proxy`. Exposes `start()`, `stop()`, `rebuild(currentIndex, lookaheadN, items)`. Emits `item-loaded(id, mediaEl)` and `item-failed(id)`.
- **`feedView`** — Renders the vertical scroll container with CSS scroll-snap. Renders placeholders for unloaded items, swaps to media items on `item-loaded`. Exposes `snapToIndex(idx)`, `snapToNext()`, `snapToPrev()`.

Two controllers wire the layers:

- **`scrollController`** — IntersectionObserver with `threshold: 0.6` on each item wrap. Determines the item with the highest `intersectionRatio` and emits `currentindex-changed` on change. Calls `cacheQueue.rebuild()` and `itemStore.setCurrentIndex()`.
- **`autoscrollController`** — Listens for `image-delay-elapsed`, `gif-cycle-ended`, and `video-ended` events from the visible media. Each event calls `feedView.snapToNext()`. Toggling autoscroll on enables a global flag; the controller binds/unbinds its listeners accordingly.

### 2. Cache queue mechanics

**Structure.** A plain `Array<itemId>`. Head is the next item to load. A single async worker processes the head.

**Worker loop.**
1. If queue is empty, wait for a `rebuild` event.
2. Pop head, set `loadingItemId`.
3. Create a fresh off-DOM element: `<video>` for `media_type === "video"`, `new Image()` for `image` and `gif`.
4. Set `src = /api/media/proxy?url=...`.
5. On `load` / `loadeddata`: emit `item-loaded(loadingItemId, el)`, mark the item `_cached = true`, clear `loadingItemId`, loop to step 1.
6. On `error`: emit `item-failed(loadingItemId)`, clear `loadingItemId`, loop to step 1.

**Strict one-at-a-time.** The worker is a single coroutine. We do not speculatively start the next download before the current one finishes.

**Priority rebuild** (called whenever `currentIndex` changes):

```
queue = []
queue.push(items[currentIndex].id)                                       // 1. visible
forward = items.slice(currentIndex + 1, currentIndex + 1 + N)           // 2. forward lookahead
forward.forEach(it => queue.push(it.id))
behind = items.slice(0, currentIndex).reverse()                         // 3. reverse-behind (uncached only)
behind.filter(it => !it._cached).forEach(it => queue.push(it.id))
queue.push(...items.slice(currentIndex + 1 + N).map(it => it.id))        // 4. forward beyond N
```

The queue is rebuilt in place; the `loadingItemId` is preserved in its current position if it is still in the new queue, so the in-flight download is not aborted. Items that were already `_cached` are not re-added.

**Promotion on scroll-back.** When the user scrolls back to a previously-seen item, that item's ID is already at position 0 of the rebuilt queue (it became the new "visible" item). If the item was *beyond* the previous forward window, the rebuild still includes it as the new visible. The worker is not preempted.

**No eviction.** Items already added to the DOM stay there. The cache queue only governs download ordering.

### 3. Rendering and layout

- `#feed` is `height: 100vh; overflow-y: scroll; scroll-snap-type: y mandatory;`.
- Each item wrap is `height: 100vh; scroll-snap-align: start;` and centered with flex.
- Placeholders and media items share the same wrap dimensions. A `.placeholder` is a wrap with only a centered spinner. A `.media-item` is a wrap with the real `<img>` / `<video>` element. Pattern reuses the round-2 mobile-video-scroll-fix design (`createPlaceholder`, `createMediaItem`).
- The media element is `object-fit: contain` and max-sizes to viewport.

**Initial state.**
- Fetch first page: `GET /api/items?unseen=true&page=0&size=FEED_INITIAL_COUNT`.
- Render `FEED_INITIAL_COUNT` placeholder wraps in `#feed`.
- Call `cacheQueue.start()`.
- Audio: muted. Autoscroll: disabled. These default to `true` / `false` in code and are not persisted across reloads in this rewrite.

### 4. Visible-media rule

At most one `<video>` plays at a time. The visible video is the one whose wrap intersects the viewport at `intersectionRatio >= 0.6`. All other `<video>` elements have `pause()` called and remain muted. The muted flag is the global one; audio plays only on the visible video and only when not muted globally.

When a video scrolls out of view, it is paused immediately and muted. No background playback.

### 5. Image / GIF / video behavior

**Image.**
- Placeholder → swap to `<img>` on `load` → no automatic playback.
- Autoscroll: advance after `IMAGE_AUTOSCROLL_DELAY_S` seconds from when the image becomes visible.

**GIF.**
- Placeholder → swap to `<img>` on `load` → loops natively (browsers loop GIFs by default).
- Autoscroll: advance after one full loop. We reuse the existing `getGifDuration` helper (GIF-header scan) to compute loop length. If the duration cannot be determined, fall back to `IMAGE_AUTOSCROLL_DELAY_S`.
- The first loop is measured from the time the GIF becomes visible.

**Video.**
- Placeholder → on `loadeddata`, swap to a `<video>` with `preload="auto"`, `playsinline`, `webkit-playsinline`, `muted=true` (always muted on creation; global mute flag controls whether the visible video can unmute).
- `poster` is left empty initially; the first frame is shown via the natural browser behavior of a `<video>` whose `currentTime` is 0 and which has not yet received sufficient data to play. A spinner overlays the wrap until the buffer threshold is met.
- `loop`: `false` while autoscroll is on (so `ended` fires once and triggers advance); `true` while autoscroll is off (so the video loops in manual mode).
- `autoplay`: `true`. The video will play as soon as the buffer threshold is met.
- Autoscroll: advance on the first `ended` event.

**Buffer-threshold play.** The video's `progress` event fires each time the buffer grows. On each fire (and on `canplay` as a fast path), compute:
- `forwardSeconds = buffered.end(timeRange) - currentTime` (seconds of forward buffer from the playhead).
- `pct = buffered.end(lastRange) / duration * 100` (percent of the whole video buffered).

Play when **either** condition is met: `pct >= VIDEO_BUFFER_THRESHOLD_PCT` **or** `forwardSeconds >= VIDEO_BUFFER_THRESHOLD_MIN_S`. The minimum-seconds check overrides the percent check: if `VIDEO_BUFFER_THRESHOLD_MIN_S` is larger than the seconds equivalent of `VIDEO_BUFFER_THRESHOLD_PCT` for a given video length, the seconds check wins; otherwise the percent check is the binding constraint. Both are evaluated on every progress event.

iOS Safari caveat: `progress` events are sometimes sparse. A `setInterval(100ms)` fallback recomputes the same condition until the video starts playing; it is cleared on `playing`.

### 6. Autoscroll

- Global flag, default `false`. Toggled by the autoscroll button or a key binding.
- When `true`, the `autoscrollController` is bound to the visible media. It listens for:
  - `image-delay-elapsed` — fired by an image after it has been visible for `IMAGE_AUTOSCROLL_DELAY_S`.
  - `gif-cycle-ended` — fired by a GIF after one full loop from the time it became visible.
  - `video-ended` — fired by a video on the first `ended` event.
- On any of these, the controller calls `feedView.snapToNext()`, which uses `element.scrollIntoView({ behavior: 'smooth' })`; CSS scroll-snap aligns to the next item.
- When the IntersectionObserver fires with a new `currentIndex`, the controller resets its timers for the new visible item (e.g., the GIF/loop counter starts from zero, the image delay timer starts fresh).
- Toggling autoscroll on applies to all subsequent items, not just the one currently visible.
- All visible videos have their `loop` attribute updated when the flag changes.

### 7. Mute

Single global boolean, default `true` (muted). Toggled by the mute button or a key binding. The flag is stored only in memory; not persisted.

The global flag applies to **all** videos in the DOM at all times. When `globalMuted` is `true`, every `<video>` element has `el.muted = true`. When `false`, every `<video>` element has `el.muted = false`. The visible-media rule (only one video plays at a time) is independent of the mute flag — the visible video is the only one that can be `el.play()`-ing, but its `muted` is bound to the global flag.

Toggling the mute flag walks all `<video>` elements in the DOM and updates each `el.muted` accordingly. No element is exempt.

### 8. Key bindings

| Key | Action |
|---|---|
| `j` / `↓` | Next item |
| `k` / `↑` | Previous item |
| `a` | Toggle autoscroll |
| `m` | Toggle mute |

The keys are bound to `document` and ignored when focus is in an input or textarea (preserved from the prior design for keyboard accessibility).

### 9. Controls UI

Two fixed-position icon buttons at the bottom-right of the viewport (single position; no FAB / no separate desktop bar). Active state is visually distinct (filled vs. outlined). A small text counter at bottom-left shows the 1-indexed current position and the total (e.g. `3 / 47`) and updates as `currentIndex` and `total` change. The counter is a passive read-out, not interactive.

## Configuration

**New env vars** (added to `src/config.py`):

| Variable | Default | Description |
|---|---|---|
| `FEED_INITIAL_COUNT` | `10` | Initial placeholders + forward lookahead window size |
| `VIDEO_BUFFER_THRESHOLD_PCT` | `10` | Percent of video buffered before playback starts |
| `VIDEO_BUFFER_THRESHOLD_MIN_S` | `2` | Minimum seconds of forward buffer (overrides pct if larger) |
| `IMAGE_AUTOSCROLL_DELAY_S` | `2` | Image dwell time in autoscroll mode |

**Removed env vars** (no longer read by the frontend; deleted from `src/config.py`):

- `IMAGE_DISPLAY_DELAY_MS` (replaced by `IMAGE_AUTOSCROLL_DELAY_S`).
- `PREFETCH_AHEAD` (replaced by `FEED_INITIAL_COUNT`).
- `SLIDESHOW_TRANSITION_MS` (no slideshow).
- `AUTO_SCROLL_SPEED` (no continuous scroll; autoscroll is snap-to-next).

**Frontend-injected CSS variables** (in `src/main.py:_build_html`):

- `--feed-initial-count: N`
- `--video-buffer-threshold-pct: N`
- `--video-buffer-threshold-min-s: N`
- `--image-autoscroll-delay-s: N`

These are read by the JS at startup (or directly in CSS for animation durations).

## API changes

**New endpoint** — `GET /api/items/count?unseen=true` returning `{count: int}`.

Defaults: `unseen` defaults to `true` (matches the frontend's default request). The endpoint accepts the same `feed_id` filter as `list_items` for completeness, but the frontend does not currently use it.

Reuses the same `WHERE` clause as the existing `list_items` route. Implemented in `src/api/items.py`. Used by the frontend to populate the `N / total` counter and to detect "end of feed" without a separate count query per page.

No other backend changes. The `POST /api/prefetch/hint` endpoint stays (the URL is harmless; the new frontend simply does not call it).

## Files to change

- `src/static/index.html` — full rewrite.
- `src/static/app.js` — full rewrite. Module structure: `itemStore`, `cacheQueue`, `feedView`, `scrollController`, `autoscrollController`, `controls`, plus a small `keymap`.
- `src/static/style.css` — full rewrite. CSS variables, scroll-snap layout, dark theme.
- `src/config.py` — add 4 new settings; remove 4 unused.
- `src/main.py` — update `_build_html()` to inject only the new CSS variables.
- `src/api/items.py` — add `/items/count` route.
- `tests/test_api.py` — add tests for `/api/items/count` (unseen filter, pagination-independent).

## Behavior matrix

| Scenario | Behavior |
|---|---|
| Initial load | Render N placeholders, start cache queue, audio muted, autoscroll off |
| Item 1 finishes caching | Placeholder swaps to media item (image/gif/video) |
| User scrolls to item K | IntersectionObserver updates currentIndex; cache queue rebuilds; visible media takes priority |
| User scrolls to item that was not cached | That item is promoted to queue head; placeholder remains until the worker reaches it; then swaps |
| Autoscroll off, video visible | Video plays and loops (`loop=true`) |
| Autoscroll on, image visible | Advance to next after `IMAGE_AUTOSCROLL_DELAY_S` |
| Autoscroll on, GIF visible | Advance to next after one full GIF loop |
| Autoscroll on, video visible | Advance to next on first `ended` event |
| User manually scrolls during autoscroll | Destination item's autoscroll timer restarts from zero |
| Mute on, video becomes visible | Video plays silently |
| Mute off, video becomes visible | Video plays with audio; previous visible video (if any) is paused + muted |
| Video fully out of view | Paused and muted |
| Network error on item | Item is removed from DOM; cache queue continues with the next |
| End of feed (all pages exhausted) | Counter shows final total; no more fetches |
| User toggles autoscroll | All visible videos' `loop` attribute is updated |

## Risks and mitigations

- **iOS Safari sparse `progress` events.** Mitigated by a 100ms `setInterval` fallback that recomputes the buffer-threshold condition until the video starts. The interval is cleared on `playing`.
- **Worker not preempted.** An in-flight download may take 5–10s on a slow connection; during that time the queue head is "wrong" relative to the user's position. Acceptable per the strict "one at a time" requirement; flagged as the trade-off of strictness.
- **DOM growth.** Items are never removed (per spec). For long sessions the DOM grows. Out of scope for this rewrite; future work may add a sliding window.
- **GIF duration fallback.** If `getGifDuration` cannot parse the GIF (e.g., a malformed header), the autoscroll timer falls back to `IMAGE_AUTOSCROLL_DELAY_S`. Worst case: the GIF autoscrolls a bit too early.
- **First-render flash of unstyled content.** The first paint shows N empty placeholder slots with spinners. The dark theme is applied via `data-theme="dark"` on `<html>` in the initial HTML to avoid a flash.
- **CSS scroll-snap and reduced-motion.** `prefers-reduced-motion` should disable smooth scrolling on snap-to-next. Implemented via a `@media (prefers-reduced-motion: reduce)` rule that overrides `scroll-behavior` to `auto` on `#feed`.

## Out of scope

- Backend, auth, DB, OPML sync, scheduler, media proxy.
- Theme toggle, slideshow, show-seen, FAB.
- JS unit tests.
- DOM eviction / virtual scrolling.
- Reduced-motion / accessibility audit beyond the prefers-reduced-motion media query noted above.
