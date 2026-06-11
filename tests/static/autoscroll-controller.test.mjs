// ---------------------------------------------------------------------------
// autoscroll-controller.test.mjs — tests for autoscroll behaviour across
// image, gif, and video media types.
//
// GIF behaviour: in autoscroll mode, the GIF <img> is left intact so the
// browser keeps animating it. The autoscroll timer fires after the parsed
// GIF duration and snaps to the next item.
// ---------------------------------------------------------------------------

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { createDomContext, loadScript } from "./dom-mock.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../src/static");

// Build a minimal GIF89a 1x1 image (35 bytes) so the duration parser
// finds no GCE blocks and falls back to the imageAutoscrollDelayMs default.
const TINY_GIF = Uint8Array.from([
  0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00, 0x01, 0x00, 0x80, 0x00, 0x00,
  0xff, 0xff, 0xff, 0x00, 0x00, 0x00, 0x21, 0xf9, 0x04, 0x01, 0x00, 0x00, 0x00,
  0x00, 0x2c, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x02, 0x02,
  0x4c, 0x01, 0x00, 0x3b,
]);

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

function installItemStore(ctx, items) {
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
    imageAutoscrollDelayMs: 2000,
  };
  ctx.window.MRR.feedView = { snapToNext: () => {} };
  // Stub fetch: only /api/media/proxy?... (used by getGifDuration) returns
  // the tiny GIF buffer; the prefetch hint is a no-op.
  ctx.fetch = (url) => {
    if (url.startsWith("/api/media/proxy?")) {
      return Promise.resolve({
        ok: true,
        arrayBuffer: async () => TINY_GIF.buffer,
      });
    }
    return Promise.resolve({ ok: true });
  };
}

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

test("bindIfVisible for a gif wrap does not call getGifDuration synchronously (it schedules snapToNext after the duration)", async () => {
  const ctx = createDomContext();
  const { wrap, img } = makeGifWrap(ctx);
  let snapCalls = 0;
  installItemStore(ctx, [{ id: "gif0", media_type: "gif", media_url: "https://example/foo.gif" }]);
  ctx.window.MRR.feedView.snapToNext = () => { snapCalls += 1; };
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  ctx.window.MRR.autoscrollController.setAutoscroll(true);
  ctx.window.MRR.autoscrollController.bindIfVisible(wrap);

  // snapToNext is scheduled by the async getGifDuration promise, which the
  // mock fetch resolves with the tiny GIF buffer (no GCE blocks, so the
  // duration falls back to imageAutoscrollDelayMs = 2000).
  assert.equal(snapCalls, 0, "snapToNext must not fire synchronously");
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(snapCalls, 0, "snapToNext must not fire before the 2000ms delay");
  await new Promise((r) => setTimeout(r, 2050));
  assert.equal(snapCalls, 1, "snapToNext must fire once after the GIF duration");
});

test("setAutoscroll(false) does not touch the GIF <img>", () => {
  const ctx = createDomContext();
  const { wrap, img } = makeGifWrap(ctx);
  installItemStore(ctx, [{ id: "gif0", media_type: "gif", media_url: "https://example/foo.gif" }]);
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  ctx.window.MRR.autoscrollController.setAutoscroll(true);
  ctx.window.MRR.autoscrollController.bindIfVisible(wrap);
  // sanity: still the same <img>
  assert.equal(wrap.children[0].tagName, "IMG");

  ctx.window.MRR.autoscrollController.setAutoscroll(false);
  // After turning autoscroll off, the <img> must still be there unchanged.
  assert.equal(wrap.children[0].tagName, "IMG", "<img> must remain after autoscroll off");
  assert.equal(wrap.children[0], img, "the original <img> must still be in the wrap");
});

test("bindIfVisible for a non-gif wrap is not affected by the swap (image/video paths unchanged)", () => {
  const ctx = createDomContext();
  installItemStore(ctx, [
    { id: "img0", media_type: "image", media_url: "https://example/foo.jpg" },
    { id: "vid0", media_type: "video", media_url: "https://example/foo.mp4" },
  ]);
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  ctx.window.MRR.autoscrollController.setAutoscroll(true);

  // image wrap
  const imgWrap = ctx.document.createElement("div");
  imgWrap.className = "media-item";
  imgWrap.dataset.id = "img0";
  imgWrap.dataset.mediaType = "image";
  const img = ctx.document.createElement("img");
  img.src = "https://example/foo.jpg";
  imgWrap.appendChild(img);
  ctx.window.MRR.autoscrollController.bindIfVisible(imgWrap);
  assert.equal(imgWrap.children[0].tagName, "IMG", "image wrap keeps its <img>");

  // video wrap
  const vidWrap = ctx.document.createElement("div");
  vidWrap.className = "media-item";
  vidWrap.dataset.id = "vid0";
  vidWrap.dataset.mediaType = "video";
  const vid = ctx.document.createElement("video");
  vidWrap.appendChild(vid);
  ctx.window.MRR.autoscrollController.bindIfVisible(vidWrap);
  assert.equal(vidWrap.children[0].tagName, "VIDEO", "video wrap keeps its <video>");
});
