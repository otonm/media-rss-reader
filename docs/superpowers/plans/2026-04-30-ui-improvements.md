# UI Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the desktop text controls bar with icon buttons + tooltips, enable native wheel scroll, replace discrete auto-scroll with continuous RAF drift, and fix video autoplay.

**Architecture:** All changes confined to three frontend static files. A `viewObserver` tracks `currentIndex` as items scroll naturally. A `mediaObserver` handles video/GIF play/pause for both autoplay and auto-scroll pausing. Auto-scroll switches from `setTimeout` to `requestAnimationFrame`.

**Tech Stack:** Vanilla JS (ES2020), CSS custom properties, IntersectionObserver API, requestAnimationFrame API

---

## File Map

| File | What changes |
|------|-------------|
| `src/static/index.html` | Icon buttons replace text in `#controls`; FAB `onclick` updated to `updateControls()` |
| `src/static/style.css` | `#controls` restyled as glass icon row; `.ctrl-btn` + tooltip styles; FAB hidden on desktop |
| `src/static/app.js` | Remove wheel listener; add `viewObserver` + `mediaObserver`; RAF auto-scroll; GIF binary parser; rename `updateFab` → `updateControls`; video autoplay |

---

### Task 1: HTML — Replace text controls with icon buttons

**Files:**
- Modify: `src/static/index.html`

- [ ] **Step 1: Replace `#controls` content**

Open `src/static/index.html`. The current `#controls` block is:
```html
<div id="controls">
  <span>j/k navigate</span>
  <span>a auto-scroll</span>
  <span>s slideshow</span>
  <span>m mute</span>
  <span>d theme</span>
</div>
```

Replace it with:
```html
<div id="controls">
  <button class="ctrl-btn" id="ctrl-autoscroll" title="Auto-scroll [a]" onclick="toggleAutoScroll()">⟳</button>
  <button class="ctrl-btn" id="ctrl-slideshow"  title="Slideshow [s]"   onclick="toggleSlideshow()">⊞</button>
  <button class="ctrl-btn" id="ctrl-mute"       title="Mute [m]"        onclick="toggleMute()">🔇</button>
  <button class="ctrl-btn" id="ctrl-theme"      title="Theme [d]"       onclick="toggleTheme()">🌙</button>
</div>
```

- [ ] **Step 2: Update FAB button `onclick` attributes to call `updateControls()` instead of `updateFab()`**

The FAB buttons currently call `updateFab()` inline. Update all four:
```html
<button class="fab-btn" id="fab-theme"      title="Theme"       onclick="toggleTheme();updateControls()">🌙</button>
<button class="fab-btn" id="fab-mute"       title="Mute"        onclick="toggleMute();updateControls()">🔇</button>
<button class="fab-btn" id="fab-slideshow"  title="Slideshow"   onclick="toggleSlideshow();updateControls()">⊞</button>
<button class="fab-btn" id="fab-autoscroll" title="Auto-scroll" onclick="toggleAutoScroll();updateControls()">⟳</button>
```

- [ ] **Step 3: Commit**
```bash
git add src/static/index.html
git commit -m "feat: replace controls text bar with icon buttons"
```

---

### Task 2: CSS — Style icon bar, tooltips, hide FAB on desktop

**Files:**
- Modify: `src/static/style.css`

- [ ] **Step 1: Replace the `#controls` CSS block**

The current `#controls` block (the one with `position: fixed; bottom: 0; left: 0; right: 0; ...`) reads:
```css
#controls {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  padding: 0.5rem 1rem;
  background: rgba(0, 0, 0, 0.5);
  color: #ccc;
  font-size: 0.75rem;
  display: flex;
  gap: 1.5rem;
}
```

Replace it with:
```css
#controls {
  position: fixed;
  bottom: 1.25rem;
  left: 1rem;
  z-index: 100;
  display: flex;
  gap: 0.5rem;
  align-items: center;
  background: rgba(20, 20, 30, 0.82);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 24px;
  padding: 0.5rem 0.6rem;
}
```

