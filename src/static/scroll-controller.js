// ---------------------------------------------------------------------------
// scrollController
//
// An IntersectionObserver (threshold 0.6) tracks the most-visible item for
// currentIndex, video play/pause, and cache rebuild.
//
// Seen-marking uses a scroll event listener on #feed: when items scroll
// completely above the viewport, POST /api/items/{id}/seen fires. This is
// independent of IntersectionObserver threshold crossings — no timing gap
// between placeholder and media-element replacement, no dependency on item
// heights or scroll-snap behaviour.
// ---------------------------------------------------------------------------

(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    observer: null,
    feed: null,
    scrollTimer: null,
  };

  function init() {
    state.observer = new IntersectionObserver(onIntersect, {
      threshold: 0.6,
    });

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

  function onFeedScroll() {
    clearTimeout(state.scrollTimer);
    state.scrollTimer = setTimeout(markItemsAboveViewport, 200);
  }

  function markItemsAboveViewport() {
    const feedTop = state.feed.getBoundingClientRect().top;
    const items = state.feed.querySelectorAll(".placeholder, .media-item");
    items.forEach((el) => {
      if (el.getBoundingClientRect().bottom <= feedTop) {
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
  }

  MRR.scrollController = { init, observe };
})();