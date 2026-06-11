// ---------------------------------------------------------------------------
// feedView
//
// Renders the vertical scroll container. Each item in the feed is either
// a .placeholder (spinner) or a .media-item (img/video). On
// cacheQueue 'item-loaded', the placeholder is replaced with the loaded
// media element wrapped in a .media-item.
//
// The "visible media" rule: at most one <video> plays at a time. The visible
// video is the one with the highest intersectionRatio (set by the scroll
// controller). All other videos are paused and muted.
//
// Public API:
//   on('currentindex-changed', ...)  // forwards scroll-controller events
//   renderInitial(items)
//   createPlaceholder(item)            // exposed for app.js's "append more"
//   onItemLoaded(id, el)
//   onItemFailed(id)
//   snapToIndex(idx), snapToNext(), snapToPrev()
//   setCurrentMedia(el)
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    feed: null,
    currentVisibleEl: null,   // currently-playing <video> or null
    autoscrollBound: false,
  };

  const listeners = { "currentindex-changed": [] };
  function on(event, cb) {
    if (!listeners[event]) throw new Error("unknown event: " + event);
    listeners[event].push(cb);
  }
  function emit(event, ...args) { listeners[event].forEach((cb) => cb(...args)); }

  function createPlaceholder(item) {
    const wrap = document.createElement("div");
    wrap.className = "placeholder";
    wrap.dataset.id = item.id;
    const spinner = document.createElement("div");
    spinner.className = "spinner";
    wrap.appendChild(spinner);
    return wrap;
  }

  // Returns the forward-seconds buffered past the playhead, or null if the
  // buffer is empty / not yet reported. Used by the buffer-threshold logic.
  function forwardSeconds(video) {
    const b = video.buffered;
    if (!b.length) return 0;
    let total = 0;
    for (let i = 0; i < b.length; i++) {
      const start = b.start(i);
      const end = b.end(i);
      if (end >= video.currentTime) total += end - Math.max(start, video.currentTime);
    }
    return total;
  }

  function bufferedPct(video) {
    if (!video.duration || !isFinite(video.duration)) return 0;
    const b = video.buffered;
    if (!b.length) return 0;
    return (b.end(b.length - 1) / video.duration) * 100;
  }

  // Wait until the buffer reaches the configured threshold AND `video` is
  // the currently visible media, then play. The 'progress' event fires on
  // each buffer growth; we evaluate on each. On browsers where 'progress'
  // is sparse (notably iOS Safari), a 100ms setInterval re-evaluates the
  // same condition until the video starts. We also short-circuit if the
  // video stops being the visible one (e.g. the user scrolled away).
  function playWhenBufferedAndVisible(video) {
    const cfg = MRR.config;
    let intervalId = null;
    function clearAll() {
      if (intervalId !== null) { clearInterval(intervalId); intervalId = null; }
    }
    function evaluate() {
      if (state.currentVisibleEl !== video) { clearAll(); return; }
      const pct = bufferedPct(video);
      const fs = forwardSeconds(video);
      if (pct >= cfg.videoBufferThresholdPct || fs >= cfg.videoBufferThresholdMinS) {
        video.play().catch(() => {});
        clearAll();
        video.removeEventListener("progress", evaluate);
        video.removeEventListener("canplay", evaluate);
      }
    }
    video.addEventListener("progress", evaluate);
    video.addEventListener("canplay", evaluate);
    intervalId = setInterval(evaluate, 100);
    video.addEventListener("playing", clearAll, { once: true });
  }

  function createMediaWrap(item, el) {
    const wrap = document.createElement("div");
    wrap.className = "media-item";
    wrap.dataset.id = item.id;
    wrap.dataset.mediaType = item.media_type;
    if (item.media_type === "video") {
      el.setAttribute("playsinline", "");
      el.setAttribute("webkit-playsinline", "");
      el.muted = MRR.config.mutedDefault;
      el.loop = !MRR.config.autoscroll;
      // NOTE: do NOT set el.autoplay here. The visible-media rule drives
      // playback: setCurrentMedia calls playWhenBufferedAndVisible when
      // this video becomes the current visible one. Setting autoplay
      // would bypass that gate.
      el.addEventListener("error", () => onItemFailed(item.id));
      // The cache-queue download already produced 'loadeddata' on `el`, so
      // the first frame is paintable now; we still gate .play() on the
      // buffer threshold so playback doesn't stall mid-stream.
      playWhenBufferedAndVisible(el);
    } else {
      el.addEventListener("error", () => onItemFailed(item.id));
    }
    wrap.appendChild(el);
    return wrap;
  }

  function renderInitial(items) {
    state.feed = document.getElementById("feed");
    items.forEach((it) => state.feed.appendChild(createPlaceholder(it)));
  }

  function onItemLoaded(id, el) {
    const placeholder = state.feed.querySelector(`.placeholder[data-id="${id}"]`);
    if (!placeholder) return; // placeholder already removed (e.g. scrolled past)
    const item = MRR.itemStore.getItems().find((i) => i.id === id);
    if (!item) return;
    const wrap = createMediaWrap(item, el);
    placeholder.replaceWith(wrap);
    MRR.scrollController.observe(wrap);
    MRR.autoscrollController.bindIfVisible(wrap);
  }

  function onItemFailed(id) {
    const el = state.feed.querySelector(`.placeholder[data-id="${id}"], .media-item[data-id="${id}"]`);
    if (!el) return;
    el.remove();
    const idx = MRR.itemStore.findIndexById(id);
    if (idx !== -1) MRR.itemStore.getItems().splice(idx, 1);
  }

  function snapToIndex(idx) {
    const items = state.feed.querySelectorAll(".placeholder, .media-item");
    if (items[idx]) items[idx].scrollIntoView({ block: "start" });
  }
  function snapToNext() {
    snapToIndex(MRR.itemStore.getCurrentIndex() + 1);
  }
  function snapToPrev() {
    snapToIndex(MRR.itemStore.getCurrentIndex() - 1);
  }

  function setCurrentMedia(el) {
    if (state.currentVisibleEl === el) return;
    if (state.currentVisibleEl && state.currentVisibleEl !== el) {
      state.currentVisibleEl.pause();
      state.currentVisibleEl.muted = true;
    }
    state.currentVisibleEl = el;
    if (el && el.tagName === "VIDEO") {
      el.muted = MRR.config.mutedDefault;
      playWhenBufferedAndVisible(el);
    }
  }

  MRR.feedView = {
    on,
    createPlaceholder,
    renderInitial,
    onItemLoaded,
    onItemFailed,
    snapToIndex,
    snapToNext,
    snapToPrev,
    setCurrentMedia,
  };
})();