- [ ] **Step 2: Add `.ctrl-btn` styles after the `#controls` block**

```css
.ctrl-btn {
  position: relative;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  border: none;
  background: rgba(255, 255, 255, 0.08);
  color: #ddd;
  font-size: 1rem;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s;
}

.ctrl-btn.active {
  background: rgba(108, 142, 191, 0.5);
  color: #fff;
}

.ctrl-btn::after {
  content: attr(title);
  position: absolute;
  bottom: calc(100% + 8px);
  left: 50%;
  transform: translateX(-50%);
  background: rgba(20, 20, 30, 0.95);
  color: #eee;
  font-size: 0.7rem;
  padding: 4px 8px;
  border-radius: 6px;
  border: 1px solid rgba(255, 255, 255, 0.15);
  white-space: nowrap;
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.15s;
}

.ctrl-btn:hover::after {
  opacity: 1;
}
```

- [ ] **Step 3: Update the responsive section at the bottom of `style.css`**

The current responsive block is:
```css
@media (min-width: 1024px) {
  .media-item { max-width: 900px; }
}

@media (max-width: 767px) {
  #controls { display: none; }
}
```

Replace with:
```css
@media (min-width: 1024px) {
  .media-item { max-width: 900px; }
  #fab-container { display: none; }
}

@media (max-width: 767px) {
  #controls { display: none; }
}
```

- [ ] **Step 4: Start dev server and verify CSS visually**

```bash
uv run uvicorn src.main:app --reload --port 8080
```

Open http://localhost:8080 at ≥1024px width:
- Bottom-left: glass pill with 4 icon buttons visible
- Hover each icon: tooltip appears above (e.g. "Auto-scroll [a]")
- No FAB visible in bottom-right

Resize to <768px:
- Icon bar hidden, FAB visible bottom-right

- [ ] **Step 5: Commit**
```bash
git add src/static/style.css
git commit -m "feat: style desktop controls icon bar with tooltips, hide FAB on desktop"
```

---

### Task 3: JS — `updateControls()` replaces `updateFab()`

**Files:**
- Modify: `src/static/app.js`

- [ ] **Step 1: Replace the `updateFab()` function**

Find and replace the entire `updateFab()` function (currently in section 13):
```js
function updateFab() {
  const theme = document.documentElement.getAttribute("data-theme") || "dark";
  document.getElementById("fab-autoscroll").classList.toggle("active", autoScroll);
  document.getElementById("fab-slideshow").classList.toggle("active", slideshowMode);
  document.getElementById("fab-mute").classList.toggle("active", muted);
  document.getElementById("fab-theme").textContent = theme === "dark" ? "🌙" : "☀";
}
```

Replace with:
```js
function updateControls() {
  const theme = document.documentElement.getAttribute("data-theme") || "dark";
  const icon = theme === "dark" ? "🌙" : "☀";

  // Mobile FAB
  document.getElementById("fab-autoscroll").classList.toggle("active", autoScroll);
  document.getElementById("fab-slideshow").classList.toggle("active", slideshowMode);
  document.getElementById("fab-mute").classList.toggle("active", muted);
  document.getElementById("fab-theme").textContent = icon;

  // Desktop ctrl bar
  document.getElementById("ctrl-autoscroll").classList.toggle("active", autoScroll);
  document.getElementById("ctrl-slideshow").classList.toggle("active", slideshowMode);
  document.getElementById("ctrl-mute").classList.toggle("active", muted);
  document.getElementById("ctrl-theme").textContent = icon;
}
```

- [ ] **Step 2: Replace all remaining `updateFab()` calls in `app.js` with `updateControls()`**

There are calls inside `toggleAutoScroll()`, `toggleSlideshow()`, `toggleMute()`, `toggleTheme()`, `toggleFab()`, and the startup sequence. Do a global replace:
```bash
sed -i 's/updateFab()/updateControls()/g' src/static/app.js
```

Verify with:
```bash
grep -n "updateFab" src/static/app.js
```
Expected: no matches.

- [ ] **Step 3: Verify active states in browser**

