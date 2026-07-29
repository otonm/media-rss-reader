// ---------------------------------------------------------------------------
// feedView
//
// Renders the vertical scroll container. Each item in the feed is either
// a .placeholder (spinner) or a .media-item (img/video). On
// cacheQueue 'item-loaded', the placeholder is replaced with the loaded
// media element wrapped in a .media-item.
//
// The "visible media" rule: at most one <video> plays at a time. The visible
// video is the one with the highest intersectionRatio (set by the scroll
// controller). All other videos are paused. (We deliberately do NOT mutate
// the old video's muted state on transition: setting `muted` on a paused
// video would fire `volumechange`, which the browser uses to infer user
// interaction in some implementations and would suppress future autoplay.)
//
// Public API:
//   on('currentindex-changed', ...)  // forwards scroll-controller events
//   renderInitial(items)
//   createPlaceholder(item)            // exposed for app.js's "append more"
//   onItemLoaded(id, el)
//   onItemFailed(id)
//   snapToIndex(idx), snapToNext(), snapToPrev()
//   setCurrentMedia(el)
//   activeMediaEl(wrap)   // media of the active gallery slide (or single media)
//   advanceOrNext(wrap)   // next gallery slide, or snapToNext on the last one
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    feed: null,
    currentVisibleEl: null,   // currently-playing <video> or null
    autoscrollBound: false,
  };

  function createPlaceholder(item) {
    const wrap = document.createElement("div");
    wrap.className = "placeholder";
    wrap.dataset.id = item.id;
    const spinner = document.createElement("div");
    spinner.className = "spinner";
    wrap.appendChild(spinner);
    return wrap;
  }

  // Shared wiring for every video element (single items and gallery slides).
  // We deliberately do NOT mutate the old video's muted state on transition:
  // setting `muted` on a paused video would fire `volumechange`, which the
  // browser uses to infer user interaction in some implementations and would
  // suppress future autoplay.
  function wireVideo(el) {
    el.setAttribute("playsinline", "");
    el.setAttribute("webkit-playsinline", "");
    el.setAttribute("controls", "");
    el.muted = MRR.config.mutedDefault;
    el.loop = !MRR.config.autoscroll;
    // Track explicit user pauses only. The browser fires spurious
    // 'volumechange' and 'seeking' events for its own reasons (autoplay
    // policy adjustments, end-of-video seek-backs, visibility changes);
    // trusting those would suppress autoplay on the next visible
    // transition. The reliable signal is a `pause` event that was not
    // preceded by our own pause() call in setCurrentMedia — that is a
    // real user click on the browser controls.
    el.addEventListener("pause", () => {
      if (el._pausedByJs) {
        el._pausedByJs = false;
      } else {
        el.userPaused = true;
      }
    });
    // A subsequent `play` (user clicking the controls) clears userPaused
    // so a later scroll-back resumes autoplay. Without this the user's
    // explicit "play" intent is lost the moment they scroll away.
    el.addEventListener("play", () => { el.userPaused = false; });
  }

  function createMediaWrap(item, el) {
    const wrap = document.createElement("div");
    wrap.className = "media-item";
    wrap.dataset.id = item.id;
    wrap.dataset.mediaType = item.media_type;
    const count = Array.isArray(item.media) ? item.media.length : 1;
    const countBadge = document.createElement("span");
    countBadge.className = "count-badge";
    countBadge.textContent = count;
    wrap.appendChild(countBadge);
    const galleryMedia = Array.isArray(item.media) && item.media.length > 1 ? item.media : null;
    if (galleryMedia) {
      buildGallery(wrap, galleryMedia, el);
    } else {
      if (item.media_type === "video") wireVideo(el);
      el.addEventListener("error", () => onItemFailed(item.id));
      wrap.appendChild(el);
    }
    if (item.seen_at) tagAsSeen(wrap);
    return wrap;
  }

  // Builds a horizontally scrollable gallery: one .gallery-slide per media
  // entry plus a dot per slide on the lower edge. Slide 1 reuses the element
  // already downloaded by the cache queue; the remaining slides point at the
  // media proxy directly and load natively in the background.
  function buildGallery(wrap, mediaList, firstEl) {
    const gallery = document.createElement("div");
    gallery.className = "gallery";
    const dots = document.createElement("div");
    dots.className = "gallery-dots";
    mediaList.forEach((m, i) => {
      const slide = document.createElement("div");
      slide.className = "gallery-slide" + (i === 0 ? " active" : "");
      slide.dataset.mediaType = m.type;
      let el;
      if (i === 0) {
        el = firstEl;
      } else {
        el = m.type === "video" ? document.createElement("video") : new Image();
        el.src = `/api/media/proxy?url=${encodeURIComponent(m.url)}`;
      }
      if (m.type === "video") wireVideo(el);
      el.addEventListener("error", () => removeSlide(wrap, gallery, dots, slide), { once: true });
      slide.appendChild(el);
      gallery.appendChild(slide);
      const dot = document.createElement("span");
      dot.className = "gallery-dot" + (i === 0 ? " active" : "");
      dots.appendChild(dot);
    });
    gallery.addEventListener("scroll", () => onGalleryScroll(wrap, gallery, dots), { passive: true });
    wrap.appendChild(gallery);
    wrap.appendChild(dots);
  }

  // A slide's media failed to load: drop the slide and its dot so the
  // indicator stays in sync. With no slides left, fail the whole item.
  function removeSlide(wrap, gallery, dots, slide) {
    const idx = Array.prototype.indexOf.call(gallery.children, slide);
    slide.remove();
    if (dots.children[idx]) dots.children[idx].remove();
    if (gallery.children.length === 0) {
      onItemFailed(wrap.dataset.id);
    } else if (gallery.children.length === 1) {
      dots.remove(); // a single remaining slide needs no indicator
    }
  }

  // Debounced gallery scroll: mark the active slide + dot, pause offscreen
  // slide videos, and — when this wrap is the current feed item — re-point
  // the visible-media rule and autoscroll at the new slide.
  function onGalleryScroll(wrap, gallery, dots) {
    clearTimeout(wrap._galleryScrollTimer);
    wrap._galleryScrollTimer = setTimeout(() => {
      const slides = gallery.children;
      if (slides.length === 0) return;
      const idx = Math.max(0, Math.min(Math.round(gallery.scrollLeft / gallery.clientWidth), slides.length - 1));
      if (slides[idx].classList.contains("active")) return;
      for (let i = 0; i < slides.length; i++) {
        slides[i].classList.toggle("active", i === idx);
        if (dots.children[i]) dots.children[i].classList.toggle("active", i === idx);
      }
      gallery.querySelectorAll("video").forEach((v) => {
        if (v.closest(".gallery-slide") !== slides[idx]) { v._pausedByJs = true; v.pause(); }
      });
      const currentItem = MRR.itemStore.getItemAt(MRR.itemStore.getCurrentIndex());
      if (currentItem && currentItem.id === wrap.dataset.id) {
        const mediaEl = slides[idx].querySelector("img, video");
        setCurrentMedia(mediaEl && mediaEl.tagName === "VIDEO" ? mediaEl : null);
        MRR.autoscrollController.reset(wrap);
      }
    }, 60);
  }

  // The media element the user is currently looking at: the active gallery
  // slide's media, or the single media element for non-gallery wraps.
  function activeMediaEl(wrap) {
    const slide = wrap.querySelector(".gallery-slide.active");
    if (slide) return slide.querySelector("img, video");
    return wrap.querySelector(":scope > img, :scope > video");
  }

  // Autoscroll advance: galleries step to the next slide first; on the last
  // slide (or for non-gallery items) advance the feed itself.
  function advanceOrNext(wrap) {
    const gallery = wrap.querySelector(".gallery");
    if (gallery) {
      const idx = Math.round(gallery.scrollLeft / gallery.clientWidth);
      if (idx < gallery.children.length - 1) {
        gallery.scrollTo({ left: (idx + 1) * gallery.clientWidth, behavior: "smooth" });
        return; // the gallery scroll handler rebinds autoscroll to the new slide
      }
    }
    snapToNext();
  }

  // Add the `.seen` class and a small corner badge to a wrap. Idempotent:
  // calling it twice does not stack badges.
  function tagAsSeen(wrap) {
    if (wrap.className.includes("seen")) return;
    wrap.className = (wrap.className + " seen").trim();
    const badge = document.createElement("span");
    badge.className = "seen-badge";
    badge.setAttribute("aria-label", "seen");
    badge.textContent = "✓";
    wrap.appendChild(badge);
  }

  function renderInitial(items) {
    state.feed = document.getElementById("feed");
    items.forEach((it) => state.feed.appendChild(createPlaceholder(it)));
  }

  function onItemLoaded(id, el) {
    const placeholder = state.feed.querySelector(`.placeholder[data-id="${id}"]`);
    if (!placeholder) return; // placeholder already removed (e.g. scrolled past)
    const item = MRR.itemStore.getItems().find((i) => i.id === id);
    if (!item) return;
    const wrap = createMediaWrap(item, el);
    placeholder.replaceWith(wrap);
    MRR.scrollController.observe(wrap);
    // Only call bindIfVisible when the newly loaded item IS the currently
    // visible one. bindIfVisible rebinds unconditionally, so calling it
    // for a lookahead item would steal the `ended` listener from the
    // item the user is actually watching. The scroll-controller will
    // call bindIfVisible via reset() when the visible item changes.
    const currentItem = MRR.itemStore.getItemAt(MRR.itemStore.getCurrentIndex());
    if (currentItem && currentItem.id === id) {
      MRR.autoscrollController.bindIfVisible(wrap);
    }
  }

  function onItemFailed(id) {
    const el = state.feed.querySelector(`.placeholder[data-id="${id}"], .media-item[data-id="${id}"]`);
    if (!el) return;
    el.remove();
    const idx = MRR.itemStore.findIndexById(id);
    if (idx !== -1) MRR.itemStore.getItems().splice(idx, 1);
  }

  // Live checkmark: called by the scroll-controller after a successful
  // POST /api/items/{id}/seen. Idempotent.
  function markSeen(id) {
    const wrap = state.feed?.querySelector(`.media-item[data-id="${id}"]`);
    if (!wrap) return;
    tagAsSeen(wrap);
  }

  function snapToIndex(idx) {
    const items = state.feed.querySelectorAll(".placeholder, .media-item");
    if (items[idx]) items[idx].scrollIntoView({ block: "start" });
  }
  function snapToNext() {
    snapToIndex(MRR.itemStore.getCurrentIndex() + 1);
  }
  function snapToPrev() {
    snapToIndex(MRR.itemStore.getCurrentIndex() - 1);
  }

  function setCurrentMedia(el) {
    if (state.currentVisibleEl === el) return;
    // Enforce the visible-media rule across the WHOLE feed, not just the
    // previous currentVisibleEl. Each video is created with autoplay=true
    // and starts as soon as it lands in the DOM; the old single-pause code
    // left the others running, so unmuting any one of them (via the global
    // mute toggle) leaked audio from non-visible items.
    if (state.feed) {
      state.feed.querySelectorAll("video").forEach((v) => {
        v._pausedByJs = true;
        v.pause();
      });
    }
    state.currentVisibleEl = el;
    if (el && el.tagName === "VIDEO") {
      el.muted = MRR.config.mutedDefault;
      if (!el.userPaused) {
        el.play().catch((err) => console.warn("video play rejected", err));
      }
    }
  }

  MRR.feedView = {
    createPlaceholder,
    renderInitial,
    onItemLoaded,
    onItemFailed,
    markSeen,
    snapToIndex,
    snapToNext,
    snapToPrev,
    setCurrentMedia,
    activeMediaEl,
    advanceOrNext,
  };
})();
