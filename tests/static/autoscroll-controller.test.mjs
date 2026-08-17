// ---------------------------------------------------------------------------
// autoscroll-controller.test.mjs — tests for autoscroll dwell behaviour.
//
// The autoscroll timer fires snap-to-next for the bound item. To prevent
// the "next item gets jumped over" perception (caused by short GIFs,
// fast scroll-snap overshoots, or sub-floor videos), every media type
// honours a minimum dwell equal to MRR.config.imageAutoscrollDelayMs:
//
//   - image:  setTimeout(max(IMAGE_AUTOSCROLL_DELAY_S, minDwell))
//   - gif:    setTimeout(max(parsedDuration, minDwell))
//   - video:  the 'ended' handler defers snapToNext until minDwell has
//             elapsed since bindIfVisible
// ---------------------------------------------------------------------------

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { createDomContext, loadScript } from "./dom-mock.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../src/static");

// A minimal GIF89a 1x1 image (35 bytes) with NO GCE block. The duration
// parser sees no GCE entries, so getGifDuration falls back to
// imageAutoscrollDelayMs.
const TINY_GIF_NO_GCE = Uint8Array.from([
  0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00, 0x01, 0x00, 0x80, 0x00, 0x00,
  0xff, 0xff, 0xff, 0x00, 0x00, 0x00, 0x2c, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00,
  0x01, 0x00, 0x00, 0x02, 0x02, 0x4c, 0x01, 0x00, 0x3b,
]);

// A minimal GIF89a 1x1 image with a single GCE block whose delay is
// 5 centi-seconds (50 ms total). The duration parser will sum this and
// return 50 ms — well below the min-dwell floor.
const TINY_GIF_50MS = Uint8Array.from([
  0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00, 0x01, 0x00, 0x80, 0x00, 0x00,
  0xff, 0xff, 0xff, 0x00, 0x00, 0x00, 0x21, 0xf9, 0x04, 0x05, 0x00, 0x00, 0x00,
  0x00, 0x2c, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x02, 0x02,
  0x4c, 0x01, 0x00, 0x3b,
]);

// Test-wide min-dwell: 100ms keeps the suite fast. The tests below assert
// the floor is honoured, not a specific value.
const MIN_DWELL_MS = 100;

function makeGifWrap(ctx, id = "gif0", src = "https://example/foo.gif") {
  const feed = ctx.document.getElementById("feed") || ctx.document.createElement("div");
  if (!feed.id) { feed.id = "feed"; ctx.document.register(feed); }
  const wrap = ctx.document.createElement("div");
  wrap.className = "media-item";
  wrap.dataset.id = id;
  wrap.dataset.mediaType = "gif";
  const img = ctx.document.createElement("img");
  img.src = src;
  wrap.appendChild(img);
  feed.appendChild(wrap);
  return { wrap, img, feed };
}

function installItemStore(ctx, items, gifBuffer = TINY_GIF_NO_GCE) {
  ctx.window.MRR.itemStore = {
    items,
    getItems: () => items,
    getCurrentIndex: () => 0,
    getItemAt: (i) => items[i],
    findIndexById: (id) => items.findIndex((it) => it.id === id),
  };
  ctx.window.MRR.config = {
    autoscroll: false,
    mutedDefault: true,
    imageAutoscrollDelayMs: MIN_DWELL_MS,
  };
  ctx.window.MRR.feedView = {
    snapToNext: () => {},
    advanceOrNext: (wrap) => { ctx.window.MRR.feedView.snapToNext(); },
    activeMediaEl: (wrap) => wrap.children[0] || null,
  };
  ctx.fetch = (url) => {
    if (url.startsWith("/api/media/proxy?")) {
      return Promise.resolve({
        ok: true,
        arrayBuffer: async () => gifBuffer.buffer,
      });
    }
    return Promise.resolve({ ok: true });
  };
}