Open http://localhost:8080 at desktop width:
- Press `a` — `#ctrl-autoscroll` gets `.active` blue highlight; press again — removed
- Press `s` — `#ctrl-slideshow` highlights; press again — removed
- Press `m` — `#ctrl-mute` highlights; press again — removed
- Press `d` — both `#ctrl-theme` and `#fab-theme` icon updates to ☀/🌙

- [ ] **Step 4: Commit**
```bash
git add src/static/app.js
git commit -m "feat: rename updateFab to updateControls, sync desktop ctrl buttons"
```

---

### Task 4: JS — Native wheel scroll + `viewObserver`

**Files:**
- Modify: `src/static/app.js`

- [ ] **Step 1: Remove the wheel event listener**

Find and delete this entire block from section 12:
```js
// ---------------------------------------------------------------------------
// 12. Mouse wheel
// ---------------------------------------------------------------------------

document.addEventListener("wheel", e => {
  if (e.deltaY > 0) advance(1);
  else if (e.deltaY < 0) advance(-1);
}, { passive: true });
```

- [ ] **Step 2: Add `viewObserver` after the `seenObserver` block**

After the `seenObserver` closing `}, { threshold: 0.8 });` line, add:

```js
// ---------------------------------------------------------------------------
// 5b. Viewport observer — keeps currentIndex in sync during native scroll
// ---------------------------------------------------------------------------
const viewObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (!entry.isIntersecting) return;
    const idx = items.findIndex(i => i.id === entry.target.dataset.id);
    if (idx !== -1) currentIndex = idx;
  });
}, { threshold: 0.5 });
```

- [ ] **Step 3: Register items with `viewObserver` in `createMediaEl()`**

In `createMediaEl()`, find the line `seenObserver.observe(wrap);` and add directly after it:
```js
viewObserver.observe(wrap);
```

- [ ] **Step 4: Verify in browser**

Open http://localhost:8080. Mouse wheel scrolls naturally (speed/momentum from OS). `j`/`k` still jump item-to-item with smooth animation. Open DevTools console, scroll a few items, type `currentIndex` — should reflect correct position.

- [ ] **Step 5: Commit**
```bash
git add src/static/app.js
git commit -m "feat: remove wheel interceptor, add viewObserver for currentIndex tracking"
```

---

### Task 5: JS — Video autoplay + `mediaObserver`

**Files:**
- Modify: `src/static/app.js`

- [ ] **Step 1: Mark GIF elements and enable video autoplay in `createMediaEl()`**

In the `if (item.media_type === "video")` branch:
1. After `el.loop = false;`, add `el.autoplay = true;`
2. Change the `ended` listener to guard against double-advance in auto-scroll mode:
   ```js
   el.addEventListener("ended", () => { if (!autoScroll) advance(1); });
   ```
   (Replaces the existing `el.addEventListener("ended", () => advance(1));`)

In the `else` (img) branch, after `el.loading = "lazy";`, add:
```js
if (item.media_type === "gif") el.dataset.type = "gif";
```

- [ ] **Step 2: Add `mediaObserver` after the `viewObserver` block**

```js
// ---------------------------------------------------------------------------
// 5c. Media observer — autoplay videos; pause/resume auto-scroll for media
// ---------------------------------------------------------------------------
const mediaObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    const el = entry.target;
    const isVideo = el.tagName === "VIDEO";
    const isGif = el.tagName === "IMG" && el.dataset.type === "gif";

    if (entry.isIntersecting) {
      if (isVideo) el.play().catch(() => {});
    } else {
      if (isVideo) el.pause();
    }
  });
}, { threshold: 0.5 });
```

(The auto-scroll pause logic will be added in Task 7; this observer is intentionally minimal here.)

- [ ] **Step 3: Register video elements with `mediaObserver` in `createMediaEl()`**

In the video branch of `createMediaEl()`, after all `el.addEventListener(...)` calls, add:
```js
mediaObserver.observe(el);
```

- [ ] **Step 4: Verify video autoplay in browser**

