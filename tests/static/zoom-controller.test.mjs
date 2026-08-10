// ---------------------------------------------------------------------------
// zoom-controller.test.mjs — zoom-to-100%: the scale decision, the pan clamp,
// the double-tap detector, and the in/out transition.
//
// The parts worth pinning: an image already at or above 100% must not zoom at
// all (a "zoom" that downscales is not what the gesture means), and the pan
// offset must never exceed the point where the picture's edge comes inside
// the item — that clamp is the whole reason the picture cannot be dragged
// into the void.
// ---------------------------------------------------------------------------

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { createDomContext, fakeTimeout, loadScript } from "./dom-mock.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../src/static");

// Container 1000x800 at the viewport origin; picture rendered 800x600.
const CONT_W = 1000;
const CONT_H = 800;

function rect(left, top, width, height) {
  return { left, top, width, height, right: left + width, bottom: top + height };
}

// naturalWidth 1600 against a rendered 800 => scale 2 => maxPan {x:300, y:200}.
function setupHarness({ naturalWidth = 1600, zoomTransitionMs = 200 } = {}) {
  const ctx = createDomContext();
  const clock = fakeTimeout(ctx);
  ctx.window.MRR.config = { zoomTransitionMs };
  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  ctx.document.register(feed);

  const wrap = ctx.document.createElement("div");
  wrap.className = "media-item";
  wrap.dataset.id = "id0";
  wrap.getBoundingClientRect = () => rect(0, 0, CONT_W, CONT_H);
  feed.appendChild(wrap);

  const img = ctx.document.createElement("img");
  img.naturalWidth = naturalWidth;
  img.naturalHeight = naturalWidth * 0.75;
  img.getBoundingClientRect = () => rect(100, 100, 800, 600);
  wrap.appendChild(img);

  const autoscroll = { suspended: 0, resets: [] };
  ctx.window.MRR.autoscrollController = {
    suspend() { autoscroll.suspended += 1; },
    reset(w) { autoscroll.resets.push(w.dataset.id); },
  };

  loadScript(resolve(STATIC, "zoom-controller.js"), ctx);
  ctx.window.MRR.zoomController.init();
  return { ctx, feed, wrap, img, autoscroll, clock, zoom: ctx.window.MRR.zoomController };
}

// Pull the numbers back out of `translate(Xpx, Ypx) scale(S)`.
function readTransform(img) {
  const m = /translate\((-?[\d.]+)px, (-?[\d.]+)px\) scale\(([\d.]+)\)/.exec(img.style.transform);
  assert.ok(m, `unparseable transform: ${img.style.transform}`);
  return { tx: Number(m[1]), ty: Number(m[2]), scale: Number(m[3]) };
}

function tap(feed, target, clientX, clientY, pointerType = "mouse") {
  feed.dispatchEvent({ type: "pointerup", target, clientX, clientY, pointerType });
}

// Returns the event so a test can assert whether preventDefault was called —
// that call is what stops the feed and the gallery scrolling under the finger.
function touch(feed, type, target, clientX, clientY) {
  const evt = {
    type,
    target,
    touches: [{ clientX, clientY }],
    prevented: false,
    preventDefault() { evt.prevented = true; },
  };
  feed.dispatchEvent(evt);
  return evt;
}

test("zooms to 1:1 and anchors the picture at the tap point", () => {
  const { feed, wrap, img, autoscroll, zoom } = setupHarness();

  // Tapped dead centre: the picture stays centred.
  zoom.toggle(img, CONT_W / 2, CONT_H / 2);
  assert.equal(zoom.isZoomed(), true);
  assert.deepEqual(readTransform(img), { tx: 0, ty: 0, scale: 2 });
  assert.equal(img.classList.contains("zoomed"), true);
  assert.equal(wrap.style.touchAction, "none");
  assert.equal(autoscroll.suspended, 1);

  // Toggling out restores everything and re-arms autoscroll.
  zoom.toggle(img, CONT_W / 2, CONT_H / 2);
  assert.equal(zoom.isZoomed(), false);
  assert.equal(img.style.transform, "");
  assert.equal(img.classList.contains("zoomed"), false);
  assert.equal(wrap.style.touchAction, "");
  assert.deepEqual(autoscroll.resets, ["id0"]);
});

