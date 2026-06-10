# Mobile Video Scroll Fix — Round 2

**Date:** 2026-06-10
**Status:** Implemented
**Builds on:** `docs/superpowers/specs/2026-06-10-mobile-video-scroll-fix-design.md` (round 1)
**Scope:** Frontend bug fix + UX improvement
**Files:** `src/static/app.js`, `src/static/style.css`

## Problem
After round 1, two issues remain:

1. **Video still jumps to next item on `ended`, even when autoScroll is OFF.** The user wants the video to loop in manual mode and only advance when autoScroll is on.

2. **Placeholder/media separation is not strict enough.** The current `createMediaEl` returns a single wrap that contains BOTH a spinner AND the media element. The spinner is shown/hidden via a `.loaded` class. The user wants true skeleton-screen separation: a placeholder element (spinner only) that is replaced by a media item (no spinner) when the media loads.

## Goals
- Video loops in manual mode; only advances to next when autoScroll is on
- `el.loop` is kept in sync with the `autoScroll` flag (initial value + on toggle)
- Placeholder is a separate DOM element from the media item
- Placeholder is removed from the DOM on upgrade (not re-classed)
- Media preloads off-DOM via `loadeddata` (first frame, not full download — same as current behavior)
- Existing tests still pass; no backend/API changes

## Non-Goals
- Backend / API / DB
- Slideshow mode refactor
- New key bindings or controls
- Off-DOM fallback for browsers that don't fire media events on detached elements

## Design

### Change A — Issue 1: video loop/advance logic

**File:** `src/static/app.js`

A1. In `createMediaEl` (line 76): `el.loop = false` → `el.loop = !autoScroll`. The video natively loops when autoScroll is off; with `loop=true` the `ended` event never fires, so the wrong branch becomes unreachable.

A2. In the `ended` listener (line 79): `if (!autoScroll)` → `if (autoScroll)`. The handler now only advances when autoScroll is on.

A3. In `toggleAutoScroll` (line 386-395): after the existing body, walk all `.media-item video` elements in the DOM and set `v.loop = !autoScroll`. Keeps existing videos in sync with the new state.

### Change B — Issue 2: true placeholder pattern

**File:** `src/static/app.js`

B1. **Add `createPlaceholder(item)`** — returns `<div class="placeholder" data-id="..."><div class="spinner"></div></div>`. Spinner only; no media element.

B2. **Add `createMediaItem(item)`** — returns `<div class="media-item" data-id="...">` with the real `<img>`/`<video>` and all the listeners + observers (`viewObserver`, `seenObserver`, `mediaObserver`). No spinner inside.

B3. **Rewrite `createMediaEl(item)`** — returns the placeholder; creates an off-DOM preloader (`<img>` via `new Image()` or `<video>`); on `load`/`loadeddata` calls `viewObserver.unobserve(placeholder)`, builds a media item, and `placeholder.replaceWith(mediaItem)`; on `error` calls a new `_removeItem(placeholder)`.

The preloader uses `loadeddata` (first frame) for videos, not `canplaythrough` — same UX as the current code, no regression. Listeners are attached **before** `src` is set to avoid a race where a cached media loads synchronously and the `load` event fires before the listener exists. For the video preloader, `playsinline`/`webkit-playsinline`, `muted=true`, and `preload="auto"` are set so off-DOM preload actually starts and iOS doesn't fullscreen-hijack the preload.

B4. **Split `_discardFailedItem` into two helpers** — `_discardFailedItem(wrap, el)` (unobserves mediaObserver, viewObserver, clears videoRatios, calls `_removeItem`) for media-item errors; `_removeItem(wrap)` (just removes the wrap and splices from `items[]`) for placeholder errors. `viewObserver.unobserve` is a no-op on unobserved elements per the IntersectionObserver spec, so it's safe to call on both kinds of wrap.

B5. **`scrollToIndex` (line 360-361)** — query both `.placeholder` and `.media-item` so `j`/`k` keys can target items that are still loading.

B6. **`showSlide` (line 401-421)** — slideshow needs the media element immediately, so call `createMediaItem(item)` directly. `createMediaEl` would return a placeholder (no media), which breaks the slideshow layer.

**File:** `src/static/style.css`

B7. **Share the base rule** for `.placeholder` and `.media-item` (same height, width, margin, position). Currently only `.media-item` has it; add `.placeholder` to the selector.

B8. **Move the spinner rule** from `.media-item .spinner` to `.placeholder .spinner`. The spinner only lives in placeholders now.

B9. **Delete** `.media-item.loaded .spinner { display: none; }` — the spinner is only in placeholders, and placeholders are removed from the DOM on upgrade, so the rule is dead.

B10. Keep `.media-item .spinner::after`, `@keyframes spin`, `.media-item img/video`, and `.media-item.seen::after` unchanged.

## Behavior Matrix (post-fix)

| Scenario | Before | After |
|---|---|---|
| Manual mode, video ends | advance(1) — jumps to next | video loops (el.loop = !autoScroll) |
| AutoScroll on, video ends | video stops, autoScroll resumes | same (unchanged) |
| Toggle autoScroll while video plays | video's loop state stays the same | video's loop is updated to match new state |
| Page load: items fetched | each becomes `.media-item` with hidden spinner | each becomes a `.placeholder` (spinner only) |
| Media loads | `.loaded` class hides spinner | placeholder is replaced by `.media-item` (no spinner) |
| Media errors | media item removed | placeholder removed (same) |
| `j` / `↓` on a still-loading item | no-op (item not in `.media-item` query) | scrolls to placeholder (now queryable) |
| Slideshow mode | uses `createMediaEl`, extracts media element | uses `createMediaItem` directly (immediate media) |

## Risks & Mitigations
- **Off-DOM video preload may not fire `loadeddata` on some browsers**: modern browsers handle this correctly. If reported, add a timeout fallback that upgrades the placeholder anyway.
- **Double media load for cold-cache videos**: the preloader triggers one load, the new media element inside `createMediaItem` triggers a second; the browser cache serves the second from the first. No re-download.
- **Race in `createMediaEl`**: the listeners are attached to the preloader BEFORE `src` is set, so a synchronously-loading cached media won't fire the event before the listener is attached.
- **Placeholders are observed by `viewObserver`**: required so `currentIndex` tracks correctly while items are loading. `_discardFailedItem` and `_removeItem` handle both placeholder and media-item cases; `viewObserver.unobserve` is a no-op on unobserved elements.

## Out of Scope
- Backend, API, DB
- Test additions (no JS test framework in this project)
- Slideshow mode refactor beyond the one-line change in `showSlide`
- New key bindings / controls
- Custom video controls UI
