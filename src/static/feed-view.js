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
// controller). All other videos are paused. (We deliberately do NOT mutate
// the old video's muted state on transition: setting `muted` on a paused
// video would fire `volumechange`, which the browser uses to infer user
// interaction in some implementations and would suppress future autoplay.)
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

  function createPlaceholder(item) {
    const wrap = document.createElement("div");
    wrap.className = "placeholder";
    wrap.dataset.id = item.id;
    const spinner = document.createElement("div");
    spinner.className = "spinner";
    wrap.appendChild(spinner);
    return wrap;
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
      // Track explicit user pauses only. The browser fires spurious
      // 'volumechange' and 'seeking' events for its own reasons (autoplay
      // policy adjustments, end-of-video seek-backs, visibility changes);
      // trusting those would suppress autoplay on the next visible
      // transition. The reliable signal is a `pause` event that was not
      // preceded by our own pause() call in setCurrentMedia — that is a
      // real user click on the browser controls.
      el.addEventListener("pause", () => {
        if (el._pausedByJs) {
          el._pausedByJs = false;
        } else {
          el.userPaused = true;
        }
      });
      // A subsequent `play` (user clicking the controls) clears userPaused
      // so a later scroll-back resumes autoplay. Without this the user's
      // explicit "play" intent is lost the moment they scroll away.
      el.addEventListener("play", () => { el.userPaused = false; });
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
    // Enforce the visible-media rule across the WHOLE feed, not just the
    // previous currentVisibleEl. Each video is created with autoplay=true
    // and starts as soon as it lands in the DOM; the old single-pause code
    // left the others running, so unmuting any one of them (via the global
    // mute toggle) leaked audio from non-visible items.
    if (state.feed) {
      state.feed.querySelectorAll("video").forEach((v) => {
        v._pausedByJs = true;
        v.pause();
      });
    }
    state.currentVisibleEl = el;
    if (el && el.tagName === "VIDEO") {
      el.muted = MRR.config.mutedDefault;
      if (!el.userPaused) {
        el.play().catch((err) => console.warn("video play rejected", err));
      }
    }
  }

  MRR.feedView = {
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
