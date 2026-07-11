// ---------------------------------------------------------------------------
// scrollController
//
// Two IntersectionObservers:
//   observer      (threshold 0.6) — tracks the most-visible item, drives
//                                  currentIndex, video play/pause, cache rebuild
//   seenObserver  (threshold 0.8) — fires POST /api/items/{id}/seen when an
//                                  item becomes 80% visible (the user is
//                                  looking at it). Also handles the fallback
//                                  case: media loads after the user already
//                                  scrolled past (element above viewport).
//
// Dedup: the seen POST is only sent if the in-memory item's seen_at is null.
// The browser stores the returned seen_at on the item object via
// itemStore.markSeen, preventing a second POST for the same item.
// ---------------------------------------------------------------------------

(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    observer: null,
    seenObserver: null,
  };

  function init() {
    state.observer = new IntersectionObserver(onIntersect, {
      threshold: 0.6,
    });
    state.seenObserver = new IntersectionObserver(onSeen, { threshold: 0.8 });
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

  function onSeen(entries) {
    entries.forEach((entry) => {
      if (!entry.target.dataset.mediaType) return; // skip placeholders/spinners
      const id = entry.target.dataset.id;
      const item = MRR.itemStore.getItems().find((i) => i.id === id);
      if (!item || item.seen_at) return; // dedup: already marked seen

      // Primary: item entered viewport and is 80%+ visible.
      if (entry.isIntersecting && entry.intersectionRatio >= 0.8) {
        postSeen(id);
        return;
      }
      // Fallback: media loaded after user already scrolled past (element above viewport).
      if (entry.boundingClientRect.bottom <= 0) {
        postSeen(id);
      }
    });
  }

  function postSeen(id) {
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
