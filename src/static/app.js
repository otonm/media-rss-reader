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
let autoScrollTimer = null;  // timeout handle
let autoScroll = false;   // auto-scroll active?
let slideshowMode = false; // slideshow or scroll mode
let activeSlide = "a";    // which slide layer is currently visible
let muted = true;         // video mute state

// ---------------------------------------------------------------------------
// 3. IMAGE_DELAY_MS — read from CSS variable injected by the backend
// ---------------------------------------------------------------------------
const IMAGE_DELAY_MS = parseInt(
  getComputedStyle(document.documentElement).getPropertyValue("--image-display-delay-ms").trim() || "5000",
  10,
);

// ---------------------------------------------------------------------------
// 4. Helper functions
// ---------------------------------------------------------------------------

function createMediaEl(item) {
  const wrap = document.createElement("div");
  wrap.className = "media-item";
  wrap.dataset.id = item.id;

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
    el.addEventListener("mouseenter", () => { el.controls = true; });
    el.addEventListener("mouseleave", () => { el.controls = false; });
    el.addEventListener("ended", () => advance(1));
    el.addEventListener("loadeddata", () => wrap.classList.add("loaded"));
    el.addEventListener("error", () => wrap.classList.add("loaded"));
  } else {
    el = document.createElement("img");
    el.src = `/api/media/proxy?url=${encodeURIComponent(item.media_url)}`;
    el.loading = "lazy";
    el.addEventListener("load", () => wrap.classList.add("loaded"));
    el.addEventListener("error", () => wrap.classList.add("loaded"));
  }
  wrap.appendChild(el);

  // Register with the seen observer
  seenObserver.observe(wrap);

  return wrap;
}

async function fetchItems() {
  if (loading) return;
  loading = true;
  try {
    const resp = await fetch(`/api/items?unseen=false&page=${page}&size=50`);
    if (!resp.ok) return;
    const newItems = await resp.json();
    if (!newItems.length) {
      if (page === 0) document.getElementById("empty-state").classList.remove("hidden");
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

function maybeLoadMore() {
  if (items.length - currentIndex < 10) fetchItems();
}

// ---------------------------------------------------------------------------
// 5. Seen observer
// ---------------------------------------------------------------------------
const seenObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (!entry.isIntersecting) return;
    const id = entry.target.dataset.id;
    const item = items.find(i => i.id === id);
    if (!item || item.seen_at) return;
    item.seen_at = "pending"; // prevent double-post
    fetch(`/api/items/${id}/seen`, { method: "POST" })
      .then(r => r.json())
      .then(data => { item.seen_at = data.seen_at; })
      .catch(() => {});
  });
}, { threshold: 0.8 });

// ---------------------------------------------------------------------------
// 6. Navigation
// ---------------------------------------------------------------------------

function advance(delta) {
  const next = currentIndex + delta;
  if (next < 0 || next >= items.length) return;
  currentIndex = next;

  if (slideshowMode) {
    showSlide(items[currentIndex]);
  } else {
    scrollToIndex(currentIndex);
  }
  maybeLoadMore();

  // Reset auto-scroll timer on manual advance
  if (autoScroll) {
    clearTimeout(autoScrollTimer);
    scheduleAutoAdvance();
  }
}

function scrollToIndex(idx) {
  const els = document.querySelectorAll(".media-item");
  if (els[idx]) els[idx].scrollIntoView({ behavior: "smooth", block: "center" });
}

// ---------------------------------------------------------------------------
// 7. Auto-scroll
// ---------------------------------------------------------------------------

function scheduleAutoAdvance() {
  if (!autoScroll) return;
  const item = items[currentIndex];
  if (!item) return;

  if (item.media_type === "video") {
    // Video advances via the "ended" event — nothing to schedule here
    return;
  }

  autoScrollTimer = setTimeout(() => {
    advance(1);
    scheduleAutoAdvance();
  }, IMAGE_DELAY_MS);
}

function toggleAutoScroll() {
  autoScroll = !autoScroll;
  if (autoScroll) {
    scheduleAutoAdvance();
  } else {
    clearTimeout(autoScrollTimer);
    autoScrollTimer = null;
  }
  updateFab();
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
  updateFab();
}

// ---------------------------------------------------------------------------
// 9. Mute toggle
// ---------------------------------------------------------------------------

function toggleMute() {
  muted = !muted;
  document.querySelectorAll("video").forEach(v => { v.muted = muted; });
  updateFab();
}

// ---------------------------------------------------------------------------
// 10. Theme toggle
// ---------------------------------------------------------------------------

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || "dark";
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
  updateFab();
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
// 12. Mouse wheel
// ---------------------------------------------------------------------------

document.addEventListener("wheel", e => {
  if (e.deltaY > 0) advance(1);
  else if (e.deltaY < 0) advance(-1);
}, { passive: true });

// ---------------------------------------------------------------------------
// 13. FAB
// ---------------------------------------------------------------------------

function updateFab() {
  const theme = document.documentElement.getAttribute("data-theme") || "dark";
  document.getElementById("fab-autoscroll").classList.toggle("active", autoScroll);
  document.getElementById("fab-slideshow").classList.toggle("active", slideshowMode);
  document.getElementById("fab-mute").classList.toggle("active", muted);
  document.getElementById("fab-theme").textContent = theme === "dark" ? "🌙" : "☀";
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
// 14. Swipe gestures (TikTok-style)
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
// 15. Startup sequence
// ---------------------------------------------------------------------------

// Restore view mode from localStorage (theme already applied at top of file)
if (localStorage.getItem("mode") === "slideshow") toggleSlideshow();

// Sync FAB to initial state
updateFab();

// Initial item fetch
fetchItems();
