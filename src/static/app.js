// ---------------------------------------------------------------------------
// 1. Theme init — must run before any rendering to avoid a flash
// ---------------------------------------------------------------------------
const savedTheme = localStorage.getItem("theme");
if (savedTheme) document.documentElement.setAttribute("data-theme", savedTheme);

// ---------------------------------------------------------------------------
// 2. State
// ---------------------------------------------------------------------------
let items = [];           // all loaded items
let currentIndex = 0;     // index into items[]
let page = 0;             // next page to fetch
let loading = false;      // prevent concurrent fetches
let hasMore = true;       // false once the API returns an empty page
let autoScrollRafId = null;   // requestAnimationFrame handle
let autoScrollPaused = false; // paused waiting for video/gif
let autoScroll = false;   // auto-scroll active?
let slideshowMode = false; // slideshow or scroll mode
let activeSlide = "a";    // which slide layer is currently visible
let muted = true;         // video mute state
let showSeen = localStorage.getItem("showSeen") === "true";
let fetchGeneration = 0;

// ---------------------------------------------------------------------------
// 3. IMAGE_DELAY_MS — read from CSS variable injected by the backend
// ---------------------------------------------------------------------------
const IMAGE_DELAY_MS = parseInt(
  getComputedStyle(document.documentElement).getPropertyValue("--image-display-delay-ms").trim() || "5000",
  10,
);
const PREFETCH_AHEAD = parseInt(
  getComputedStyle(document.documentElement).getPropertyValue("--prefetch-ahead").trim() || "5",
  10,
);
const AUTO_SCROLL_SPEED = 1.5; // px per frame (~90px/s at 60fps)

// ---------------------------------------------------------------------------
// 4. Helper functions
// ---------------------------------------------------------------------------

function _discardFailedItem(wrap, el) {
  mediaObserver.unobserve(el);
  viewObserver.unobserve(wrap);
  wrap.remove();
  const idx = items.findIndex(i => i.id === wrap.dataset.id);
  if (idx !== -1) {
    items.splice(idx, 1);
    if (currentIndex > idx) currentIndex--;
    else if (currentIndex >= items.length) currentIndex = Math.max(0, items.length - 1);
  }
}

function createMediaEl(item) {
  const wrap = document.createElement("div");
  wrap.className = "media-item";
  wrap.dataset.id = item.id;
  if (item.seen_at) wrap.classList.add("seen");

  const spinner = document.createElement("div");
  spinner.className = "spinner";
  wrap.appendChild(spinner);

  let el;
  if (item.media_type === "video") {
    el = document.createElement("video");
    el.src = `/api/media/proxy?url=${encodeURIComponent(item.media_url)}`;
    el.controls = false;
    el.muted = muted;
    el.loop = false;
    el.autoplay = true;
    el.addEventListener("mouseenter", () => { el.controls = true; });
    el.addEventListener("mouseleave", () => { el.controls = false; });
    el.addEventListener("ended", () => { if (!autoScroll) advance(1); });
    el.addEventListener("loadeddata", () => { wrap.classList.add("loaded"); seenObserver.observe(wrap); });
    el.addEventListener("error", () => _discardFailedItem(wrap, el));
    mediaObserver.observe(el);
  } else {
    el = document.createElement("img");
    el.src = `/api/media/proxy?url=${encodeURIComponent(item.media_url)}`;
    el.loading = "lazy";
    if (item.media_type === "gif") {
      el.dataset.type = "gif";
      mediaObserver.observe(el);
    }
    el.addEventListener("load", () => { wrap.classList.add("loaded"); seenObserver.observe(wrap); });
    el.addEventListener("error", () => _discardFailedItem(wrap, el));
  }
  wrap.appendChild(el);

  // Register with observers (seenObserver deferred to load/loadeddata to avoid zero-height false positives)
  viewObserver.observe(wrap);

  return wrap;
}

async function fetchItems() {
  if (loading) return;
  loading = true;
  const gen = ++fetchGeneration;
  try {
    const resp = await fetch(`/api/items?unseen=${showSeen ? "false" : "true"}&page=${page}&size=50`);
    if (!resp.ok) return;
    const newItems = await resp.json();
    if (gen !== fetchGeneration) return;  // stale response, discard
    if (!newItems.length) {
      if (page === 0) document.getElementById("empty-state").classList.remove("hidden");
      hasMore = false;
      return;
    }
    document.getElementById("empty-state").classList.add("hidden");

    items = items.concat(newItems);
    page++;

    // Append to scroll view
    const list = document.getElementById("feed-list");
    newItems.forEach(item => list.appendChild(createMediaEl(item)));

    // Trigger prefetch hint for items ahead of current position
    if (items.length > 0) {
      triggerPrefetch(items[Math.min(currentIndex + 5, items.length - 1)].id);
    }
  } finally {
    loading = false;
  }
}

