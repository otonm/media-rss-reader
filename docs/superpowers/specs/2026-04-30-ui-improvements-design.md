# UI Improvements — Design Spec

> Brainstormed and approved 2026-04-30.

## Context

The media RSS reader has a working scroll+slideshow UI but several UX pain points:
- On desktop the FAB (floating action button) is redundant next to the keyboard-shortcut text bar
- Mouse wheel scrolling intercepts native scroll, making it feel unnatural
- Auto-scroll jumps discretely item-to-item instead of drifting continuously
- Videos never autoplay; GIFs have no pause behavior

This spec covers four improvements using vanilla JS — no new runtime dependencies.

---

## 1. Desktop Controls Bar

- `#controls` bar (bottom-left) replaces text shortcuts with icon buttons identical to the FAB buttons (⟳ ⊞ 🔇 🌙).
- FAB hidden on desktop (≥1024px) via media query. FAB stays on mobile.
- Each button gets a pure-CSS tooltip (`:hover` + `::after`) showing `"Label [key]"` above the icon.
- Active state uses the same `.active` class as FAB buttons.
- `updateFab()` renamed to `updateControls()`, syncs both button sets.

## 2. Mouse Wheel — Native Browser Scroll

- Remove `wheel` event listener (currently calls `advance(±1)`).
- `#scroll-view` already has `overflow-y: auto` — browser handles wheel natively.
- Add `viewObserver` (IntersectionObserver, threshold 0.5) watching `.media-item` elements to update `currentIndex` as items scroll into view.
- j/k keys unchanged: `scrollIntoView({behavior:'smooth', block:'center'})`.

## 3. Auto-scroll — Continuous RAF Drift

- Replace `setTimeout` loop with `requestAnimationFrame` loop calling `scrollView.scrollBy(0, AUTO_SCROLL_SPEED)` (~1.5 px/frame).
- One `mediaObserver` (IntersectionObserver, threshold 0.85) watches `video` and `img[data-type="gif"]` elements.
- When a video enters view during auto-scroll: pause RAF, play video, on `ended` → `advance(1)`, resume RAF.
- When a GIF enters view during auto-scroll: pause RAF, parse GIF duration, after timeout → `advance(1)`, resume RAF.
- GIF duration parsed from binary (Graphic Control Extension blocks, 0x21 0xF9 0x04 signature), cached on `item.gifDuration`. URL validated to `/api/media/proxy?` prefix. Duration clamped 50ms–60,000ms; fallback to `IMAGE_DELAY_MS`.
- `ended` listener on video guarded: `if (!autoScroll) advance(1)` to prevent double-advance.

## 4. Video Autoplay

- `el.autoplay = true` set at creation (muted by default, browsers allow muted autoplay).
- `mediaObserver` also handles non-auto-scroll mode: `video.play()` on enter, `video.pause()` on leave.
- Prevents multiple off-screen videos playing simultaneously.

## Files

| File | Changes |
|------|---------|
| `src/static/index.html` | Icon buttons in `#controls`; update FAB onclick to `updateControls()` |
| `src/static/style.css` | Icon row styles, tooltips, hide FAB on desktop |
| `src/static/app.js` | All logic changes |
