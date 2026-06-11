// ---------------------------------------------------------------------------
// app.js — startup, configuration, keymap, module wiring
//
// Reads config from CSS custom properties injected by the backend in
// src/main.py:_build_html. Initializes all modules in dependency order
// and kicks off the initial feed load.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  function readConfig() {
    const root = document.documentElement;
    const cs = getComputedStyle(root);
    const num = (name, fallback) => {
      const v = parseInt(cs.getPropertyValue(name).trim(), 10);
      return Number.isFinite(v) ? v : fallback;
    };
    MRR.config = {
      feedInitialCount: num("--feed-initial-count", 10),
      videoBufferThresholdPct: num("--video-buffer-threshold-pct", 10),
      videoBufferThresholdMinS: num("--video-buffer-threshold-min-s", 2),
      imageAutoscrollDelayMs: num("--image-autoscroll-delay-s", 2) * 1000,
      autoscroll: false,
      mutedDefault: true,
    };
  }

  function init() {
    readConfig();
    MRR.scrollController.init();
    MRR.controls.init();
    MRR.cacheQueue.on("item-loaded", (id, el) => MRR.feedView.onItemLoaded(id, el));
    MRR.cacheQueue.on("item-failed", (id) => MRR.feedView.onItemFailed(id));

    // Initial load: fetch total + first page, then render + start the queue.
    Promise.resolve()
      .then(() => MRR.itemStore.fetchCount())
      .then(() => MRR.itemStore.fetchPage())
      .then(() => {
        const items = MRR.itemStore.getItems();
        if (items.length === 0) {
          document.getElementById("counter").textContent = "0 / 0";
          return;
        }
        MRR.feedView.renderInitial(items);
        MRR.cacheQueue.rebuild(0, MRR.config.feedInitialCount, items);
        MRR.cacheQueue.start();
        MRR.controls.updateCounter();
      })
      .catch((err) => console.error("startup failed", err));

    // Observe placeholders so the very first currentIndex fires once they're mounted.
    const mo = new MutationObserver(() => {
      const placeholders = document.querySelectorAll("#feed .placeholder");
      if (placeholders.length === 0) return;
      placeholders.forEach((p) => MRR.scrollController.observe(p));
      mo.disconnect();
    });
    mo.observe(document.getElementById("feed"), { childList: true });

    // Keymap
    document.addEventListener("keydown", (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      switch (e.key) {
        case "j":
        case "ArrowDown":
          e.preventDefault();
          MRR.feedView.snapToNext();
          break;
        case "k":
        case "ArrowUp":
          e.preventDefault();
          MRR.feedView.snapToPrev();
          break;
        case "a":
          e.preventDefault();
          document.getElementById("btn-autoscroll").click();
          break;
        case "m":
          e.preventDefault();
          document.getElementById("btn-mute").click();
          break;
      }
    });

    // Periodic check to fetch more pages when nearing the end of the loaded list.
    setInterval(() => {
      const cur = MRR.itemStore.getCurrentIndex();
      const total = MRR.itemStore.getItems().length;
      if (MRR.itemStore.hasMoreItems() && total - cur < MRR.config.feedInitialCount) {
        MRR.itemStore.fetchPage().then(() => {
          const items = MRR.itemStore.getItems();
          const feed = document.getElementById("feed");
          const existing = new Set(
            Array.from(feed.children).map((el) => el.dataset.id)
          );
          items.forEach((it) => {
            if (existing.has(it.id)) return;
            const placeholder = MRR.feedView.createPlaceholder(it);
            feed.appendChild(placeholder);
            MRR.scrollController.observe(placeholder);
          });
        });
      }
    }, 2000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