test("a tap on the item's edge reveals that edge of the picture, clamped", () => {
  const { img, zoom } = setupHarness();

  zoom.toggle(img, 0, 0); // top-left corner of the item
  // maxPan = ((800*2)-1000)/2 = 300 horizontally, ((600*2)-800)/2 = 200 vertically.
  assert.deepEqual(readTransform(img), { tx: 300, ty: 200, scale: 2 });

  zoom.reset();
  zoom.toggle(img, CONT_W, CONT_H); // bottom-right corner
  assert.deepEqual(readTransform(img), { tx: -300, ty: -200, scale: 2 });
});

test("does nothing when the picture is already at 100% or smaller", () => {
  const { img, autoscroll, zoom } = setupHarness({ naturalWidth: 400 });

  zoom.toggle(img, CONT_W / 2, CONT_H / 2);
  assert.equal(zoom.isZoomed(), false);
  assert.equal(img.style.transform, undefined);
  assert.equal(autoscroll.suspended, 0);
});

test("ignores videos", () => {
  const { ctx, wrap, zoom } = setupHarness();
  const video = ctx.document.createElement("video");
  video.naturalWidth = 1600;
  video.getBoundingClientRect = () => rect(100, 100, 800, 600);
  wrap.appendChild(video);

  zoom.toggle(video, CONT_W / 2, CONT_H / 2);
  assert.equal(zoom.isZoomed(), false);
});

test("two quick taps in the same spot toggle the zoom; a slow pair does not", () => {
  const { feed, img, zoom } = setupHarness();

  tap(feed, img, 500, 400);
  tap(feed, img, 505, 402); // within 350ms and 30px
  assert.equal(zoom.isZoomed(), true);

  tap(feed, img, 500, 400);
  tap(feed, img, 900, 400); // 400px away — two unrelated single taps
  assert.equal(zoom.isZoomed(), true, "far-apart taps must not toggle");

  tap(feed, img, 500, 400);
  tap(feed, img, 500, 400);
  assert.equal(zoom.isZoomed(), false);
});

test("the mouse pans by position, a finger pans by drag, both clamped", () => {
  const { feed, img, zoom } = setupHarness();
  zoom.toggle(img, CONT_W / 2, CONT_H / 2);

  // Mouse: the picture follows the cursor across the item.
  feed.dispatchEvent({ type: "pointermove", target: img, clientX: 0, clientY: CONT_H / 2, pointerType: "mouse" });
  assert.equal(readTransform(img).tx, 300);

  // Finger: 1:1 drag from wherever the picture currently sits, clamped at 300.
  touch(feed, "touchstart", img, 500, 400);
  assert.equal(touch(feed, "touchmove", img, 400, 400).prevented, true);
  assert.equal(readTransform(img).tx, 200, "300 - 100 of drag");
  touch(feed, "touchmove", img, 5000, 400);
  assert.equal(readTransform(img).tx, 300, "clamped to maxPan");
});

// The mobile bug: touch-action:none alone did not hold the gallery's own
// scroller (nor the feed on iOS), so a finger drag scrolled to the next slide
// instead of moving the picture. Only a preventDefault-ed touchmove does.
test("a finger drag while zoomed cancels the browser's scroll", () => {
  const { feed, img, zoom } = setupHarness();

  touch(feed, "touchstart", img, 500, 400);
  assert.equal(touch(feed, "touchmove", img, 400, 400).prevented, false,
    "not zoomed: the feed and the gallery must still scroll normally");

  zoom.toggle(img, CONT_W / 2, CONT_H / 2);
  touch(feed, "touchstart", img, 500, 400);
  assert.equal(touch(feed, "touchmove", img, 400, 400).prevented, true);

  // Lifting the finger ends the drag; a stray move must not resume it.
  feed.dispatchEvent({ type: "touchend", target: img });
  touch(feed, "touchmove", img, 100, 400);
  assert.equal(readTransform(img).tx, -100, "unchanged by the post-touchend move");
});