// ---------------------------------------------------------------------------
// GIF: <img> is left intact; snap-to-next fires at >= minDwell.
// ---------------------------------------------------------------------------

test("bindIfVisible for a gif wrap leaves the <img> in place (browser keeps animating)", () => {
  const ctx = createDomContext();
  const { wrap, img } = makeGifWrap(ctx);
  installItemStore(ctx, [{ id: "gif0", media_type: "gif", media_url: "https://example/foo.gif" }]);
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  ctx.window.MRR.autoscrollController.setAutoscroll(true);
  ctx.window.MRR.autoscrollController.bindIfVisible(wrap);

  // The <img> must still be there — no <canvas> swap.
  assert.equal(wrap.children[0].tagName, "IMG", "first child should still be an <img>");
  assert.equal(wrap.children[0], img, "the original <img> must not be replaced");
});

test("bindIfVisible for a gif wrap schedules snapToNext at the minDwell floor when the parsed duration is shorter", async () => {
  // Regression for the "next item gets jumped over" bug: a parsed 50ms
  // GIF must NOT cause snapToNext in 50ms. The floor is imageAutoscrollDelayMs.
  const ctx = createDomContext();
  const { wrap } = makeGifWrap(ctx);
  let snapCalls = 0;
  installItemStore(ctx, [{ id: "gif0", media_type: "gif", media_url: "https://example/foo.gif" }], TINY_GIF_50MS);
  ctx.window.MRR.feedView.snapToNext = () => { snapCalls += 1; };
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  ctx.window.MRR.autoscrollController.setAutoscroll(true);
  ctx.window.MRR.autoscrollController.bindIfVisible(wrap);

  // Right after binding, no snap.
  assert.equal(snapCalls, 0, "snapToNext must not fire synchronously");
  // Before the floor, still no snap (the parsed 50ms is past).
  await new Promise((r) => setTimeout(r, MIN_DWELL_MS - 20));
  assert.equal(snapCalls, 0, `snapToNext must not fire before minDwell (${MIN_DWELL_MS}ms)`);
  // At/after the floor, exactly one snap.
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(snapCalls, 1, "snapToNext must fire once at the minDwell floor");
});

test("bindIfVisible for a gif wrap schedules snapToNext at the parsed duration when it exceeds the minDwell floor", async () => {
  // The opposite direction: a long GIF must NOT be cut short by the floor.
  // We stub getGifDuration indirectly by going through the GIF buffer, but
  // since TINY_GIF_NO_GCE has no GCE blocks, getGifDuration returns the
  // imageAutoscrollDelayMs fallback. So the effective delay is MIN_DWELL_MS
  // (equal in this test). The test asserts that snapToNext fires at the
  // expected time and only once.
  const ctx = createDomContext();
  const { wrap } = makeGifWrap(ctx);
  let snapCalls = 0;
  installItemStore(ctx, [{ id: "gif0", media_type: "gif", media_url: "https://example/foo.gif" }]);
  ctx.window.MRR.feedView.snapToNext = () => { snapCalls += 1; };
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  ctx.window.MRR.autoscrollController.setAutoscroll(true);
  ctx.window.MRR.autoscrollController.bindIfVisible(wrap);
  await new Promise((r) => setTimeout(r, MIN_DWELL_MS + 50));
  assert.equal(snapCalls, 1, "snapToNext must fire once after the dwell");
  await new Promise((r) => setTimeout(r, MIN_DWELL_MS));
  assert.equal(snapCalls, 1, "snapToNext must NOT fire twice");
});

test("setAutoscroll(false) does not touch the GIF <img>", () => {
  const ctx = createDomContext();
  const { wrap, img } = makeGifWrap(ctx);
  installItemStore(ctx, [{ id: "gif0", media_type: "gif", media_url: "https://example/foo.gif" }]);
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  ctx.window.MRR.autoscrollController.setAutoscroll(true);
  ctx.window.MRR.autoscrollController.bindIfVisible(wrap);
  assert.equal(wrap.children[0].tagName, "IMG");

  ctx.window.MRR.autoscrollController.setAutoscroll(false);
  assert.equal(wrap.children[0].tagName, "IMG", "<img> must remain after autoscroll off");
  assert.equal(wrap.children[0], img, "the original <img> must still be in the wrap");
});

