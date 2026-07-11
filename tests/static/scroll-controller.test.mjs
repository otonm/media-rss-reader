// ---------------------------------------------------------------------------
// scroll-controller.test.mjs — tests for the seen-marking flow.
//
// scrollController uses TWO IntersectionObservers:
//   observer      (threshold 0.6) — tracks the most-visible item
//   seenObserver  (threshold 0.8) — fires POST /api/items/{id}/seen
//
// The seen POST fires when:
//   1. Primary: item enters viewport and is 80%+ visible (isIntersecting: true)
//   2. Fallback: media loads after user already scrolled past (element above viewport)
//
// Dedup: if the in-memory item already has seen_at set, no POST is sent.
// On a successful POST, itemStore.markSeen + feedView.markSeen are called.
// ---------------------------------------------------------------------------

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { createDomContext, loadScript } from "./dom-mock.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../src/static");

// Build a fresh context with a recording IntersectionObserver so we can
// fire synthetic entries at the seen callback. Returns the loaded
// scrollController plus a `getSeenCb()` accessor and a fetch spy.
function setupHarness({ items = [{ id: "id42", seen_at: null }] } = {}) {
  const ctx = createDomContext();
  const callbacks = [];
  ctx.IntersectionObserver = class {
    constructor(cb) { callbacks.push(cb); }
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  const fetchCalls = [];
  ctx.fetch = (url, opts) => {
    fetchCalls.push({ url, opts });
    return Promise.resolve({
      ok: true,
      json: async () => ({ seen_at: "2026-06-11T12:00:00" }),
    });
  };
  const markSeenCalls = [];
  ctx.window.MRR.itemStore = {
    getItems: () => items,
    getCurrentIndex: () => 0,
    getItemAt: () => null,
    markSeen: (id, ts) => { markSeenCalls.push({ who: "store", id, ts }); },
  };
  ctx.window.MRR.feedView = {
    markSeen: (id) => { markSeenCalls.push({ who: "feed", id }); },
  };
  loadScript(resolve(STATIC, "scroll-controller.js"), ctx);
  ctx.window.MRR.scrollController.init();
  // init() registers two observers; the second one (created last) is the
  // seen observer (threshold 0.8).
  return {
    ctx,
    markSeenCalls,
    fetchCalls,
    items,
    getSeenCb: () => callbacks[callbacks.length - 1],
  };
}

test("a successful POST triggers both itemStore.markSeen and feedView.markSeen", async () => {
  const { ctx, markSeenCalls, fetchCalls, getSeenCb } = setupHarness();
  const wrap = ctx.document.createElement("div");
  wrap.dataset.id = "id42";
  wrap.dataset.mediaType = "image";
  getSeenCb()([{
    target: wrap,
    isIntersecting: true,
    intersectionRatio: 0.9,
    boundingClientRect: { bottom: 100 },
  }]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0].url, "/api/items/id42/seen");
  assert.equal(fetchCalls[0].opts.method, "POST");
  assert.ok(
    markSeenCalls.some((c) => c.who === "store" && c.id === "id42" && c.ts === "2026-06-11T12:00:00"),
    "itemStore.markSeen must be called with id and timestamp",
  );
  assert.ok(
    markSeenCalls.some((c) => c.who === "feed" && c.id === "id42"),
    "feedView.markSeen must be called with id",
  );
});

