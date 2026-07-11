// ---------------------------------------------------------------------------
// scroll-controller.test.mjs — tests for the seen-marking flow.
//
// scrollController uses a single IntersectionObserver (threshold 0.6).
// When the most-visible item changes, the previous item is marked as seen
// via POST /api/items/{id}/seen.
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

function makeEntry(ctx, id, ratio) {
  const wrap = ctx.document.createElement("div");
  wrap.dataset.id = id;
  return {
    target: wrap,
    isIntersecting: true,
    intersectionRatio: ratio,
  };
}

// Build a fresh context with a recording IntersectionObserver.
// Returns the main observer callback (`getCb`) and a fetch spy.
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
    findIndexById: (id) => items.findIndex((i) => i.id === id),
    setCurrentIndex: () => {},
    markSeen: (id, ts) => { markSeenCalls.push({ who: "store", id, ts }); },
  };
  ctx.window.MRR.feedView = {
    markSeen: (id) => { markSeenCalls.push({ who: "feed", id }); },
    setCurrentMedia: () => {},
  };
  ctx.window.MRR.cacheQueue = { rebuild: () => {} };
  ctx.window.MRR.autoscrollController = { reset: () => {} };
  ctx.window.MRR.config = { feedInitialCount: 10 };
  loadScript(resolve(STATIC, "scroll-controller.js"), ctx);
  ctx.window.MRR.scrollController.init();
  return {
    ctx,
    markSeenCalls,
    fetchCalls,
    items,
    getCb: () => callbacks[0],
  };
}

test("scroll past triggers seen POST for previous item", async () => {
  const { ctx, fetchCalls, markSeenCalls, getCb } = setupHarness({
    items: [
      { id: "id42", seen_at: null },
      { id: "id77", seen_at: null },
    ],
  });
  getCb()([makeEntry(ctx, "id42", 0.7)]);
  getCb()([makeEntry(ctx, "id77", 0.7)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 1, "one POST for the previous item");
  assert.equal(fetchCalls[0].url, "/api/items/id42/seen");
  assert.equal(fetchCalls[0].opts.method, "POST");
  assert.ok(
    markSeenCalls.some((c) => c.who === "store" && c.id === "id42" && c.ts === "2026-06-11T12:00:00"),
    "itemStore.markSeen must be called for previous item",
  );
  assert.ok(
    markSeenCalls.some((c) => c.who === "feed" && c.id === "id42"),
    "feedView.markSeen must be called for previous item",
  );
});

test("first observable item does not trigger a POST", async () => {
  const { fetchCalls, getCb, ctx } = setupHarness({
    items: [{ id: "id42", seen_at: null }],
  });
  getCb()([makeEntry(ctx, "id42", 0.7)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 0, "no POST when no previous item exists");
});

test("same item staying most-visible does not re-trigger POST", async () => {
  const { fetchCalls, getCb, ctx } = setupHarness({
    items: [{ id: "id42", seen_at: null }],
  });
  getCb()([makeEntry(ctx, "id42", 0.7)]);
  getCb()([makeEntry(ctx, "id42", 0.8)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 0, "no POST when same item remains most-visible");
});

test("already seen item does NOT trigger a second POST", async () => {
  const { fetchCalls, getCb, ctx } = setupHarness({
    items: [
      { id: "id42", seen_at: "2026-06-10T00:00:00" },
      { id: "id77", seen_at: null },
    ],
  });
  getCb()([makeEntry(ctx, "id42", 0.7)]);
  getCb()([makeEntry(ctx, "id77", 0.7)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 0, "no POST for already-seen item");
});

test("failed POST does NOT trigger markSeen callbacks", async () => {
  const ctx = createDomContext();
  const callbacks = [];
  ctx.IntersectionObserver = class { constructor(cb) { callbacks.push(cb); } observe() {} unobserve() {} disconnect() {} };
  ctx.fetch = () => Promise.resolve({ ok: false, json: async () => ({ detail: "fail" }) });
  const markSeenCalls = [];
  ctx.window.MRR.itemStore = {
    getItems: () => [
      { id: "id42", seen_at: null },
      { id: "id77", seen_at: null },
    ],
    getCurrentIndex: () => 0,
    getItemAt: () => null,
    findIndexById: (id) => [{ id: "id42", seen_at: null }, { id: "id77", seen_at: null }].findIndex((i) => i.id === id),
    setCurrentIndex: () => {},
    markSeen: (id, ts) => { markSeenCalls.push({ who: "store", id, ts }); },
  };
  ctx.window.MRR.feedView = { markSeen: (id) => { markSeenCalls.push({ who: "feed", id }); }, setCurrentMedia: () => {} };
  ctx.window.MRR.cacheQueue = { rebuild: () => {} };
  ctx.window.MRR.autoscrollController = { reset: () => {} };
  ctx.window.MRR.config = { feedInitialCount: 10 };
  loadScript(resolve(STATIC, "scroll-controller.js"), ctx);
  ctx.window.MRR.scrollController.init();
  callbacks[0]([makeEntry(ctx, "id42", 0.7)]);
  callbacks[0]([makeEntry(ctx, "id77", 0.7)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(markSeenCalls.length, 0, "no markSeen callbacks on failed POST");
});

test("scrolling back to a previous item marks the one left", async () => {
  const { fetchCalls, getCb, ctx } = setupHarness({
    items: [
      { id: "id42", seen_at: null },
      { id: "id77", seen_at: null },
    ],
  });
  getCb()([makeEntry(ctx, "id42", 0.7)]);
  getCb()([makeEntry(ctx, "id77", 0.7)]);
  getCb()([makeEntry(ctx, "id42", 0.7)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 2, "POST for id42 when leaving, then id77 when leaving");
  assert.equal(fetchCalls[0].url, "/api/items/id42/seen");
  assert.equal(fetchCalls[1].url, "/api/items/id77/seen");
});
