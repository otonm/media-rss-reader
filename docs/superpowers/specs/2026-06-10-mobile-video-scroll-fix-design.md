# Mobile Video Scroll Fix

**Date:** 2026-06-10
**Status:** Implemented
**Scope:** Frontend bug fix + UX improvement
**Files:** `src/static/app.js`, `src/static/style.css`

## Problem
On mobile, scrolling on a video causes the page to jump forward by an indefinite number of items, and scrolling back is erratic. The root causes are: (1) a `touchend` handler in scroll mode that calls `advance()` and fights native scroll; (2) all intersecting videos play simultaneously, causing resource contention and a feedback loop with `el.play()`; (3) `el.controls = true` on hover causes layout shift; (4) the loading spinner is invisible because the wrap has 0 height until media loads.

## Goals
- Native scroll is the only scroll interaction in scroll mode
- Only the video at `currentIndex` plays at any time
- No layout shift from native video controls
- Scroll-to-item advances are immediate, not smooth-interrupted
- Loading state is always visible (placeholder slot)
- iOS does not fullscreen-hijack videos

## Non-Goals
- Backend / API / DB changes
- Test framework changes (no JS test framework in project)
- Slideshow mode refactor
- New key bindings or controls

## Design

### Change 1 — Drop swipe-to-advance in scroll mode
**File:** `src/static/app.js`
**Where:** `touchend` handler

In scroll mode, the handler does nothing. Native scroll handles all vertical motion. Swipe-to-advance is preserved for slideshow mode only.

```js
document.addEventListener("touchend", e => {
  if (!slideshowMode) return;  // native scroll handles motion in scroll mode
  const dx = e.changedTouches[0].clientX - _tx;
  const dy = e.changedTouches[0].clientY - _ty;
  if (Math.abs(dx) < SWIPE_MIN && Math.abs(dy) < SWIPE_MIN) return;
  const forward = Math.abs(dy) >= Math.abs(dx) ? dy < 0 : dx < 0;
  advance(forward ? 1 : -1);
}, { passive: true });
```

### Change 2 — Enforce single active video
**File:** `src/static/app.js`
**Where:** `mediaObserver` callback

Before calling `el.play()`, check that the video's index matches `currentIndex`. For all other intersecting videos, pause and skip the play path. The `updateActiveAudio` mute logic stays.

```js
if (isVideo) {
  videoRatios.set(el, ratio);
  updateActiveAudio();
}

// Start playing only the current item's video, once 50% visible
if (!el.dataset.playing && ratio >= 0.5) {
  if (isVideo) {
    const wrap = el.closest(".media-item");
    const idx = wrap ? items.findIndex(i => i.id === wrap.dataset.id) : -1;
    if (idx !== currentIndex) return;  // wait for this video to become current
    el.dataset.playing = "1";
    el.play().catch(() => {});
  } else if (isGif) {
    el.dataset.playing = "1";
  }
}
```

When `currentIndex` changes, the previous video goes out of view (paused) and the new one becomes intersecting — its next observer tick at `ratio >= 0.5` will satisfy the `currentIndex` check and call `play()`.

### Change 3 — Remove hover-controls handlers
**File:** `src/static/app.js`
**Where:** video element creation

Delete the `mouseenter` / `mouseleave` listeners that toggle `el.controls`. The initial `el.controls = false` stays. No native controls are ever shown.

### Change 4 — Immediate scroll on advance
**File:** `src/static/app.js`
**Where:** `scrollToIndex`

```js
function scrollToIndex(idx) {
  const els = document.querySelectorAll(".media-item");
  if (els[idx]) els[idx].scrollIntoView({ block: "center" });
}
```

Remove `behavior: "smooth"`. Smooth scroll was being interrupted by subsequent intersection events, leaving the page in inconsistent states during the animation.

### Change 5 — iOS inline playback
**File:** `src/static/app.js`
**Where:** video element creation

Add `playsinline` attributes so iOS Safari doesn't fullscreen-hijack videos on tap.

```js
el = document.createElement("video");
el.src = `/api/media/proxy?url=${encodeURIComponent(item.media_url)}`;
el.setAttribute("playsinline", "");
el.setAttribute("webkit-playsinline", "");
el.controls = false;
el.muted = muted;
el.loop = false;
el.autoplay = true;
```

### Change 6 — Placeholder slot
**File:** `src/static/style.css`
**Where:** `.media-item` and `.media-item .spinner` rules

Add `min-height: 90vh` to `.media-item` so the slot reserves a viewport's worth of space before the media loads. Adjust the spinner to be centered within the now-reliable-height slot.

```css
.media-item {
  max-width: 100%;
  min-height: 90vh;        /* reserves space so spinner is visible */
  display: block;
  margin: 0 auto 2rem;
  position: relative;
}

.media-item .spinner {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 1;
}
```

## Behavior Matrix (post-fix)

| Action | Result |
|---|---|
| Page load | Each item appears as a 90vh slot with a centered spinner; media fills the slot as it loads |
| Scroll on a video (mobile) | Native scroll; video pauses as it leaves the viewport per the existing observer |
| `j` / `↓` | `advance(1)` → immediate scroll to center the next item; if next is a video, it becomes the current and starts playing |
| `k` / `↑` | Same as `j`, in reverse |
| Video ends, autoScroll on | Auto-scroll resumes (existing logic) |
| Video ends, autoScroll off | `advance(1)` centers the next item; its slot is already in the DOM with the spinner (or the loaded media) |
| Swipe in scroll mode | Native scroll only (was: jump to next item) |
| Swipe in slideshow mode | Advance by one item (unchanged) |
| Multiple videos in viewport | Only the `currentIndex` video plays; others are paused |
| iOS tap on video | No fullscreen hijack; video plays inline |

## Risks & Mitigations
- **Video doesn't resume on scroll-back**: handled by the existing observer re-entering with `ratio >= 0.5` and triggering `play()`.
- **`currentIndex` lag**: a one-tick delay between "becomes current" and "video plays" is acceptable and matches existing behavior in slideshow mode.
- **Spinners on already-loaded items below the fold**: unchanged from current behavior — spinners are hidden by `.loaded` class once media loads.

## Out of Scope
- Backend, API, DB
- Test additions (no JS test surface in this project)
- Slideshow mode refactor
- New key bindings / controls
- Custom video controls UI
