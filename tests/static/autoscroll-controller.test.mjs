// ---------------------------------------------------------------------------
// autoscroll-controller.test.mjs — tests for the GIF → canvas swap.
//
// Bug: when autoscroll is ON and the current item is a GIF, the <img>
// keeps looping visually. The autoscroll timer fires after the parsed GIF
// duration, but the user sees the GIF animate many times during that
// window. We want the GIF to effectively "play once": replace the looping
// <img> with a <canvas> showing the first frame for the duration of the
// autoscroll window, then snap to next. When autoscroll is turned OFF,
// the canvas is swapped back to a fresh <img> so the GIF resumes looping
// naturally.
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
// The 2D context will draw the image successfully because the mock canvas
// does not actually decode pixels.
const TINY_GIF = Uint8Array.from([
  0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00, 0x01, 0x00, 0x80, 0x00, 0x00,
  0xff, 0xff, 0xff, 0x00, 0x00, 0x00, 0x21, 0xf9, 0x04, 0x01, 0x00, 0x00, 0x00,
  0x00, 0x2c, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x02, 0x02,
  0x4c, 0x01, 0x00, 0x3b,
]);

function makeGifWrap(ctx, id = "gif0", src = "https://example/foo.gif") {
  // Build the wrap inside the document's feed container so the
  // restoreAllSwappedGifs() query (#feed .media-item[data-media-type="gif"])
  // can find it.
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

test("bindIfVisible for a gif wrap replaces the <img> with a <canvas> and draws the first frame", async () => {
  const ctx = createDomContext();
  const { wrap, img } = makeGifWrap(ctx);
  installItemStore(ctx, [{ id: "gif0", media_type: "gif", media_url: "https://example/foo.gif" }]);
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  ctx.window.MRR.autoscrollController.setAutoscroll(true);
  ctx.window.MRR.autoscrollController.bindIfVisible(wrap);

  // The swap is synchronous inside bindIfVisible — the duration fetch is
  // async and only schedules the snap-to-next timer.
  assert.equal(wrap.children[0].tagName, "CANVAS", "first child should be a <canvas>");
  assert.equal(img.parentNode, null, "the original <img> should be detached");
  const canvas = wrap.children[0];
  assert.ok(canvas._ctxCalls?.some((c) => c.method === "drawImage"), "drawImage should be called on the canvas 2D context");
});

test("setAutoscroll(false) restores the <img> so the GIF loops again", async () => {
  const ctx = createDomContext();
  const { wrap, img } = makeGifWrap(ctx);
  installItemStore(ctx, [{ id: "gif0", media_type: "gif", media_url: "https://example/foo.gif" }]);
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  ctx.window.MRR.autoscrollController.setAutoscroll(true);
  ctx.window.MRR.autoscrollController.bindIfVisible(wrap);
  // sanity: the swap happened
  assert.equal(wrap.children[0].tagName, "CANVAS");

  ctx.window.MRR.autoscrollController.setAutoscroll(false);
  // After turning autoscroll off, the canvas should be replaced with a
  // fresh <img> carrying the original src.
  assert.equal(wrap.children[0].tagName, "IMG", "first child should be an <img> after autoscroll off");
  assert.equal(wrap.children[0].src, "https://example/foo.gif", "restored img should have the original src");
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