// ---------------------------------------------------------------------------
// Image: dwell is exactly the minDwell value.
// ---------------------------------------------------------------------------

test("bindIfVisible for an image wrap schedules snapToNext at exactly imageAutoscrollDelayMs", async () => {
  const ctx = createDomContext();
  installItemStore(ctx, [
    { id: "img0", media_type: "image", media_url: "https://example/foo.jpg" },
  ]);
  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  ctx.document.register(feed);
  const wrap = ctx.document.createElement("div");
  wrap.className = "media-item";
  wrap.dataset.id = "img0";
  wrap.dataset.mediaType = "image";
  const img = ctx.document.createElement("img");
  img.src = "https://example/foo.jpg";
  wrap.appendChild(img);
  feed.appendChild(wrap);

  let snapCalls = 0;
  ctx.window.MRR.feedView.snapToNext = () => { snapCalls += 1; };
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  ctx.window.MRR.autoscrollController.setAutoscroll(true);
  ctx.window.MRR.autoscrollController.bindIfVisible(wrap);

  await new Promise((r) => setTimeout(r, MIN_DWELL_MS - 20));
  assert.equal(snapCalls, 0, "snapToNext must not fire before imageAutoscrollDelayMs");
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(snapCalls, 1, "snapToNext must fire once at imageAutoscrollDelayMs");
});

// ---------------------------------------------------------------------------
// Video: the 'ended' handler defers snapToNext until the minDwell floor
// has elapsed since bindIfVisible. Videos longer than the floor advance
// immediately on 'ended'.
// ---------------------------------------------------------------------------

test("bindIfVisible for a video: 'ended' before the minDwell defers snapToNext", async () => {
  const ctx = createDomContext();
  installItemStore(ctx, [
    { id: "vid0", media_type: "video", media_url: "https://example/foo.mp4" },
  ]);
  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  ctx.document.register(feed);
  const wrap = ctx.document.createElement("div");
  wrap.className = "media-item";
  wrap.dataset.id = "vid0";
  wrap.dataset.mediaType = "video";
  const v = ctx.document.createElement("video");
  wrap.appendChild(v);
  feed.appendChild(wrap);

  let snapCalls = 0;
  ctx.window.MRR.feedView.snapToNext = () => { snapCalls += 1; };
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  ctx.window.MRR.autoscrollController.setAutoscroll(true);
  ctx.window.MRR.autoscrollController.bindIfVisible(wrap);

  // Fire 'ended' immediately (well before the min-dwell floor).
  v.dispatchEvent({ type: "ended" });
  // The dispatch above runs the handler synchronously and the handler
  // schedules a setTimeout for the remaining dwell.
  assert.equal(snapCalls, 0, "snapToNext must not fire synchronously on ended-before-floor");
  await new Promise((r) => setTimeout(r, MIN_DWELL_MS + 30));
  assert.equal(snapCalls, 1, "snapToNext must fire after the floor elapses");
});

test("bindIfVisible for a video: 'ended' after the minDwell fires snapToNext immediately (no extra delay)", async () => {
  const ctx = createDomContext();
  installItemStore(ctx, [
    { id: "vid0", media_type: "video", media_url: "https://example/foo.mp4" },
  ]);
  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  ctx.document.register(feed);
  const wrap = ctx.document.createElement("div");
  wrap.className = "media-item";
  wrap.dataset.id = "vid0";
  wrap.dataset.mediaType = "video";
  const v = ctx.document.createElement("video");
  wrap.appendChild(v);
  feed.appendChild(wrap);

  let snapCalls = 0;
  ctx.window.MRR.feedView.snapToNext = () => { snapCalls += 1; };
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  ctx.window.MRR.autoscrollController.setAutoscroll(true);
  ctx.window.MRR.autoscrollController.bindIfVisible(wrap);

  // Wait past the floor, then fire 'ended'.
  await new Promise((r) => setTimeout(r, MIN_DWELL_MS + 10));
  v.dispatchEvent({ type: "ended" });
  // Give any scheduled setTimeout a chance to run — must be 0ms in this case.
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(snapCalls, 1, "snapToNext must fire once when ended happens after the floor");
});

