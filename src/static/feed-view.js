// ---------------------------------------------------------------------------
// feedView
//
// Renders the vertical scroll container. Each item in the feed is either
// a .placeholder (spinner) or a .media-item (img/video). On
// cacheQueue 'item-loaded', the placeholder is replaced with the loaded
// media element wrapped in a .media-item.
//
// The "visible media" rule: at most one <video> plays at a time — the one
// with the highest intersectionRatio, set by the scroll controller. All
// other videos are paused.
//
// Public API:
//   renderInitial(items)
//   createPlaceholder(item)
//   onItemLoaded(id, el)
//   onItemFailed(id)
//   snapToNext(), snapToPrev()
//   setCurrentMedia(el)
//   activeMediaEl(wrap)   // media of the active gallery slide (or single media)
//   wrapById(id)          // the rendered .media-item for an id, or null
//   currentWrap()         // the .media-item the feed is snapped to, or null
//   advanceOrNext(wrap)   // next gallery slide, or snapToNext on the last one
//   galleryNext(), galleryPrev()  // ←/→ slide stepping on the current item
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    feed: null,
    currentVisibleEl: null,   // currently-playing <video> or null
    currentEl: null,          // wrap the feed is snapped to, per the observer
    autoscrollBound: false,
  };

  // The rendered .media-item for an id, or null — a placeholder does not
  // count. This is what "current" means for navigation (spec.md §10.2).
  function wrapById(id) {
    return state.feed ? state.feed.querySelector(`.media-item[data-id="${id}"]`) : null;
  }

  // Either the placeholder or the loaded media-item for an id — the two
  // states a node in #feed can be in. Shared by isRendered and onItemFailed,
  // which (unlike wrapById) both need to catch a duplicate/failed item
  // regardless of which state it's in.
  function placeholderOrMediaSelector(id) {
    return `.placeholder[data-id="${id}"], .media-item[data-id="${id}"]`;
  }

  // Is this id already on screen — as a placeholder OR a loaded media-item?
  // The DOM is the source of truth for what is rendered — deliberately not a
  // Set kept alongside it, since a second copy of that answer is exactly
  // what drifts and puts an item on screen twice. Not wrapById: a duplicate
  // placeholder (not yet a .media-item) must be caught too.
  function isRendered(id) {
    return state.feed.querySelector(placeholderOrMediaSelector(id)) !== null;
  }

  // The only way a node enters #feed, with dedup. A duplicate node is not
  // cosmetic: findIndexById returns the FIRST store index with that id, so
  // landing on the second copy drags currentIndex back to the first.
  //
  // Returns the new placeholder, or null when the append was refused.
  function appendItem(item) {
    if (!state.feed) state.feed = document.getElementById("feed");
    if (isRendered(item.id)) {
      console.warn("feedView: refused duplicate append", item.id);
      return null;
    }
    const placeholder = createPlaceholder(item);
    state.feed.appendChild(placeholder);
    return placeholder;
  }

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
    el.muted = MRR.controls?.isMuted() ?? true;
    el.loop = !MRR.autoscrollController.isEnabled?.();
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
      buildGallery(wrap, galleryMedia, el, item.id);
    } else {
      if (item.media_type === "video") wireVideo(el);
      el.addEventListener("error", () => onItemFailed(item.id, "media load failed"));
      wrap.appendChild(el);
    }
    if (item.seen_at) tagAsSeen(wrap);
    return wrap;
  }

  // Builds a horizontally scrollable gallery: one .gallery-slide per media
  // entry plus a dot per slide on the lower edge. Slide 1 reuses the element
  // already downloaded by the cache queue; the remaining slides point at the
  // media proxy directly and load natively in the background.
  function buildGallery(wrap, mediaList, firstEl, itemId) {
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
        // Offscreen slides defer to the browser's own lazy loading. A 20-slide
        // gallery opening 20 connections at once exhausts the ~6-per-host
        // budget and starves the cache queue and /api/items behind it.
        el = MRR.mediaEl.create(m, itemId, { defer: true });
      }
      if (m.type === "video") wireVideo(el);
      el.addEventListener("error", () => removeSlide(wrap, gallery, dots, slide), { once: true });
      slide.appendChild(el);
      gallery.appendChild(slide);
      const dot = document.createElement("button");
      dot.className = "gallery-dot";
      dot.type = "button";
      dot.setAttribute("aria-label", `Go to slide ${i + 1}`);
      dots.appendChild(dot);
    });
    // Delegated, not one listener per dot: removeSlide shifts the indices, so a
    // captured loop index would point at the wrong slide afterwards.
    dots.addEventListener("click", (e) => {
      const dot = e.target.closest(".gallery-dot");
      const idx = Array.prototype.indexOf.call(dots.children, dot);
      if (idx < 0) return;
      e.stopPropagation();
      MRR.zoomController?.reset(); // before the scroll, same as the arrows below
      gallery.scrollTo({ left: idx * gallery.clientWidth, behavior: "smooth" });
    });
    gallery.addEventListener("scroll", () => {
      paintDots(gallery, dots);
      onGalleryScroll(wrap, gallery);
    }, { passive: true });
    wrap.appendChild(gallery);
    wrap.appendChild(dots);
    const prevBtn = document.createElement("button");
    prevBtn.className = "gallery-nav prev";
    prevBtn.type = "button";
    prevBtn.setAttribute("aria-label", "Previous slide");
    prevBtn.textContent = "\u276E";
    prevBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      // Before the smooth scroll, not after it lands, so the picture snaps
      // back to fitted in place — same as the ←/→ keys do in app.js.
      MRR.zoomController?.reset();
      MRR.feedView.galleryPrev();
    });
    const nextBtn = document.createElement("button");
    nextBtn.className = "gallery-nav next";
    nextBtn.type = "button";
    nextBtn.setAttribute("aria-label", "Next slide");
    nextBtn.textContent = "\u276F";
    nextBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      MRR.zoomController?.reset();
      MRR.feedView.galleryNext();
    });
    wrap.appendChild(prevBtn);
    wrap.appendChild(nextBtn);
    paintDots(gallery, dots);
  }

  // Live dot tracking. Mid-swipe the scroll position is fractional, so each dot
  // gets its closeness to the current slide as --t (1 centred, 0 a slide away)
  // and CSS interpolates size and brightness from it. Undebounced and with no
  // CSS transition: the scroll position IS the animation, for the arrows' smooth
  // scroll exactly as for a finger.
  function paintDots(gallery, dots) {
    const pos = gallery.clientWidth ? gallery.scrollLeft / gallery.clientWidth : 0;
    for (let i = 0; i < dots.children.length; i++) {
      dots.children[i].style.setProperty("--t", Math.max(0, 1 - Math.abs(i - pos)));
    }
  }

  // A slide's media failed to load: drop the slide and its dot so the
  // indicator stays in sync. With no slides left, fail the whole item.
  function removeSlide(wrap, gallery, dots, slide) {
    const idx = Array.prototype.indexOf.call(gallery.children, slide);
    slide.remove();
    if (dots.children[idx]) dots.children[idx].remove();
    if (gallery.children.length === 0) {
      onItemFailed(wrap.dataset.id, "every slide failed to load");
    } else if (gallery.children.length === 1) {
      dots.remove(); // a single remaining slide needs no indicator
      wrap.querySelectorAll(".gallery-nav").forEach((b) => b.remove());
    }
  }

  // Debounced gallery scroll: mark the active slide, pause offscreen slide
  // videos, and — when this wrap is the current feed item — re-point the
  // visible-media rule and autoscroll at the new slide. The dots do not wait
  // for this; paintDots runs on every scroll event.
  function onGalleryScroll(wrap, gallery) {
    clearTimeout(wrap._galleryScrollTimer);
    wrap._galleryScrollTimer = setTimeout(() => {
      const slides = gallery.children;
      if (slides.length === 0) return;
      const idx = Math.max(0, Math.min(Math.round(gallery.scrollLeft / gallery.clientWidth), slides.length - 1));
      if (slides[idx].classList.contains("active")) return;
      // A slide change is a navigation, same as a feed item change, so it
      // drops the zoom. This is the gallery's setCurrentEl: every way a slide
      // changes — the arrows, ←/→, a swipe, autoscroll — lands here, and a
      // zoom left behind on the slide we just left comes back with it.
      MRR.zoomController?.reset();
      for (let i = 0; i < slides.length; i++) {
        slides[i].classList.toggle("active", i === idx);
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

  // The wrap of the item the feed is currently snapped to, or null. Prefers the
  // element the observer reported over anything derived from a store index, for
  // the reason given on setCurrentEl.
  function currentWrap() {
    if (!state.feed) return null;
    if (state.currentEl && state.currentEl.className.includes("media-item")) return state.currentEl;
    const item = MRR.itemStore.getItemAt(MRR.itemStore.getCurrentIndex());
    return item ? wrapById(item.id) : null;
  }

  // Keyboard →: next gallery slide, or next feed item on the last slide.
  function galleryNext() {
    const wrap = currentWrap();
    if (wrap) advanceOrNext(wrap);
    else snapToNext();
  }

  // Keyboard ←: previous gallery slide, or previous feed item on the first.
  function galleryPrev() {
    const wrap = currentWrap();
    const gallery = wrap && wrap.querySelector(".gallery");
    if (gallery) {
      const idx = Math.round(gallery.scrollLeft / gallery.clientWidth);
      if (idx > 0) {
        gallery.scrollTo({ left: (idx - 1) * gallery.clientWidth, behavior: "smooth" });
        return;
      }
    }
    snapToPrev();
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
    items.forEach(appendItem);
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

  // An item whose media could not be downloaded leaves the feed entirely —
  // the node and the store entry together. The DOM and the store must stay
  // in sync: a node with no store entry makes onIntersect's findIndexById
  // return -1 and bails before the cache queue is rebuilt or autoscroll is
  // re-armed.
  function onItemFailed(id, reason) {
    const el = state.feed && state.feed.querySelector(placeholderOrMediaSelector(id));
    if (el) {
      // Close the feed up onto a neighbour before the node goes, rather than
      // letting the browser decide where the scroll lands once it vanishes.
      if (state.currentEl === el) {
        const next = el.nextElementSibling || el.previousElementSibling;
        state.currentEl = next;
        if (next) next.scrollIntoView({ block: "start" });
      }
      el.remove();
    }
    const idx = MRR.itemStore.findIndexById(id);
    if (idx !== -1) MRR.itemStore.getItems().splice(idx, 1);
    console.warn("feedView: dropped unloadable item", id, reason);
  }

  // Live checkmark: called by the scroll-controller after a successful
  // POST /api/items/{id}/seen. Idempotent.
  function markSeen(id) {
    const wrap = wrapById(id);
    if (!wrap) return;
    tagAsSeen(wrap);
  }

  // The wrap the observer last reported as most-visible. Navigation walks from
  // here rather than re-deriving the element from itemStore's index: the two
  // are separate index spaces (the store splices failed items), and sibling
  // walking from the observed element cannot be off by one.
  function setCurrentEl(el) {
    // Landing on a different item drops any zoom — the single choke point for
    // every path that moves the feed: keys, wheel, autoscroll advance, a touch
    // scroll started beside the picture, or onItemFailed closing a gap.
    if (el !== state.currentEl) MRR.zoomController?.reset();
    state.currentEl = el;
  }

  function snapTo(el) {
    if (el) el.scrollIntoView({ block: "start" });
  }

  function snapToNext() {
    const cur = state.currentEl;
    snapTo(cur ? cur.nextElementSibling : state.feed && state.feed.children[0]);
  }

  function snapToPrev() {
    const cur = state.currentEl;
    snapTo(cur ? cur.previousElementSibling : state.feed && state.feed.children[0]);
  }

  function setCurrentMedia(el) {
    if (state.currentVisibleEl === el) return;
    // Enforce the visible-media rule across the WHOLE feed, not just the
    // previous currentVisibleEl: every video is created with autoplay=true
    // and starts as soon as it lands in the DOM, so pausing only the previous
    // one leaves the rest playing — and unmuting any one of them (via the
    // global mute toggle) would leak audio from non-visible items.
    if (state.feed) {
      state.feed.querySelectorAll("video").forEach((v) => {
        v._pausedByJs = true;
        v.pause();
      });
    }
    state.currentVisibleEl = el;
    if (el && el.tagName === "VIDEO") {
      MRR.controls?.applyMute(el);
      if (!el.userPaused) {
        el.play().catch((err) => console.warn("video play rejected", err));
      }
    }
  }

  MRR.feedView = {
    createPlaceholder,
    appendItem,
    setCurrentEl,
    wrapById,
    renderInitial,
    onItemLoaded,
    onItemFailed,
    markSeen,
    snapToNext,
    snapToPrev,
    setCurrentMedia,
    activeMediaEl,
    currentWrap,
    advanceOrNext,
    galleryNext,
    galleryPrev,
  };
})();