test("a second finger is ignored rather than pinching", () => {
  const { feed, img, zoom } = setupHarness();
  zoom.toggle(img, CONT_W / 2, CONT_H / 2);
  touch(feed, "touchstart", img, 500, 400);

  const evt = { type: "touchmove", target: img, touches: [{ clientX: 400, clientY: 400 }, { clientX: 900, clientY: 900 }], preventDefault() { evt.prevented = true; } };
  feed.dispatchEvent(evt);
  assert.equal(evt.prevented, true, "still holds the browser off");
  assert.deepEqual(readTransform(img), { tx: 0, ty: 0, scale: 2 }, "but does not pan");
});

test("reset clears a zoom and is a no-op otherwise", () => {
  const { img, autoscroll, zoom } = setupHarness();

  zoom.reset();
  assert.equal(autoscroll.resets.length, 0);

  zoom.toggle(img, CONT_W / 2, CONT_H / 2);
  zoom.reset();
  assert.equal(zoom.isZoomed(), false);
  assert.equal(img.style.transform, "");
  assert.deepEqual(autoscroll.resets, ["id0"]);
});

// ---------------------------------------------------------------------------
// The zoom in/out step animates over ZOOM_TRANSITION_MS. Panning must not:
// the picture follows the cursor, and a transform transition left switched on
// would drag behind it on every move.
// ---------------------------------------------------------------------------

test("the zoom in step animates, then hands the element back for crisp panning", () => {
  const { feed, img, clock, zoom } = setupHarness();

  zoom.toggle(img, CONT_W / 2, CONT_H / 2);
  assert.equal(img.style.transition, "transform 200ms ease-out");
  assert.equal(clock.pending(), 1);

  clock.advance(200);
  assert.equal(img.style.transition, "", "the transition comes back off once it has landed");
  assert.deepEqual(readTransform(img), { tx: 0, ty: 0, scale: 2 }, "the zoom itself stays");

  // A pan after the window must not re-arm it.
  feed.dispatchEvent({ type: "pointermove", target: img, clientX: 0, clientY: 0, pointerType: "mouse" });
  assert.equal(img.style.transition, "");
  assert.equal(clock.pending(), 0);
});

test("the zoom out step animates too, but gives navigation back immediately", () => {
  const { wrap, img, clock, zoom } = setupHarness();

  zoom.toggle(img, CONT_W / 2, CONT_H / 2);
  clock.advance(200);
  zoom.toggle(img, CONT_W / 2, CONT_H / 2);

  // Before the animation has run: already un-zoomed as far as input is concerned.
  assert.equal(zoom.isZoomed(), false);
  assert.equal(wrap.style.touchAction, "", "scrolling must not wait for the animation");
  assert.equal(img.classList.contains("zoomed"), false);
  assert.equal(img.style.transition, "transform 200ms ease-out");
  assert.equal(img.style.transform, "", "shrinking back to the fitted size");

  clock.advance(200);
  assert.equal(img.style.transition, "");
});

test("toggling straight back out keeps the second animation", () => {
  const { img, clock, zoom } = setupHarness();

  zoom.toggle(img, CONT_W / 2, CONT_H / 2);
  zoom.toggle(img, CONT_W / 2, CONT_H / 2); // before the first timer fires
  assert.equal(clock.pending(), 1, "the stale timer must be cancelled, not left to strip the new one");

  assert.equal(img.style.transition, "transform 200ms ease-out");
  clock.advance(200);
  assert.equal(img.style.transition, "");
});

test("ZOOM_TRANSITION_MS=0 snaps, with no timer to clean up", () => {
  const { img, clock, zoom } = setupHarness({ zoomTransitionMs: 0 });

  zoom.toggle(img, CONT_W / 2, CONT_H / 2);
  assert.equal(img.style.transition, "");
  assert.equal(clock.pending(), 0);
  assert.deepEqual(readTransform(img), { tx: 0, ty: 0, scale: 2 });

  zoom.toggle(img, CONT_W / 2, CONT_H / 2);
  assert.equal(img.style.transition, "");
  assert.equal(clock.pending(), 0);
});

test("prefers-reduced-motion wins over the setting", () => {
  const { ctx, img, clock, zoom } = setupHarness();
  ctx.window.matchMedia = (q) => ({ matches: q === "(prefers-reduced-motion: reduce)" });

  zoom.toggle(img, CONT_W / 2, CONT_H / 2);
  assert.equal(img.style.transition, "");
  assert.equal(clock.pending(), 0);
});