Scroll a video into view — it should autoplay (muted). Scroll it out — pauses. Press `m` — unmutes all videos. Video `ended` still calls `advance(1)` when auto-scroll is off.

- [ ] **Step 5: Commit**
```bash
git add src/static/app.js
git commit -m "feat: video autoplay via mediaObserver, mark gif elements with data-type"
```

---

### Task 6: JS — RAF auto-scroll replaces setTimeout loop

**Files:**
- Modify: `src/static/app.js`

- [ ] **Step 1: Update state variables (section 2)**

Replace:
```js
let autoScrollTimer = null;  // timeout handle
```
With:
```js
let autoScrollRafId = null;   // requestAnimationFrame handle
let autoScrollPaused = false; // paused waiting for video/gif
```

- [ ] **Step 2: Add `AUTO_SCROLL_SPEED` constant after `IMAGE_DELAY_MS`**

After the `IMAGE_DELAY_MS` declaration (section 3), add:
```js
const AUTO_SCROLL_SPEED = 1.5; // px per frame (~90px/s at 60fps)
```

- [ ] **Step 3: Replace `scheduleAutoAdvance()` with RAF functions**

Delete the entire `scheduleAutoAdvance()` function. In its place, add:
```js
function rafAutoScroll() {
  if (!autoScroll || autoScrollPaused) return;
  document.getElementById("scroll-view").scrollBy(0, AUTO_SCROLL_SPEED);
  autoScrollRafId = requestAnimationFrame(rafAutoScroll);
}

function startAutoScroll() {
  autoScrollPaused = false;
  autoScrollRafId = requestAnimationFrame(rafAutoScroll);
}

function stopAutoScroll() {
  if (autoScrollRafId !== null) {
    cancelAnimationFrame(autoScrollRafId);
    autoScrollRafId = null;
  }
}
```

- [ ] **Step 4: Update `toggleAutoScroll()`**

Replace the existing `toggleAutoScroll()`:
```js
function toggleAutoScroll() {
  autoScroll = !autoScroll;
  if (autoScroll) {
    startAutoScroll();
  } else {
    stopAutoScroll();
    autoScrollPaused = false;
  }
  updateControls();
}
```

- [ ] **Step 5: Remove the timer-reset block from `advance()`**

In `advance()`, find and delete:
```js
  // Reset auto-scroll timer on manual advance
  if (autoScroll) {
    clearTimeout(autoScrollTimer);
    scheduleAutoAdvance();
  }
```

The RAF loop continues uninterrupted by manual advances — no reset needed.

- [ ] **Step 6: Verify continuous drift in browser**

Open http://localhost:8080. Press `a` — page should drift slowly and continuously downward. Press `a` again — stops. Press `j` while drifting — jumps forward one item, drift continues without restarting.

- [ ] **Step 7: Commit**
```bash
git add src/static/app.js
git commit -m "feat: replace setTimeout auto-scroll with continuous requestAnimationFrame drift"
```

---

### Task 7: JS — GIF/video pause in auto-scroll

**Files:**
- Modify: `src/static/app.js`

- [ ] **Step 1: Add `getGifDuration()` after `triggerPrefetch()`**

```js
async function getGifDuration(url) {
  if (!url.startsWith("/api/media/proxy?")) return IMAGE_DELAY_MS;
  let buf;
  try {
    const resp = await fetch(url);
    if (!resp.ok) return IMAGE_DELAY_MS;
    buf = new Uint8Array(await resp.arrayBuffer());
  } catch {
    return IMAGE_DELAY_MS;
  }
  let ms = 0;
  for (let i = 0; i + 5 < buf.length; i++) {
    if (buf[i] === 0x21 && buf[i + 1] === 0xF9 && buf[i + 2] === 0x04) {
      ms += (buf[i + 4] + buf[i + 5] * 256) * 10; // 1/100s → ms
      i += 5;
    }
  }
  return ms > 0 ? Math.min(Math.max(ms, 50), 60_000) : IMAGE_DELAY_MS;
}
```

- [ ] **Step 2: Replace `mediaObserver` with the full version including auto-scroll pause logic**

