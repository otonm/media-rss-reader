// ---------------------------------------------------------------------------
// app.js — startup, configuration, keymap, module wiring
//
// Reads config from CSS custom properties injected by the backend in
// src/main.py:_build_html. Initializes all modules in dependency order
// and kicks off the initial feed load.
//
// Public API (exposed on MRR.app):
//   setShowSeen(on)           — toggle the seen-filter and refetch
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
      imageAutoscrollDelayMs: num("--image-autoscroll-delay-s", 2) * 1000,
      mediaLoadTimeoutMs: num("--media-load-timeout-s", 10) * 1000,
      zoomTransitionMs: num("--zoom-transition-ms", 200),
      uiDebug: num("--ui-debug", 0) === 1,
      autoscroll: false,
      mutedDefault: true,
    };
  }

  function readShowSeenPref() {
    try {
      return localStorage.getItem("showSeen") === "1";
    } catch (e) {
      return false;
    }
  }
  function writeShowSeenPref(on) {
    try {
      localStorage.setItem("showSeen", on ? "1" : "0");
    } catch (e) {
      // localStorage may be unavailable (private mode etc.) — silently ignore.
    }
  }

  // Refetch with the current showSeen filter, replacing the rendered
  // feed. Called on startup and whenever the user toggles the preference.
  //
  // The removed wraps' IntersectionObserver registrations stay registered
  // (scrollController exposes no unobserve); harmless while the feed is
  // empty.
  function reloadFeed() {
    document.getElementById("feed").replaceChildren();
    MRR.itemStore.resetForReload();
    MRR.itemStore.fetchPage()
      .then(() => {
        const items = MRR.itemStore.getItems();
        if (items.length === 0) return;
        MRR.feedView.renderInitial(items);
        MRR.cacheQueue.reset();
        MRR.cacheQueue.rebuild(0, MRR.config.feedInitialCount, items);
        document.getElementById("feed").scrollTop = 0;
      })
      .catch((err) => console.error("feed load failed", err));
  }

  function setShowSeen(on) {
    const next = !!on;
    writeShowSeenPref(next);
    MRR.itemStore.setShowSeen(next);
    reloadFeed();
  }

  function init() {
    readConfig();
    MRR.itemStore.setShowSeen(readShowSeenPref());
    MRR.scrollController.init();
    MRR.controls.init();
    MRR.zoomController.init();
    MRR.cacheQueue.on("item-loaded", (id, el, ms) => {
      MRR.controls?.recordLoadMs(id, ms);
      MRR.feedView.onItemLoaded(id, el);
      MRR.controls?.renderDebug();
    });
    MRR.cacheQueue.on("item-failed", (id, reason) => {
      // Report first: onItemFailed splices the store entry, and media_url lives
      // nowhere else.
      MRR.itemStore.reportUnusable(id);
      MRR.feedView.onItemFailed(id, reason);
      MRR.controls?.renderDebug();
    });
    MRR.cacheQueue.start();
    reloadFeed();

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
          MRR.zoomController.reset();
          MRR.feedView.snapToNext();
          break;
        case "k":
        case "ArrowUp":
          e.preventDefault();
          MRR.zoomController.reset();
          MRR.feedView.snapToPrev();
          break;
        case "ArrowLeft":
          e.preventDefault();
          MRR.zoomController.reset();
          MRR.feedView.galleryPrev();
          break;
        case "ArrowRight":
          e.preventDefault();
          MRR.zoomController.reset();
          MRR.feedView.galleryNext();
          break;
        case "z": {
          e.preventDefault();
          const wrap = MRR.feedView.currentWrap();
          const media = wrap ? MRR.feedView.activeMediaEl(wrap) : null;
          // Anchored at the viewport centre: the keyboard has no pointer, and
          // the mouse takes over the moment it moves.
          MRR.zoomController.toggle(media, window.innerWidth / 2, window.innerHeight / 2);
          break;
        }
        case "a":
          e.preventDefault();
          document.getElementById("btn-autoscroll").click();
          break;
        case "m":
          e.preventDefault();
          document.getElementById("btn-mute").click();
          break;
        case "s":
          e.preventDefault();
          document.getElementById("btn-show-seen").click();
          break;
      }
    });

    // Click-and-hold drag = swipe emulation for mouse/pen. Touch is left to
    // native scrolling. Coordinates are only read during the drag, and the
    // snap happens on release, so native scroll-snap and video controls keep
    // working. Below the threshold it is a plain click — arrow buttons,
    // video controls, etc. unaffected.
    const DRAG_THRESHOLD = 40; // px — below this is a click, not a swipe
    let dragStart = null;       // {x, y} on pointerdown, null when not dragging

    // Wheel over a zoomed image un-zooms it and lets the scroll through, so
    // the picture snaps back to fitted before it leaves the viewport.
    document.getElementById("feed").addEventListener(
      "wheel",
      () => MRR.zoomController.reset(),
      { passive: true },
    );

    document.getElementById("feed").addEventListener("pointerdown", (e) => {
      if (e.pointerType === "touch") return;
      // While zoomed the pointer pans the picture; a drag across it is not a swipe.
      if (MRR.zoomController.isZoomed()) return;
      // Video controls (seek, volume) have their own drag behavior;
      // intercepting them would fight the browser's native handling.
      if (e.target.closest?.("video")) return;
      dragStart = { x: e.clientX, y: e.clientY };
      const onUp = (ev) => {
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", onUp);
        if (!dragStart) return;
        const dx = ev.clientX - dragStart.x;
        const dy = ev.clientY - dragStart.y;
        dragStart = null;
        if (Math.max(Math.abs(dx), Math.abs(dy)) < DRAG_THRESHOLD) return;
        if (Math.abs(dx) > Math.abs(dy)) {
          if (dx > 0) MRR.feedView.galleryPrev();
          else        MRR.feedView.galleryNext();
        } else {
          if (dy > 0) MRR.feedView.snapToPrev();
          else        MRR.feedView.snapToNext();
        }
      };
      window.addEventListener("pointerup", onUp, { passive: true });
      window.addEventListener("pointercancel", onUp, { passive: true });
    });

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/static/sw.js").catch(() => {});
    }
  }

  MRR.app = { setShowSeen };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
