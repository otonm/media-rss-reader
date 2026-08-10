// ---------------------------------------------------------------------------
// zoomController
//
// Zooms the current image to 100% (1 image pixel per CSS pixel) and back.
// Double-tap on mobile, double-click or `z` on desktop. Images only: videos
// keep their native controls and the browser's own double-click-to-fullscreen.
//
// Zoom is a CSS transform on the <img>, so nothing reflows and the existing
// overflow:hidden on .media-item / .gallery-slide clips the scaled picture to
// the viewport-sized item for free.
//
// Panning while zoomed:
//   desktop — the picture follows the mouse cursor, no button held
//   mobile  — one finger drags the picture 1:1
// While zoomed, touchmove is preventDefault-ed (and the wrap carries
// touch-action:none as a second line of defence), which holds BOTH the feed's
// vertical scroll and the gallery's horizontal swipe: on mobile the picture
// has to be zoomed out again before navigation works. On desktop the wheel and
// the navigation keys reset the zoom (see app.js) and move on.
//
// Public API:
//   init()
//   toggle(img, clientX, clientY)   // zoom in anchored at the point, or out
//   reset()                         // zoom out if zoomed, else nothing
//   isZoomed()
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const DOUBLE_TAP_MS = 350;  // max gap between the two taps
  const DOUBLE_TAP_PX = 30;   // max distance between them

  const state = {
    el: null,        // the zoomed <img>, null when not zoomed
    wrap: null,      // its .media-item
    scale: 1,
    // Geometry snapshotted at zoom time. Never re-measured while zoomed: the
    // element is transformed, so getBoundingClientRect would report the
    // scaled box and each pan would compound the error.
    baseW: 0, baseH: 0,
    contLeft: 0, contTop: 0, contW: 0, contH: 0,
    tx: 0, ty: 0,
    lastTap: null,   // {t, x, y} of the previous pointerup
    panFrom: null,   // {x, y, tx, ty} at the start of a finger drag
  };

  function clamp(v, lo, hi) {
    return v < lo ? lo : v > hi ? hi : v;
  }

  // How far the scaled picture may travel before its edge would come inside
  // the item. Zero on an axis the picture does not overflow.
  function maxPan() {
    return {
      x: Math.max(0, (state.baseW * state.scale - state.contW) / 2),
      y: Math.max(0, (state.baseH * state.scale - state.contH) / 2),
    };
  }

  function apply() {
    state.el.style.transform =
      `translate(${state.tx}px, ${state.ty}px) scale(${state.scale})`;
  }

  // Point the picture at the cursor/tap position: cursor on the left edge of
  // the item shows the picture's left edge, so the whole image is reachable
  // by sweeping the pointer across the item once.
  function panToPoint(clientX, clientY) {
    const max = maxPan();
    const fx = clamp((clientX - state.contLeft) / state.contW, 0, 1);
    const fy = clamp((clientY - state.contTop) / state.contH, 0, 1);
    state.tx = max.x * (1 - 2 * fx);
    state.ty = max.y * (1 - 2 * fy);
    apply();
  }

  function panByDrag(clientX, clientY) {
    const max = maxPan();
    state.tx = clamp(state.panFrom.tx + (clientX - state.panFrom.x), -max.x, max.x);
    state.ty = clamp(state.panFrom.ty + (clientY - state.panFrom.y), -max.y, max.y);
    apply();
  }

  function toggle(img, clientX, clientY) {
    if (state.el) {
      clear();
      return;
    }
    if (!img || img.tagName !== "IMG") return;
    const wrap = img.closest(".media-item");
    if (!wrap) return;
    const rect = img.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const scale = img.naturalWidth / rect.width;
    // Already at (or past) 100% — a downscale is not what "zoom to 100%"
    // means, so this gesture does nothing at all.
    if (!(scale > 1.01)) return;
    const cont = wrap.getBoundingClientRect();
    state.el = img;
    state.wrap = wrap;
    state.scale = scale;
    state.baseW = rect.width;
    state.baseH = rect.height;
    state.contLeft = cont.left;
    state.contTop = cont.top;
    state.contW = cont.width;
    state.contH = cont.height;
    img.classList.add("zoomed");
    // On the wrap, not the image: it is an ancestor of both the <img> and the
    // .gallery scroller, and touch-action intersects down the chain, so one
    // property stops the feed scrolling AND the gallery swiping — including
    // when the finger lands on the letterboxing beside the picture.
    wrap.style.touchAction = "none";
    panToPoint(clientX, clientY);
    MRR.autoscrollController?.suspend();
  }

  function clear() {
    const wrap = state.wrap;
    state.el.classList.remove("zoomed");
    state.el.style.transform = "";
    if (wrap) wrap.style.touchAction = "";
    state.el = null;
    state.wrap = null;
    state.panFrom = null;
    // Resume the autoscroll timer suspended on zoom-in. reset() is a no-op
    // when autoscroll is off.
    if (wrap) MRR.autoscrollController?.reset(wrap);
  }

  function reset() {
    if (state.el) clear();
  }

  function isZoomed() {
    return state.el !== null;
  }

  // One detector for mouse and touch alike. `dblclick` would cover the mouse
  // for free but does not fire reliably on a double tap in iOS Safari, and
  // two mechanisms would need a dedupe guard between them — more code than
  // this. No collision with app.js's swipe: that needs 40px of travel, this
  // needs the two taps within 30px of each other.
  function onPointerUp(e) {
    const prev = state.lastTap;
    const now = Date.now();
    state.lastTap = { t: now, x: e.clientX, y: e.clientY };
    if (!prev) return;
    if (now - prev.t > DOUBLE_TAP_MS) return;
    if (Math.abs(e.clientX - prev.x) > DOUBLE_TAP_PX) return;
    if (Math.abs(e.clientY - prev.y) > DOUBLE_TAP_PX) return;
    state.lastTap = null; // a triple tap is not a second double tap
    toggle(e.target, e.clientX, e.clientY);
  }

  // Desktop only: the picture follows the cursor. The finger is handled by the
  // touch listeners below, not here — once the browser decides a touch is a
  // scroll it fires pointercancel and no further pointermove arrives, which is
  // exactly the case the finger pan has to survive.
  function onPointerMove(e) {
    if (!state.el || e.pointerType === "touch") return;
    panToPoint(e.clientX, e.clientY);
  }

  function onTouchStart(e) {
    if (!state.el) return;
    const t = e.touches[0];
    state.panFrom = { x: t.clientX, y: t.clientY, tx: state.tx, ty: state.ty };
  }

  function onTouchMove(e) {
    if (!state.el || !state.panFrom) return;
    // Non-passive and preventDefault, deliberately: touch-action:none on the
    // wrap is not enough on its own — iOS Safari honours it inconsistently on
    // ancestors, and the gallery's own scroller took the gesture anyway. This
    // is what actually holds the feed and the gallery still while zoomed.
    e.preventDefault();
    if (e.touches.length > 1) return; // no pinch; ignore the extra fingers
    const t = e.touches[0];
    panByDrag(t.clientX, t.clientY);
  }

  function init() {
    const feed = document.getElementById("feed");
    feed.addEventListener("pointermove", onPointerMove);
    feed.addEventListener("pointerup", onPointerUp);
    feed.addEventListener("touchstart", onTouchStart, { passive: true });
    feed.addEventListener("touchmove", onTouchMove, { passive: false });
    feed.addEventListener("touchend", () => { state.panFrom = null; });
    // The snapshotted geometry is stale after a resize or a rotate. Dropping
    // the zoom is cheaper — and less surprising — than recomputing it.
    window.addEventListener("resize", reset);
  }

  MRR.zoomController = { init, toggle, reset, isZoomed };
})();