Replace the entire `mediaObserver` declaration from Task 5 with:

```js
const mediaObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    const el = entry.target;
    const isVideo = el.tagName === "VIDEO";
    const isGif = el.tagName === "IMG" && el.dataset.type === "gif";

    if (entry.isIntersecting) {
      // Always play videos when in view
      if (isVideo) el.play().catch(() => {});

      // Auto-scroll: pause drift for video or GIF
      if (autoScroll && !autoScrollPaused && (isVideo || isGif)) {
        autoScrollPaused = true;
        stopAutoScroll();

        if (isVideo) {
          el.play().catch(() => {});
          const onEnded = () => {
            el.removeEventListener("ended", onEnded);
            advance(1);
            autoScrollPaused = false;
            if (autoScroll) startAutoScroll();
          };
          el.addEventListener("ended", onEnded);
        } else {
          // GIF: parse duration (cached on item object) then advance
          const proxyUrl = `/api/media/proxy?url=${encodeURIComponent(
            decodeURIComponent(new URL(el.src).searchParams.get("url") || "")
          )}`;
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
      }
    } else {
      // Pause videos when out of view
      if (isVideo) el.pause();
    }
  });
}, { threshold: 0.85 });
```

Note: `threshold: 0.85` (up from 0.5 in Task 5) so the pause triggers only when the media is substantially in view, not just peeking in from the edge.

- [ ] **Step 3: Register GIF img elements with `mediaObserver` in `createMediaEl()`**

In `createMediaEl()`, in the `else` (img) branch, update the GIF block:
```js
if (item.media_type === "gif") {
  el.dataset.type = "gif";
  mediaObserver.observe(el);
}
```
(Place after `el.dataset.type = "gif"` — the line added in Task 5.)

- [ ] **Step 4: Verify auto-scroll + video pause**

With auto-scroll on (`a`), let the page drift to a video:
- Drift pauses when video reaches 85% in view
- Video plays
- After `ended`: `advance(1)` fires, drift resumes
- If next item is also a video: drift pauses again immediately

- [ ] **Step 5: Verify auto-scroll + GIF pause**

With auto-scroll on, drift to a GIF:
- Drift pauses when GIF reaches 85% in view
- Open DevTools → after a moment, `items[n].gifDuration` shows the parsed ms value
- After that duration: `advance(1)` fires, drift resumes

- [ ] **Step 6: Commit**
```bash
git add src/static/app.js
git commit -m "feat: pause auto-scroll for videos and GIFs, parse GIF loop duration from binary"
```

---

### Task 8: Run tests + final verification

**Files:** None modified

- [ ] **Step 1: Run test suite**
```bash
uv run pytest
```
Expected: all tests pass, coverage ≥ 90%.

- [ ] **Step 2: Run linter**
```bash
uv run ruff check .
```
Expected: no errors (only JS changed).

- [ ] **Step 3: Full browser checklist**

Open http://localhost:8080 at ≥1024px:
- [ ] FAB absent
- [ ] Glass icon bar visible bottom-left, 4 icons
- [ ] Hover each icon → tooltip shows "Auto-scroll [a]", "Slideshow [s]", "Mute [m]", "Theme [d]"
- [ ] Press `a` → `#ctrl-autoscroll` highlights, page drifts continuously; press `a` again → stops
- [ ] Press `s` → `#ctrl-slideshow` highlights, slideshow mode activates
- [ ] Press `d` → theme toggles, icon updates in ctrl bar
- [ ] Press `m` → mute icon highlights
- [ ] Mouse wheel scrolls naturally (no jumps)
- [ ] `j`/`k` jump item-to-item smoothly
- [ ] Video in feed autoplays when scrolled into view; pauses when scrolled out
- [ ] Auto-scroll pauses on video, resumes after `ended`
- [ ] Resize to <768px → FAB visible, ctrl bar hidden, all FAB buttons work

- [ ] **Step 4: Commit any final tweaks**
```bash
git add -p
git commit -m "fix: <describe tweaks if any>"
```
