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
    trackingWrap: null,
    gifTimerId: null,
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
    const newWrap = best.target;
    if (state.trackingWrap !== newWrap) {
      clearSeenTracking();
      startSeenTracking(newWrap);
    }
    MRR.itemStore.setCurrentIndex(idx);
    MRR.feedView.setCurrentMedia(best.target.querySelector("video"));
    MRR.cacheQueue.rebuild(idx, MRR.config.feedInitialCount, MRR.itemStore.getItems());
    MRR.autoscrollController.reset(best.target);
  }

  function startSeenTracking(wrap) {
    const type = wrap.dataset.mediaType;
    if (!type) return;
    state.trackingWrap = wrap;

    if (type === "image") {
      wrap._actuallySeen = true;
    } else if (type === "gif") {
      const mediaEl = wrap.querySelector("img, canvas");
      const src = mediaEl ? mediaEl.src : "";
      MRR.autoscrollController.getGifDuration(src).then((ms) => {
        if (state.trackingWrap !== wrap) return;
        state.gifTimerId = setTimeout(() => {
          if (state.trackingWrap === wrap) wrap._actuallySeen = true;
        }, ms);
      });
    } else if (type === "video") {
      attachVideoSeenTracker(wrap);
    }
  }

  function attachVideoSeenTracker(wrap) {
    if (wrap._videoSeenHandler) return;
    const video = wrap.querySelector("video");
    if (!video) return;

    function checkProgress() {
      if (wrap._actuallySeen) {
        video.removeEventListener("timeupdate", checkProgress);
        wrap._videoSeenHandler = null;
        return;
      }
      if (video.duration && isFinite(video.duration) && video.currentTime / video.duration >= 0.5) {
        wrap._actuallySeen = true;
        video.removeEventListener("timeupdate", checkProgress);
        wrap._videoSeenHandler = null;
      }
    }

    wrap._videoSeenHandler = checkProgress;
    video.addEventListener("timeupdate", checkProgress);
    checkProgress();
  }

  function clearSeenTracking() {
    if (state.gifTimerId !== null) {
      clearTimeout(state.gifTimerId);
      state.gifTimerId = null;
    }
    state.trackingWrap = null;
  }

  function onSeen(entries) {
    entries.forEach((entry) => {
      if (entry.isIntersecting) return;
      if (entry.boundingClientRect.bottom > 0) return;
      if (!entry.target._actuallySeen) return;
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
