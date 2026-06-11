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
    let cleared = false;
    function clearAll() {
      if (cleared) return;
      cleared = true;
      if (intervalId !== null) { clearInterval(intervalId); intervalId = null; }
      video.removeEventListener("progress", evaluate);
      video.removeEventListener("canplay", evaluate);
      video.removeEventListener("playing", onPlaying);
    }
    function evaluate() {
      if (state.currentVisibleEl !== video) { clearAll(); return; }
      const pct = bufferedPct(video);
      const fs = forwardSeconds(video);
      if (pct >= cfg.videoBufferThresholdPct || fs >= cfg.videoBufferThresholdMinS) {
        video.play().catch(() => {});
        clearAll();
      }
    }
    function onPlaying() { clearAll(); }
    video.addEventListener("progress", evaluate);
    video.addEventListener("canplay", evaluate);
    intervalId = setInterval(evaluate, 100);
    video.addEventListener("playing", onPlaying);
  }

  function createMediaWrap(item, el) {
    const wrap = document.createElement("div");
    wrap.className = "media-item";
    wrap.dataset.id = item.id;
    wrap.dataset.mediaType = item.media_type;
    if (item.media_type === "video") {
      el.setAttribute("playsinline", "");
      el.setAttribute("webkit-playsinline", "");
      el.setAttribute("controls", "");
      el.muted = MRR.config.mutedDefault;
      el.loop = !MRR.config.autoscroll;
      // Track user interaction with the video's browser controls so that
      // setCurrentMedia can stop auto-playing a video the user has
      // personally paused/seeked/volume-adjusted. The `pause` event
      // handler ignores the event when it follows a JS-initiated pause
      // (set via _pausedByJs in setCurrentMedia).
      el.addEventListener("seeking", () => { el.userInteracted = true; });
      el.addEventListener("volumechange", () => { el.userInteracted = true; });
      el.addEventListener("pause", () => {
        if (el._pausedByJs) {
          el._pausedByJs = false;
        } else {
          el.userInteracted = true;
        }
      });
      // NOTE: do NOT set el.autoplay here. The visible-media rule drives
      // playback: setCurrentMedia calls playWhenBufferedAndVisible when
      // this video becomes the current visible one. Setting autoplay
      // would bypass that gate.
      el.addEventListener("error", () => onItemFailed(item.id));
    } else {
      el.addEventListener("error", () => onItemFailed(item.id));
    }
    wrap.appendChild(el);
    if (item.seen_at) tagAsSeen(wrap);
    return wrap;
  }

  // Add the `.seen` class and a small corner badge to a wrap. Idempotent:
  // calling it twice does not stack badges.
  function tagAsSeen(wrap) {
    if (wrap.className.includes("seen")) return;
    wrap.className = (wrap.className + " seen").trim();
    const badge = document.createElement("span");
    badge.className = "seen-badge";
    badge.setAttribute("aria-label", "seen");
    badge.textContent = "✓";
    wrap.appendChild(badge);
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
    // Only call bindIfVisible when the newly loaded item IS the currently
    // visible one. bindIfVisible rebinds unconditionally, so calling it
    // for a lookahead item would steal the `ended` listener from the
    // item the user is actually watching. The scroll-controller will
    // call bindIfVisible via reset() when the visible item changes.
    const currentItem = MRR.itemStore.getItemAt(MRR.itemStore.getCurrentIndex());
    if (currentItem && currentItem.id === id) {
      MRR.autoscrollController.bindIfVisible(wrap);
    }
  }

  function onItemFailed(id) {
    const el = state.feed.querySelector(`.placeholder[data-id="${id}"], .media-item[data-id="${id}"]`);
    if (!el) return;
    el.remove();
    const idx = MRR.itemStore.findIndexById(id);
    if (idx !== -1) MRR.itemStore.getItems().splice(idx, 1);
  }

  // Live checkmark: called by the scroll-controller after a successful
  // POST /api/items/{id}/seen. Idempotent.
  function markSeen(id) {
    const wrap = state.feed?.querySelector(`.media-item[data-id="${id}"]`);
    if (!wrap) return;
    tagAsSeen(wrap);
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
      // Mark the old video's pause as JS-initiated so the `pause` event
      // handler does not interpret it as a user interaction.
      if (state.currentVisibleEl.tagName === "VIDEO") {
        state.currentVisibleEl._pausedByJs = true;
      }
      state.currentVisibleEl.pause();
      state.currentVisibleEl.muted = true;
    }
    state.currentVisibleEl = el;
    if (el && el.tagName === "VIDEO") {
      el.muted = MRR.config.mutedDefault;
      // Skip auto-play for videos the user has personally interacted with
      // (paused, seeked, adjusted volume). The browser's own controls
      // remain available for the user to start playback themselves.
      if (!el.userInteracted) {
        playWhenBufferedAndVisible(el);
      }
    }
  }

  MRR.feedView = {
    on,
    createPlaceholder,
    renderInitial,
    onItemLoaded,
    onItemFailed,
    markSeen,
    snapToIndex,
    snapToNext,
    snapToPrev,
    setCurrentMedia,
  };
})();
