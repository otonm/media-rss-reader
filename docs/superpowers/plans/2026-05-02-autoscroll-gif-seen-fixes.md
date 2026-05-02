# Autoscroll / GIF / Seen-Tracking Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three frontend bugs: video autoscroll pauses at the wrong point, GIF autoscroll loops infinitely, and the "read" checkmark is invisible in normal scroll mode.

**Architecture:** All changes are in two static files — `app.js` (mediaObserver logic) and `style.css` (seen badge selector). No backend changes required. No new files needed.

**Tech Stack:** Vanilla JS (IntersectionObserver, requestAnimationFrame), CSS custom properties, FastAPI static file serving.

---

### Task 1: Fix seen-badge CSS — show checkmark in all modes

**Files:**
- Modify: `src/static/style.css` (lines 259–274)

**Background:** The ✓ badge is currently inside `#feed-list.feed-list--show-all .media-item.seen::after`, so it only renders when the "show all" toggle is active. Removing the prefix makes it visible whenever an item has the `seen` class, regardless of mode.

- [ ] **Step 1: Edit style.css**

  Replace the existing seen badge rule (around line 259):

  ```css
  /* BEFORE */
  #feed-list.feed-list--show-all .media-item.seen::after {
  ```

  With:

  ```css
  /* AFTER */
  .media-item.seen::after {
  ```

  The rest of the rule stays unchanged:

  ```css
  .media-item.seen::after {
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

- [ ] **Step 2: Manual verify**

  Start the app: `uv run uvicorn src.main:app --reload --port 8080`

  Open the browser. Scroll past an item. Confirm a ✓ badge appears in its top-right corner. Confirm it stays visible (item not hidden). Reload the page — the seen item should not reappear (filtered by `unseen=true`).

- [ ] **Step 3: Commit**

  ```bash
  git add src/static/style.css
  git commit -m "fix: show seen checkmark in all modes, not just show-all"
  ```

---

### Task 2: Rewrite mediaObserver — top-edge pause + scrollWaited guard

**Files:**
- Modify: `src/static/app.js` (lines 177–250 — the `mediaObserver` const)

**Background:**

Two bugs live in the same observer block:

1. **Wrong pause point** — `ratio >= 1.0` fires when the element is fully visible (never fires for tall elements). Replace with `rect.top <= rootT` so autoscroll pauses when the element's top edge reaches the viewport top.
2. **GIF infinite loop** — after the per-cycle timeout fires, `scrollPausedHere` is deleted and autoscroll resumes, but the element's top is still at the viewport top. The observer fires again immediately and re-pauses. Fix: introduce `scrollWaited` flag — set when the pause has been served, cleared on exit. Pause condition requires `!scrollWaited`.

The same `scrollWaited` guard also protects videos (prevents adding a second `ended` listener on re-fire).

- [ ] **Step 1: Replace the entire mediaObserver block**

  Find and replace the block from `const mediaObserver = new IntersectionObserver(` through the closing `}, { threshold: [0.5, 1.0] });` (roughly lines 177–250) with:

  ```js
  const mediaObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      const el = entry.target;
      const isVideo = el.tagName === "VIDEO";
      const isGif = el.tagName === "IMG" && el.dataset.type === "gif";
      const ratio = entry.intersectionRatio;

      if (!entry.isIntersecting) {
        if (isVideo) {
          el.pause();
          delete el.dataset.playing;
        }
        if (isVideo || isGif) {
          delete el.dataset.scrollPausedHere;
          delete el.dataset.scrollWaited;
          if (autoScrollPaused) {
            autoScrollPaused = false;
            if (autoScroll) startAutoScroll();
          }
          if (isGif) delete el.dataset.playing;
        }
        return;
      }

      if (isVideo || isGif) {
        // Start playing once element is 50 % visible
        if (!el.dataset.playing && ratio >= 0.5) {
          el.dataset.playing = "1";
          if (isVideo) el.play().catch(() => {});
        }

        // Pause autoscroll when element's top edge reaches viewport top
        const rect  = entry.boundingClientRect;
        const rootT = entry.rootBounds?.top ?? 0;
        const topReached = rect.top <= rootT;

        if (topReached && autoScroll && !autoScrollPaused
            && !el.dataset.scrollPausedHere && !el.dataset.scrollWaited) {
          autoScrollPaused = true;
          el.dataset.scrollPausedHere = "1";
          stopAutoScroll();

          if (isVideo) {
            el.addEventListener("ended", () => {
              if (el.dataset.scrollPausedHere) {
                el.dataset.scrollWaited = "1";
                delete el.dataset.scrollPausedHere;
                autoScrollPaused = false;
                if (autoScroll) startAutoScroll();
              }
            }, { once: true });
          } else {
            // GIF: resume after one cycle duration
            const item = items.find(
              i => el.getAttribute("src") === `/api/media/proxy?url=${encodeURIComponent(i.media_url)}`
            );
            const resume = (duration) => {
              setTimeout(() => {
                if (el.dataset.scrollPausedHere) {
                  el.dataset.scrollWaited = "1";
                  delete el.dataset.scrollPausedHere;
                  autoScrollPaused = false;
                  if (autoScroll) startAutoScroll();
                }
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
              }).catch(() => resume(IMAGE_DELAY_MS));
            }
          }
        }
      }
    });
  }, { threshold: Array.from({ length: 21 }, (_, i) => i / 20) });
  ```

- [ ] **Step 2: Manual verify — video top-edge pause**

  Enable autoscroll (`a`). Scroll to a video. Confirm autoscroll stops when the video's top edge aligns with the top of the screen (not when the video is centred). Confirm this works for both short videos (shorter than viewport) and tall/full-screen videos.

- [ ] **Step 3: Manual verify — GIF does not loop**

  Enable autoscroll. Scroll to a GIF. Confirm autoscroll pauses for roughly one GIF cycle, then resumes and scrolls past. Confirm autoscroll does NOT pause again for the same GIF as it scrolls off the top.

- [ ] **Step 4: Run tests**

  ```bash
  uv run pytest
  ```

  Expected: all tests pass (no backend logic was changed).

- [ ] **Step 5: Commit**

  ```bash
  git add src/static/app.js
  git commit -m "fix: pause autoscroll at video/gif top edge; prevent GIF infinite re-pause"
  ```
