// ---------------------------------------------------------------------------
// cacheQueue
//
// A priority-ordered queue of item IDs. A single worker downloads one media
// file at a time via /api/media/proxy?url=... and emits 'item-loaded' or
// 'item-failed' on completion. No concurrent downloads.
//
// Public API:
//   start(), reset()
//   rebuild(currentIndex, lookaheadN, items)
//   on('item-loaded', (id, el) => ...)
//   on('item-failed', (id) => ...)
//
// The worker is a single coroutine. We do not preempt in-flight downloads.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    queue: [],          // Array<string> item IDs in priority order
    loadingId: null,    // currently being downloaded
    running: false,
    cached: new Set(),  // IDs that have finished loading successfully
  };

  const listeners = { "item-loaded": [], "item-failed": [] };

  function on(event, cb) {
    if (!listeners[event]) throw new Error("unknown event: " + event);
    listeners[event].push(cb);
  }
  function emit(event, ...args) {
    listeners[event].forEach((cb) => cb(...args));
  }

  function priorityRebuild(currentIndex, lookaheadN, items) {
    const newQueue = [];
    if (currentIndex >= 0 && currentIndex < items.length) {
      newQueue.push(items[currentIndex].id);
    }
    const forward = items.slice(currentIndex + 1, currentIndex + 1 + lookaheadN);
    forward.forEach((it) => newQueue.push(it.id));
    const behind = items.slice(0, currentIndex).reverse();
    behind.forEach((it) => { if (!state.cached.has(it.id)) newQueue.push(it.id); });
    items.slice(currentIndex + 1 + lookaheadN).forEach((it) => newQueue.push(it.id));
    state.queue = newQueue.filter((id) => !state.cached.has(id) && id !== state.loadingId);
  }

  async function processNext() {
    while (state.running && state.queue.length > 0) {
      const id = state.queue.shift();
      state.loadingId = id;
      try {
        const item = MRR.itemStore.getItems().find((i) => i.id === id);
        if (!item) { state.loadingId = null; continue; }
        const el = await downloadOne(item);
        state.cached.add(id);
        emit("item-loaded", id, el);
      } catch (err) {
        emit("item-failed", id);
      } finally {
        state.loadingId = null;
      }
    }
  }

  function downloadOne(item) {
    return new Promise((resolve, reject) => {
      const el = item.media_type === "video" ? document.createElement("video") : new Image();
      if (item.media_type === "video") {
        el.autoplay = true;
        el.setAttribute("playsinline", "");
        el.setAttribute("webkit-playsinline", "");
        el.muted = true;
        el.preload = "auto";
      }
      el.addEventListener(item.media_type === "video" ? "loadeddata" : "load", () => resolve(el), { once: true });
      el.addEventListener("error", () => reject(new Error("media load failed")), { once: true });
      el.src = `/api/media/proxy?url=${encodeURIComponent(item.media_url)}&item_id=${encodeURIComponent(item.id)}`;
    });
  }

  function start() {
    if (state.running) return;
    state.running = true;
    processNext();
  }
  // Clear the queue and the cached-id set so a future rebuild() starts
  // fresh. Does not stop the running worker. Used by the app when the
  // user toggles the seen-filter and the feed is refetched.
  function reset() {
    state.queue = [];
    state.loadingId = null;
    state.cached = new Set();
  }
  function rebuild(currentIndex, lookaheadN, items) {
    priorityRebuild(currentIndex, lookaheadN, items);
    if (state.running && state.loadingId === null) processNext();
    // Fire a server-side prewarm hint so the disk cache gets the next
    // PREFETCH_AHEAD items warmed while the browser-side worker is busy
    // downloading the lookahead. Fire-and-forget; failures are silent.
    if (currentIndex >= 0 && currentIndex < items.length) {
      const itemId = items[currentIndex].id;
      fetch("/api/prefetch/hint", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_id: itemId }),
      }).catch(() => {});
    }
  }

  MRR.cacheQueue = { on, start, reset, rebuild };
})();
