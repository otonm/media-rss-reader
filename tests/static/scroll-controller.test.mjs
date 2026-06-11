// ---------------------------------------------------------------------------
// scroll-controller.test.mjs — tests for the seen-marking flow.
//
// When the IntersectionObserver fires for an item that has scrolled fully
// past the top of the viewport, scroll-controller.onSeen POSTs
// /api/items/{id}/seen and, on success, calls:
//   - MRR.itemStore.markSeen(id, seen_at)  — updates the in-memory item
//   - MRR.feedView.markSeen(id)            — adds the .seen class live
// On a failed POST, neither callback fires.
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
function setupHarness() {
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
    getItems: () => [],
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
  // seen observer (threshold 0).
  return {
    ctx,
    markSeenCalls,
    fetchCalls,
    getSeenCb: () => callbacks[callbacks.length - 1],
  };
}

test("a successful POST triggers both itemStore.markSeen and feedView.markSeen", async () => {
  const { ctx, markSeenCalls, fetchCalls, getSeenCb } = setupHarness();
  const wrap = ctx.document.createElement("div");
  wrap.dataset.id = "id42";
  getSeenCb()([{ target: wrap, isIntersecting: false, boundingClientRect: { bottom: -10 } }]);
  // The seen handler is fire-and-forget; flush microtasks.
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
  ctx.window.MRR.itemStore = { markSeen: (id, ts) => { markSeenCalls.push({ who: "store", id, ts }); } };
  ctx.window.MRR.feedView = { markSeen: (id) => { markSeenCalls.push({ who: "feed", id }); } };
  loadScript(resolve(STATIC, "scroll-controller.js"), ctx);
  ctx.window.MRR.scrollController.init();
  const wrap = ctx.document.createElement("div");
  wrap.dataset.id = "id99";
  callbacks[callbacks.length - 1]([{ target: wrap, isIntersecting: false, boundingClientRect: { bottom: -10 } }]);
  await new Promise((r) => setImmediate(r));
  assert.equal(markSeenCalls.length, 0, "no markSeen callbacks on failed POST");
});

test("an item still visible (bottom >= 0) does NOT trigger the POST", async () => {
  const ctx = createDomContext();
  const callbacks = [];
  ctx.IntersectionObserver = class { constructor(cb) { callbacks.push(cb); } observe() {} unobserve() {} disconnect() {} };
  const fetchCalls = [];
  ctx.fetch = (url, opts) => { fetchCalls.push({ url, opts }); return Promise.resolve({ ok: true, json: async () => ({ seen_at: "x" }) }); };
  ctx.window.MRR.itemStore = { markSeen: () => {} };
  ctx.window.MRR.feedView = { markSeen: () => {} };
  loadScript(resolve(STATIC, "scroll-controller.js"), ctx);
  ctx.window.MRR.scrollController.init();
  const wrap = ctx.document.createElement("div");
  wrap.dataset.id = "id5";
  callbacks[callbacks.length - 1]([{ target: wrap, isIntersecting: true, boundingClientRect: { bottom: 50 } }]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 0, "POST must not fire for still-visible items");
});