function triggerPrefetch(itemId) {
  fetch("/api/prefetch/hint", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ item_id: itemId }),
  }).catch(() => {}); // fire and forget
}

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

function maybeLoadMore() {
  if (hasMore && items.length - currentIndex < PREFETCH_AHEAD) fetchItems();
}

// ---------------------------------------------------------------------------
// 5. Seen observer
// ---------------------------------------------------------------------------
const seenObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    // Mark seen when item fully exits through the top (scrolled past)
    if (entry.isIntersecting || entry.boundingClientRect.bottom > (entry.rootBounds?.top ?? 0)) return;
    const id = entry.target.dataset.id;
    const item = items.find(i => i.id === id);
    if (!item || item.seen_at) return;
    item.seen_at = "pending"; // prevent double-post
    fetch(`/api/items/${id}/seen`, { method: "POST" })
      .then(r => r.json())
      .then(data => {
        item.seen_at = data.seen_at;
        entry.target.classList.add("seen");
      })
      .catch(() => {});
  });
}, { threshold: 0 });

// ---------------------------------------------------------------------------
// 5b. Viewport observer — keeps currentIndex in sync during native scroll
// ---------------------------------------------------------------------------
const viewObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (!entry.isIntersecting) return;
    const idx = items.findIndex(i => i.id === entry.target.dataset.id);
    if (idx !== -1) { currentIndex = idx; maybeLoadMore(); }
  });
}, { threshold: 0.5 });

// ---------------------------------------------------------------------------
// 5c. Media observer — autoplay videos/GIFs; pause/resume auto-scroll for media
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// 6. Navigation
// ---------------------------------------------------------------------------

function markItemSeen(item) {
  if (!item || item.seen_at) return;
  item.seen_at = "pending";
  const wrap = document.querySelector(`.media-item[data-id="${item.id}"]`);
  fetch(`/api/items/${item.id}/seen`, { method: "POST" })
    .then(r => r.json())
    .then(data => {
      item.seen_at = data.seen_at;
      if (wrap) wrap.classList.add("seen");
    })
    .catch(() => {});
}

function advance(delta) {
  const next = currentIndex + delta;
  if (next < 0 || next >= items.length) return;
  if (slideshowMode) markItemSeen(items[currentIndex]);
  currentIndex = next;

  if (slideshowMode) {
    showSlide(items[currentIndex]);
  } else {
    scrollToIndex(currentIndex);
  }
  maybeLoadMore();
}

function scrollToIndex(idx) {
  const els = document.querySelectorAll(".media-item");
  if (els[idx]) els[idx].scrollIntoView({ behavior: "smooth", block: "center" });
}

// ---------------------------------------------------------------------------
// 7. Auto-scroll — continuous RAF drift
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// 8. Slideshow
// ---------------------------------------------------------------------------

function showSlide(item) {
  if (!item || !item.media_url) return;

  const incoming = activeSlide === "a" ? "b" : "a";
  const inEl = document.getElementById(`slide-${incoming}`);
  const outEl = document.getElementById(`slide-${activeSlide}`);

  // Clear incoming layer and populate with new media
  inEl.innerHTML = "";
  const mediaEl = createMediaEl(item).querySelector("img, video");
  if (mediaEl) inEl.appendChild(mediaEl);

  // Crossfade
  inEl.classList.add("active");
  outEl.classList.remove("active");
  activeSlide = incoming;
}

function toggleSlideshow() {
  slideshowMode = !slideshowMode;
  document.getElementById("scroll-view").classList.toggle("hidden", slideshowMode);
  document.getElementById("slideshow-view").classList.toggle("hidden", !slideshowMode);

  localStorage.setItem("mode", slideshowMode ? "slideshow" : "scroll");

  if (slideshowMode && items.length > 0) {
    showSlide(items[currentIndex]);
  }
  updateControls();
}

// ---------------------------------------------------------------------------
// 9. Mute toggle
// ---------------------------------------------------------------------------

function toggleMute() {
  muted = !muted;
  document.querySelectorAll("video").forEach(v => { v.muted = muted; });
  updateControls();
}

