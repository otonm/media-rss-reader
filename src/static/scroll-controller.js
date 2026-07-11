// ---------------------------------------------------------------------------
// scrollController
//
// A single IntersectionObserver (threshold 0.6) tracks the most-visible item.
// When the most-visible item changes, the previous one is marked as seen via
// POST /api/items/{id}/seen. This piggybacks on the existing "which item is
// being viewed" determination — no separate seen observer, no threshold
// crossing timing gap between placeholder and media-element replacement.
// ---------------------------------------------------------------------------

(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    observer: null,
    lastSeenItemId: null,
  };

  function init() {
    state.observer = new IntersectionObserver(onIntersect, {
      threshold: 0.6,
    });
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
    const newId = best.target.dataset.id;
    const idx = MRR.itemStore.findIndexById(newId);
    if (idx === -1) return;

    // Mark the previous most-visible item as seen (user scrolled past it).
    if (state.lastSeenItemId && state.lastSeenItemId !== newId) {
      postSeen(state.lastSeenItemId);
    }
    state.lastSeenItemId = newId;

    MRR.itemStore.setCurrentIndex(idx);
    MRR.feedView.setCurrentMedia(best.target.querySelector("video"));
    MRR.cacheQueue.rebuild(idx, MRR.config.feedInitialCount, MRR.itemStore.getItems());
    MRR.autoscrollController.reset(best.target);
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
