// ---------------------------------------------------------------------------
// scrollController
//
// Two IntersectionObservers:
//   observer     (threshold 0.6) — tracks most-visible item for currentIndex,
//                                 video play/pause, and cache rebuild
//   seenObserver (threshold 0)   — fires when any element leaves the viewport
//                                 (binary enter/leave, no ratio crossing gap)
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

    // The item on screen when the tab closes never leaves the viewport, so no
    // leave event ever fires for it; mark it here instead.
    window.addEventListener("pagehide", markCurrent);
  }

  function markCurrent() {
    const item = MRR.itemStore.getItemAt(MRR.itemStore.getCurrentIndex());
    if (item) postSeen(item.id);
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
    // The element is authoritative for navigation; the store index only feeds
    // the cache queue and the debug overlay. findIndexById returns the FIRST
    // match, so deriving position from it cannot be trusted to point back here.
    MRR.feedView.setCurrentEl(best.target);
    const idx = MRR.itemStore.findIndexById(best.target.dataset.id);
    if (idx === -1) return;
    MRR.itemStore.setCurrentIndex(idx);
    const mediaEl = MRR.feedView.activeMediaEl(best.target);
    MRR.feedView.setCurrentMedia(mediaEl && mediaEl.tagName === "VIDEO" ? mediaEl : null);
    MRR.cacheQueue.rebuild(idx, MRR.config.feedInitialCount, MRR.itemStore.getItems());
    MRR.autoscrollController.reset(best.target);
    MRR.controls?.renderDebug();
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

  // sendBeacon, not fetch: the browser cancels in-flight fetches when the
  // tab closes, so marks made in the last moments of a session would be
  // lost. Beacons are queued by the browser and delivered regardless.
  // Marking locally first (rather than on the response) makes that possible
  // — there is no response to wait for. Same-origin, so the session cookie
  // rides along.
  function postSeen(id) {
    const item = MRR.itemStore.getItems().find((i) => i.id === id);
    if (!item || item.seen_at) return;
    // Optimistic: a dropped beacon loses the mark for this session. Add a
    // retry queue if the server-side count ever drifts noticeably from what
    // was scrolled past.
    MRR.itemStore.markSeen(id, new Date().toISOString());
    MRR.feedView.markSeen(id);
    // media_url rides along so the mark survives the row being pruned between
    // the page load and this scroll. prune_items evicts oldest-first and the
    // feed is served oldest-first, so the rows on screen are the ones it takes;
    // without this the beacon 404s against a deleted row and seen_media — the
    // record meant to outlive pruning — never hears about it.
    //
    // Omitted when the item carries no URL: encodeURIComponent(undefined) is
    // the string "undefined", which the server rejects anyway, so sending it
    // only makes the request lie about what it knows.
    const suffix = item.media_url ? `?media_url=${encodeURIComponent(item.media_url)}` : "";
    navigator.sendBeacon(`/api/items/${id}/seen${suffix}`);
  }

  function observe(el) {
    state.observer.observe(el);
    state.seenObserver.observe(el);
  }

  MRR.scrollController = { init, observe };
})();