// ---------------------------------------------------------------------------
// 10. Theme toggle
// ---------------------------------------------------------------------------

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || "dark";
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
  updateControls();
}

// ---------------------------------------------------------------------------
// 10b. Show-seen toggle
// ---------------------------------------------------------------------------

function toggleShowSeen() {
  stopAutoScroll();
  autoScrollPaused = false;
  showSeen = !showSeen;
  localStorage.setItem("showSeen", showSeen ? "true" : "false");
  document.getElementById("feed-list").classList.toggle("feed-list--show-all", showSeen);
  items = [];
  currentIndex = 0;
  page = 0;
  hasMore = true;
  ++fetchGeneration;  // invalidate any in-flight fetch
  document.getElementById("feed-list").innerHTML = "";
  document.getElementById("empty-state").classList.add("hidden");
  fetchItems();
  updateControls();
}

// ---------------------------------------------------------------------------
// 11. Key bindings
// ---------------------------------------------------------------------------

document.addEventListener("keydown", e => {
  // Ignore key events when focus is in an input/textarea
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;

  switch (e.key) {
    case "j":
    case "ArrowDown":
      e.preventDefault();
      advance(1);
      break;
    case "k":
    case "ArrowUp":
      e.preventDefault();
      advance(-1);
      break;
    case "a":
      toggleAutoScroll();
      break;
    case "s":
      toggleSlideshow();
      break;
    case "m":
      toggleMute();
      break;
    case "d":
      toggleTheme();
      break;
  }
});

// ---------------------------------------------------------------------------
// 12. Controls (FAB + desktop icon bar)
// ---------------------------------------------------------------------------

function updateControls() {
  const theme = document.documentElement.getAttribute("data-theme") || "dark";
  const icon = theme === "dark" ? "🌙" : "☀";

  // Mobile FAB buttons
  document.getElementById("fab-autoscroll").classList.toggle("active", autoScroll);
  document.getElementById("fab-slideshow").classList.toggle("active", slideshowMode);
  document.getElementById("fab-mute").classList.toggle("active", muted);
  document.getElementById("fab-theme").textContent = icon;

  // Desktop ctrl bar buttons
  document.getElementById("ctrl-autoscroll").classList.toggle("active", autoScroll);
  document.getElementById("ctrl-slideshow").classList.toggle("active", slideshowMode);
  document.getElementById("ctrl-mute").classList.toggle("active", muted);
  document.getElementById("ctrl-theme").textContent = icon;
  document.getElementById("fab-show-seen").classList.toggle("active", showSeen);
  document.getElementById("ctrl-show-seen").classList.toggle("active", showSeen);
}

function toggleFab() {
  const menu = document.getElementById("fab-menu");
  menu.classList.toggle("hidden");
  document.getElementById("fab").textContent = menu.classList.contains("hidden") ? "☰" : "✕";
}

document.addEventListener("click", e => {
  if (!e.target.closest("#fab-container")) {
    const menu = document.getElementById("fab-menu");
    if (!menu.classList.contains("hidden")) {
      menu.classList.add("hidden");
      document.getElementById("fab").textContent = "☰";
    }
  }
});

// ---------------------------------------------------------------------------
// 13. Swipe gestures (TikTok-style)
// ---------------------------------------------------------------------------

let _tx = 0, _ty = 0;
const SWIPE_MIN = 50;

document.addEventListener("touchstart", e => {
  _tx = e.touches[0].clientX;
  _ty = e.touches[0].clientY;
}, { passive: true });

document.addEventListener("touchend", e => {
  const dx = e.changedTouches[0].clientX - _tx;
  const dy = e.changedTouches[0].clientY - _ty;
  if (Math.abs(dx) < SWIPE_MIN && Math.abs(dy) < SWIPE_MIN) return;

  if (slideshowMode) {
    const forward = Math.abs(dy) >= Math.abs(dx) ? dy < 0 : dx < 0;
    advance(forward ? 1 : -1);
  } else {
    if (Math.abs(dy) >= Math.abs(dx)) advance(dy < 0 ? 1 : -1);
  }
}, { passive: true });

// ---------------------------------------------------------------------------
// 14. Startup sequence
// ---------------------------------------------------------------------------

// Restore view mode from localStorage (theme already applied at top of file)
if (localStorage.getItem("mode") === "slideshow") toggleSlideshow();

// Sync controls to initial state
updateControls();

// Restore show-all badge visibility if showSeen was persisted
if (showSeen) document.getElementById("feed-list").classList.add("feed-list--show-all");

// Initial item fetch
fetchItems();
