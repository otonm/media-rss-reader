// ---------------------------------------------------------------------------
// scrollController
//
// Two IntersectionObservers:
//   observer     (threshold 0.6) — tracks most-visible item for currentIndex,
//                                 video play/pause, and cache rebuild
//   seenObserver (threshold 0)   — fires when any element leaves the viewport
//                                 (binary enter/leave, no ratio crossing gap)
//
// A debounced scroll event listener on #feed acts as a secondary trigger
// (desktop browsers fire scroll events on overflow containers reliably).
// Both mechanisms call postSeen() which deduplicates via item.seen_at.
// ---------------------------------------------------------------------------

(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    observer: null,
    seenObserver: null,
    feed: null,
    scrollTimer: null,
  };

  function init() {
    state.observer = new IntersectionObserver(onIntersect, {
      threshold: 0.6,
    });
    state.seenObserver = new IntersectionObserver(onSeen, { threshold: 0 });

    state.feed = document.getElementById("feed");
    state.feed.addEventListener("scroll", onFeedScroll, { passive: true });
  }

  function onIntersect(entries) {
    let best = null;
    let bestRatio = 0;
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      if (entry.intersectionRatio > bestRatio) {
        best = entry;
        bestRatio = entry.intersectionRatio;
      }
    });
    if (!best) return;
    const idx = MRR.itemStore.findIndexById(best.target.dataset.id);
    if (idx === -1) return;
    MRR.itemStore.setCurrentIndex(idx);
    MRR.feedView.setCurrentMedia(best.target.querySelector("video"));
    MRR.cacheQueue.rebuild(idx, MRR.config.feedInitialCount, MRR.itemStore.getItems());
    MRR.autoscrollController.reset(best.target);
  }

  // Threshold 0 observer: fires on every binary enter/leave of the viewport.
  // Marks items as seen when they leave the viewport upward (scrolled past).
  // Skips placeholders (no mediaType) — the observer fires again when the
  // placeholder is replaced by a .media-item with the new element's current
  // intersection state.
  function onSeen(entries) {
    entries.forEach((entry) => {
      if (entry.isIntersecting) return;
      if (entry.boundingClientRect.bottom > 0) return;
      if (!entry.target.dataset.mediaType) return;
      postSeen(entry.target.dataset.id);
    });
  }

  function onFeedScroll() {
    clearTimeout(state.scrollTimer);
    state.scrollTimer = setTimeout(markItemsAboveViewport, 200);
  }

  function markItemsAboveViewport() {
    const feedTop = state.feed.getBoundingClientRect().top;
    const items = state.feed.querySelectorAll(".placeholder, .media-item");
    items.forEach((el) => {
      // 1px tolerance: scroll-snap snaps items to exact viewport height;
      // getBoundingClientRect().bottom can be 0.0001 on high-DPI displays.
      if (el.getBoundingClientRect().bottom <= feedTop + 1) {
        postSeen(el.dataset.id);
      }
    });
  }

  function postSeen(id) {
    const item = MRR.itemStore.getItems().find((i) => i.id === id);
    if (!item || item.seen_at) return;
    fetch(`/api/items/${id}/seen`, { method: "POST" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data) return;
        MRR.itemStore.markSeen(id, data.seen_at);
        MRR.feedView.markSeen(id);
      })
      .catch(() => {});
  }

  function observe(el) {
    state.observer.observe(el);
    state.seenObserver.observe(el);
  }

  MRR.scrollController = { init, observe };
})();