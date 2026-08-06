// ---------------------------------------------------------------------------
// cacheQueue
//
// A priority-ordered queue of item IDs. A small pool of workers downloads
// media via /api/media/proxy?url=... and emits 'item-loaded' or 'item-failed'
// on completion.
//
// Public API:
//   start(), reset()
//   rebuild(currentIndex, lookaheadN, items)
//   getStats()                — for the UI_DEBUG overlay
//   on('item-loaded', (id, el, ms) => ...)
//   on('item-failed', (id, reason) => ...)
//
// Workers do not preempt an in-flight download; they abort it on a deadline
// instead. A single worker used to mean one slow origin froze every
// placeholder behind it, including items already cached on disk that would
// have painted instantly.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  // Browsers allow ~6 connections per host over HTTP/1.1, and gallery slides
  // load outside this queue. 3 leaves room for those, /api/items and the
  // prefetch hints.
  const WORKERS = 3;
  // A download that has not produced a decodable frame by now is treated as
  // failed and surfaced to the user, rather than spinning forever.
  const LOAD_TIMEOUT_MS = 10000;

  const state = {
    queue: [],          // Array<string> item IDs in priority order
    loading: new Set(), // IDs currently being downloaded
    running: false,
    workers: 0,         // live worker count
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

  // Items the server told us are already on disk decode in milliseconds, so
  // they go first within each band. The current item is exempt: it is what the
  // user is looking at and must load whether or not it is cached.
  function cachedFirst(items) {
    const hit = [];
    const miss = [];
    items.forEach((it) => (it.cached ? hit : miss).push(it.id));
    return hit.concat(miss);
  }

  function priorityRebuild(currentIndex, lookaheadN, items) {
    const newQueue = [];
    if (currentIndex >= 0 && currentIndex < items.length) {
      newQueue.push(items[currentIndex].id);
    }
    const forward = items.slice(currentIndex + 1, currentIndex + 1 + lookaheadN);
    cachedFirst(forward).forEach((id) => newQueue.push(id));
    const behind = items.slice(0, currentIndex).reverse().filter((it) => !state.cached.has(it.id));
    cachedFirst(behind).forEach((id) => newQueue.push(id));
    cachedFirst(items.slice(currentIndex + 1 + lookaheadN)).forEach((id) => newQueue.push(id));
    state.queue = newQueue.filter((id) => !state.cached.has(id) && !state.loading.has(id));
  }

  async function processNext() {
    state.workers += 1;
    try {
      while (state.running && state.queue.length > 0) {
        const id = state.queue.shift();
        if (state.loading.has(id) || state.cached.has(id)) continue;
        state.loading.add(id);
        try {
          const item = MRR.itemStore.getItems().find((i) => i.id === id);
          if (!item) continue;
          const started = Date.now();
          const el = await downloadOne(item);
          state.cached.add(id);
          emit("item-loaded", id, el, Date.now() - started);
        } catch (err) {
          emit("item-failed", id, err && err.message ? err.message : "load failed");
        } finally {
          state.loading.delete(id);
        }
      }
    } finally {
      state.workers -= 1;
    }
  }

  // Top the worker pool back up. processNext() runs synchronously as far as its
  // first await, so state.workers and state.queue are both already updated by
  // the time the loop re-tests them.
  function pump() {
    if (!state.running) return;
    while (state.workers < WORKERS && state.queue.length > 0) processNext();
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
      const timer = setTimeout(() => {
        el.src = ""; // abort the in-flight request so the connection is freed
        reject(new Error("timed out after " + LOAD_TIMEOUT_MS / 1000 + "s"));
      }, LOAD_TIMEOUT_MS);
      const settle = (fn, arg) => { clearTimeout(timer); fn(arg); };
      el.addEventListener(
        item.media_type === "video" ? "loadeddata" : "load",
        () => settle(resolve, el),
        { once: true }
      );
      el.addEventListener(
        "error",
        () => settle(reject, new Error("media load failed")),
        { once: true }
      );
      el.src = `/api/media/proxy?url=${encodeURIComponent(item.media_url)}&item_id=${encodeURIComponent(item.id)}`;
    });
  }

  function start() {
    if (state.running) return;
    state.running = true;
    pump();
  }
  // Clear the queue and the cached-id set so a future rebuild() starts
  // fresh. Does not stop running workers. Used by the app when the
  // user toggles the seen-filter and the feed is refetched.
  function reset() {
    state.queue = [];
    state.loading = new Set();
    state.cached = new Set();
  }
  // rebuild() runs on every scroll snap (scroll-controller.js:62). Each hint
  // costs the server two ROW_NUMBER passes over the whole items table on the
  // connection /api/items shares, so an undebounced burst made the endpoint
  // that exists to smooth scrolling compete with the scroll itself.
  let hintTimer = null;
  function sendHint(itemId, unseen) {
    if (hintTimer !== null) clearTimeout(hintTimer);
    hintTimer = setTimeout(() => {
      hintTimer = null;
      fetch("/api/prefetch/hint", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_id: itemId, unseen }),
      }).catch(() => {});
    }, 250);
  }
  function rebuild(currentIndex, lookaheadN, items) {
    priorityRebuild(currentIndex, lookaheadN, items);
    pump();
    // Fire a server-side prewarm hint so the disk cache gets the next
    // PREFETCH_AHEAD items warmed while the browser-side workers are busy
    // downloading the lookahead. Fire-and-forget; failures are silent.
    if (currentIndex >= 0 && currentIndex < items.length) {
      sendHint(items[currentIndex].id, !MRR.itemStore.getShowSeen());
    }
  }

  // Snapshot for the UI_DEBUG overlay.
  function getStats() {
    return { queued: state.queue.length, loading: state.loading.size, done: state.cached.size };
  }

  MRR.cacheQueue = { on, start, reset, rebuild, getStats };
})();
