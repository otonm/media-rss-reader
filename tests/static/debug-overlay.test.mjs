// ---------------------------------------------------------------------------
// debug-overlay.test.mjs — the UI_DEBUG overlay in controls.js.
//
// The overlay exists to make the media-loading path legible: it is what tells
// you whether a black screen is an item that was never cached, one that was
// cached but will not decode, or one stuck behind a stalled download. So the
// cache HIT/MISS line and the queue counters are the point, not decoration.
//
// Tests drive initDebugOverlay() directly rather than controls.init(), which
// would need the whole control bar registered just to build one div.
// ---------------------------------------------------------------------------

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { createDomContext, loadScript } from "./dom-mock.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../src/static");

const ITEM = {
  id: "id0",
  feed_id: "f1",
  title: "Sunrise over the Dolomites",
  media_type: "image",
  media_url: "https://cdn.example/photos/sunrise.JPG?width=900",
  media: [{ url: "https://cdn.example/photos/sunrise.JPG", type: "image" }],
  pub_date: "2026-08-02 14:31:00",
  cached: true,
};

function makeContext({ uiDebug, item = ITEM }) {
  const ctx = createDomContext();
  ctx.window.MRR.itemStore = {
    getItems: () => (item ? [item] : []),
    getCurrentIndex: () => 0,
    getItemAt: (i) => (item ? [item][i] : undefined),
  };
  ctx.window.MRR.cacheQueue = { getStats: () => ({ queued: 4, loading: 2, done: 7 }) };
  ctx.window.MRR.config = { uiDebug };
  loadScript(resolve(STATIC, "controls.js"), ctx);
  return ctx;
}

function overlayOf(ctx) {
  return ctx.document.body.children.find((c) => c.id === "debug-overlay");
}

function rows(overlay) {
  const out = {};
  overlay.children.forEach((r) => {
    out[r.children[0].textContent] = r.children[1].textContent;
  });
  return out;
}

test("no overlay is created when UI_DEBUG is off", () => {
  const ctx = makeContext({ uiDebug: false });
  ctx.window.MRR.controls.initDebugOverlay();
  ctx.window.MRR.controls.renderDebug();
  assert.equal(ctx.document.body.children.length, 0);
});

test("the overlay names the current item's feed, title, type and pubdate", () => {
  const ctx = makeContext({ uiDebug: true });
  ctx.window.MRR.controls.initDebugOverlay();

  const overlay = overlayOf(ctx);
  assert.ok(overlay, "UI_DEBUG=1 must create the overlay");
  const r = rows(overlay);
  assert.equal(r.feed, "f1", "feed_id must be displayed as-is");
  assert.equal(r.title, "Sunrise over the Dolomites");
  assert.equal(r.type, "image · jpg", "extension parsed off the URL, query string stripped");
  assert.equal(r.pubdate, "2026-08-02 14:31:00");
});

test("the overlay reports cache hit/miss, load time and queue depth", () => {
  const ctx = makeContext({ uiDebug: true });
  ctx.window.MRR.controls.initDebugOverlay();
  ctx.window.MRR.controls.recordLoadMs("id0", 12);
  ctx.window.MRR.controls.renderDebug();

  const r = rows(overlayOf(ctx));
  assert.equal(r.cache, "HIT · 12ms");
  assert.equal(r.queue, "2 loading · 4 queued");
});

test("an uncached item reads MISS", () => {
  const ctx = makeContext({ uiDebug: true, item: { ...ITEM, cached: false } });
  ctx.window.MRR.controls.initDebugOverlay();

  assert.equal(rows(overlayOf(ctx)).cache, "MISS");
});

test("the overlay survives an empty feed", () => {
  const ctx = makeContext({ uiDebug: true, item: null });
  ctx.window.MRR.controls.initDebugOverlay();

  assert.equal(rows(overlayOf(ctx)).item, "none");
});
