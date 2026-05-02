# Design: Autoscroll video/GIF fixes + seen-tracking visual feedback

**Date**: 2026-05-02

## Context

Three bugs in the autoscroll and seen-tracking behaviour of the media RSS reader frontend:

1. Autoscroll pauses when a video is *fully visible* (ratio = 1.0) rather than when its top edge reaches the viewport top. For videos taller than the viewport the threshold is never reached at all, so autoscroll skips them entirely.
2. GIFs loop indefinitely because after the per-cycle timeout fires and autoscroll resumes, the GIF's top is still at the viewport top, the observer fires again, and a new pause+timeout is scheduled — an infinite loop.
3. The "read" checkmark (✓) only appears in "show-all" mode (`feed-list--show-all` CSS class present). In normal scroll mode items are marked as seen in the DB but no visual indicator appears.

---

## Fix 1 — Video autoscroll: pause at upper edge, not full-visibility

**File**: `src/static/app.js`

**Change the `mediaObserver` thresholds** from `[0.5, 1.0]` to fine-grained 5 % steps:

```js
Array.from({ length: 21 }, (_, i) => i / 20)
// → [0, 0.05, 0.10, ..., 1.0]
```

This makes the callback fire frequently as the element moves through the viewport.

**Change the "start playing" guard** to be explicit about the 50 % threshold:

```js
if (!el.dataset.playing && ratio >= 0.5) { … }
```

**Change the pause condition** from `ratio >= 1.0` to:

```js
const rect  = entry.boundingClientRect;
const rootT = entry.rootBounds?.top ?? 0;
const topReached = rect.top <= rootT;

if (topReached && autoScroll && !autoScrollPaused && !el.dataset.scrollPausedHere && !el.dataset.scrollWaited)
```

`rect.top <= rootT` is true the moment the element's top edge crosses the viewport's top edge — the natural "now pinned to the top of the screen" point.

---

## Fix 2 — GIF (and video) autoscroll: prevent infinite re-pause

**File**: `src/static/app.js`

Introduce a second per-element flag `scrollWaited` that means "we have already waited for this element in the current viewport session; don't pause again".

**Set it** in both resume paths:

- Video `ended` handler: set `el.dataset.scrollWaited = "1"`, delete `scrollPausedHere`, resume autoscroll.
- GIF `resume()` timeout body: set `el.dataset.scrollWaited = "1"`, delete `scrollPausedHere`, resume autoscroll.

**Clear it** (along with `scrollPausedHere`) in the `!entry.isIntersecting` cleanup block, so the element can trigger a pause again the next time it enters the viewport.

Pause condition (from Fix 1) already includes `!el.dataset.scrollWaited`, completing the guard.

---

## Fix 3 — Seen checkmark: visible in all modes

**File**: `src/static/style.css`

Current selector (checkmark only visible in show-all mode):

```css
#feed-list.feed-list--show-all .media-item.seen::after { content: "✓"; … }
```

Change to (always visible when item has `seen` class):

```css
.media-item.seen::after { content: "✓"; … }
```

Items remain in the DOM after being marked seen (nothing hides them in normal mode). They simply won't be re-fetched on the next page load when `unseen=true` is the default filter.

---

## Verification

1. Enable autoscroll (`a`). Scroll to a video — autoscroll should stop exactly when the video's top edge aligns with the viewport top (not when it's centred or fully visible).
2. For a video taller than the viewport, autoscroll should still stop (this was previously broken).
3. Enable autoscroll, scroll to a GIF — autoscroll pauses for one cycle duration, then resumes and scrolls past. It must NOT pause again for the same GIF without leaving and re-entering the viewport.
4. In normal (unseen-only) mode: scroll past an item; a ✓ badge should appear in the top-right corner of the item immediately.
5. In show-all mode: previously-read items should also show ✓ badges.
6. Reload the page — items that received a ✓ badge in step 4 should no longer appear (filtered by `unseen=true`).
7. Run `uv run pytest` — all tests must pass.
