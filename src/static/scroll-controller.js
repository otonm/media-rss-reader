// ---------------------------------------------------------------------------
// scrollController
//
// A single IntersectionObserver with threshold 0.6 tracks which item is
// currently most visible. On change, emits 'currentindex-changed' (which
// itemStore.setCurrentIndex and feedView.setCurrentMedia respond to) and
// triggers a cacheQueue.rebuild around the new position.
//
// Also owns the 'seen' observer: when an item scrolls fully past the top of
// the viewport, POST /api/items/{id}/seen so the scheduler can prune it.
// On a successful POST we also call itemStore.markSeen + feedView.markSeen
// so the in-memory item and the live DOM wrap are updated to show the
// checkmark.
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
    state.seenObserver = new IntersectionObserver(onSeen, { threshold: 0 });
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
      if (entry.isIntersecting) return;
      if (entry.boundingClientRect.bottom > 0) return;
      const id = entry.target.dataset.id;
      // Fire-and-forget POST; on success, update the local state and the
      // live DOM so the seen checkmark appears without a refetch.
      fetch(`/api/items/${id}/seen`, { method: "POST" })
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (!data) return;
          MRR.itemStore.markSeen(id, data.seen_at);
          MRR.feedView.markSeen(id);
        })
        .catch(() => {});
    });
  }

  function observe(el) {
    state.observer.observe(el);
    state.seenObserver.observe(el);
  }

  MRR.scrollController = { init, observe };
})();