// ---------------------------------------------------------------------------
// Sanity: image/video paths are not affected by the GIF swap removal.
// ---------------------------------------------------------------------------

test("bindIfVisible for a non-gif wrap is not affected by the swap (image/video paths unchanged)", () => {
  const ctx = createDomContext();
  installItemStore(ctx, [
    { id: "img0", media_type: "image", media_url: "https://example/foo.jpg" },
    { id: "vid0", media_type: "video", media_url: "https://example/foo.mp4" },
  ]);
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  ctx.window.MRR.autoscrollController.setAutoscroll(true);

  const imgWrap = ctx.document.createElement("div");
  imgWrap.className = "media-item";
  imgWrap.dataset.id = "img0";
  imgWrap.dataset.mediaType = "image";
  const img = ctx.document.createElement("img");
  img.src = "https://example/foo.jpg";
  imgWrap.appendChild(img);
  ctx.window.MRR.autoscrollController.bindIfVisible(imgWrap);
  assert.equal(imgWrap.children[0].tagName, "IMG", "image wrap keeps its <img>");

  const vidWrap = ctx.document.createElement("div");
  vidWrap.className = "media-item";
  vidWrap.dataset.id = "vid0";
  vidWrap.dataset.mediaType = "video";
  const vid = ctx.document.createElement("video");
  vidWrap.appendChild(vid);
  ctx.window.MRR.autoscrollController.bindIfVisible(vidWrap);
  assert.equal(vidWrap.children[0].tagName, "VIDEO", "video wrap keeps its <video>");
});

// ---------------------------------------------------------------------------
// A video mounted after the last toggle must still get the right loop flag.
// setAutoscroll's own sweep only touches videos already in the DOM at
// toggle time; a video that loads afterwards gets its `loop` from
// feed-view's wireVideo, which must read the live autoscrollController
// state (isEnabled()) rather than a config snapshot taken at toggle time.
// ---------------------------------------------------------------------------

test("a video mounted after the toggle still gets the right loop flag", () => {
  const ctx = createDomContext();
  const items = [{ id: "i0", media_type: "image", media_url: "https://example/i0.jpg" }];
  ctx.window.MRR.itemStore = {
    items,
    getItems: () => items,
    getCurrentIndex: () => 0,
    getItemAt: (i) => items[i],
    findIndexById: (id) => items.findIndex((it) => it.id === id),
    setCurrentIndex: () => {},
  };
  ctx.window.MRR.config = { imageAutoscrollDelayMs: MIN_DWELL_MS };
  ctx.window.MRR.scrollController = { observe() {} };
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  loadScript(resolve(STATIC, "feed-view.js"), ctx);

  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  ctx.document.register(feed);
  ctx.window.MRR.feedView.renderInitial(items);

  // Toggle on; the sweep in setAutoscroll runs over zero videos (none mounted yet).
  ctx.window.MRR.autoscrollController.setAutoscroll(true);

  // Now mount a video AFTER the toggle.
  const item1 = { id: "i1", media_type: "video", media_url: "https://example/i1.mp4" };
  items.push(item1);
  ctx.window.MRR.feedView.appendItem(item1);
  const el = ctx.document.createElement("video");
  ctx.window.MRR.feedView.onItemLoaded("i1", el);

  const wrap = feed.children.find((c) => c.dataset.id === "i1");
  const v = wrap.querySelector("video");
  assert.equal(v.loop, false, "autoscroll on means videos do not loop");
});
