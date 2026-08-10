// ---------------------------------------------------------------------------
// zoom-controller.test.mjs — zoom-to-100%: the scale decision, the pan clamp,
// and the double-tap detector.
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

import { createDomContext, loadScript } from "./dom-mock.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../src/static");

// Container 1000x800 at the viewport origin; picture rendered 800x600.
const CONT_W = 1000;
const CONT_H = 800;

function rect(left, top, width, height) {
  return { left, top, width, height, right: left + width, bottom: top + height };
}

// naturalWidth 1600 against a rendered 800 => scale 2 => maxPan {x:300, y:200}.
function setupHarness({ naturalWidth = 1600 } = {}) {
  const ctx = createDomContext();
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
  return { ctx, feed, wrap, img, autoscroll, zoom: ctx.window.MRR.zoomController };
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
  feed.dispatchEvent({ type: "pointerdown", target: img, clientX: 500, clientY: 400, pointerType: "touch" });
  feed.dispatchEvent({ type: "pointermove", target: img, clientX: 400, clientY: 400, pointerType: "touch" });
  assert.equal(readTransform(img).tx, 200, "300 - 100 of drag");
  feed.dispatchEvent({ type: "pointermove", target: img, clientX: 5000, clientY: 400, pointerType: "touch" });
  assert.equal(readTransform(img).tx, 300, "clamped to maxPan");
});

test("a finger move without a preceding touchdown does not pan", () => {
  const { feed, img, zoom } = setupHarness();
  zoom.toggle(img, CONT_W / 2, CONT_H / 2);

  feed.dispatchEvent({ type: "pointermove", target: img, clientX: 0, clientY: 0, pointerType: "touch" });
  assert.deepEqual(readTransform(img), { tx: 0, ty: 0, scale: 2 });
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