test("a failed POST does NOT trigger markSeen callbacks", async () => {
  const ctx = createDomContext();
  const callbacks = [];
  ctx.IntersectionObserver = class { constructor(cb) { callbacks.push(cb); } observe() {} unobserve() {} disconnect() {} };
  ctx.fetch = () => Promise.resolve({ ok: false, json: async () => ({ detail: "fail" }) });
  const markSeenCalls = [];
  ctx.window.MRR.itemStore = {
    getItems: () => [{ id: "id99", seen_at: null }],
    markSeen: (id, ts) => { markSeenCalls.push({ who: "store", id, ts }); },
  };
  ctx.window.MRR.feedView = { markSeen: (id) => { markSeenCalls.push({ who: "feed", id }); } };
  loadScript(resolve(STATIC, "scroll-controller.js"), ctx);
  ctx.window.MRR.scrollController.init();
  const wrap = ctx.document.createElement("div");
  wrap.dataset.id = "id99";
  wrap.dataset.mediaType = "image";
  callbacks[callbacks.length - 1]([{
    target: wrap,
    isIntersecting: true,
    intersectionRatio: 0.9,
    boundingClientRect: { bottom: 100 },
  }]);
  await new Promise((r) => setImmediate(r));
  assert.equal(markSeenCalls.length, 0, "no markSeen callbacks on failed POST");
});

test("item entering viewport at 80%+ triggers the POST (primary trigger)", async () => {
  const { ctx, fetchCalls, getSeenCb } = setupHarness();
  const wrap = ctx.document.createElement("div");
  wrap.dataset.id = "id42";
  wrap.dataset.mediaType = "image";
  getSeenCb()([{
    target: wrap,
    isIntersecting: true,
    intersectionRatio: 0.8,
    boundingClientRect: { bottom: 100 },
  }]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 1, "POST must fire when item is 80%+ visible");
});

test("item entering viewport below 80% does NOT trigger the POST", async () => {
  const { ctx, fetchCalls, getSeenCb } = setupHarness();
  const wrap = ctx.document.createElement("div");
  wrap.dataset.id = "id42";
  wrap.dataset.mediaType = "image";
  getSeenCb()([{
    target: wrap,
    isIntersecting: true,
    intersectionRatio: 0.5,
    boundingClientRect: { bottom: 100 },
  }]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 0, "POST must not fire below 80% visibility");
});

test("item scrolled past viewport (late media load) triggers the POST (fallback)", async () => {
  const { ctx, fetchCalls, getSeenCb } = setupHarness();
  const wrap = ctx.document.createElement("div");
  wrap.dataset.id = "id42";
  wrap.dataset.mediaType = "image";
  getSeenCb()([{
    target: wrap,
    isIntersecting: false,
    intersectionRatio: 0,
    boundingClientRect: { bottom: -10 },
  }]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 1, "POST must fire for items already past viewport");
});

test("item still below viewport (bottom > 0, not intersecting) does NOT trigger POST", async () => {
  const { ctx, fetchCalls, getSeenCb } = setupHarness();
  const wrap = ctx.document.createElement("div");
  wrap.dataset.id = "id42";
  wrap.dataset.mediaType = "image";
  getSeenCb()([{
    target: wrap,
    isIntersecting: false,
    intersectionRatio: 0,
    boundingClientRect: { bottom: 50 },
  }]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 0, "POST must not fire for items below viewport that haven't been seen");
});

test("placeholder (no dataset.mediaType) is skipped", async () => {
  const { ctx, fetchCalls, getSeenCb } = setupHarness();
  const wrap = ctx.document.createElement("div");
  wrap.dataset.id = "id42";
  // no dataset.mediaType — placeholder
  getSeenCb()([{
    target: wrap,
    isIntersecting: true,
    intersectionRatio: 0.9,
    boundingClientRect: { bottom: 100 },
  }]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 0, "POST must not fire for placeholders");
});

test("item already marked seen does NOT trigger a second POST (dedup)", async () => {
  const { ctx, fetchCalls, getSeenCb } = setupHarness({
    items: [{ id: "id42", seen_at: "2026-06-10T00:00:00" }],
  });
  const wrap = ctx.document.createElement("div");
  wrap.dataset.id = "id42";
  wrap.dataset.mediaType = "image";
  getSeenCb()([{
    target: wrap,
    isIntersecting: true,
    intersectionRatio: 0.9,
    boundingClientRect: { bottom: 100 },
  }]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 0, "POST must not fire for already-seen items");
});
