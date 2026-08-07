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

    // The item on screen when the tab closes never leaves the viewport, so
    // neither trigger above would ever mark it and it returned every session.
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

  // sendBeacon, not fetch: the browser cancels in-flight fetches when the tab
  // closes, so marks made in the last moments of a session were silently lost
  // and those items came back on the next load. Beacons are queued by the
  // browser and delivered regardless. Marking locally first (rather than on
  // the response) is what makes that possible — there is no response to wait
  // for. Same-origin, so the session cookie rides along.
  function postSeen(id) {
    const item = MRR.itemStore.getItems().find((i) => i.id === id);
    if (!item || item.seen_at) return;
    // ponytail: optimistic — a dropped beacon loses the mark for this session,
    // exactly as the old swallowed fetch error did. Add a retry queue if the
    // server-side count ever drifts noticeably from what was scrolled past.
    MRR.itemStore.markSeen(id, new Date().toISOString());
    MRR.feedView.markSeen(id);
    // media_url rides along so the mark survives the row being pruned between
    // the page load and this scroll. prune_items evicts oldest-first and the
    // feed is served oldest-first, so the rows on screen are the ones it takes;
    // without this the beacon 404s against a deleted row and seen_media — the
    // record meant to outlive pruning — never hears about it.
    const q = encodeURIComponent(item.media_url);
    navigator.sendBeacon(`/api/items/${id}/seen?media_url=${q}`);
  }

  function observe(el) {
    state.observer.observe(el);
    state.seenObserver.observe(el);
  }

  MRR.scrollController = { init, observe };
})();