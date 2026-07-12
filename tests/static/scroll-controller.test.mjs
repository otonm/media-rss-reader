// ---------------------------------------------------------------------------
// scroll-controller.test.mjs — tests for the seen-marking flow.
//
// scrollController uses three seen-marking triggers:
//   1. IntersectionObserver threshold 0 — fires when element leaves viewport
//   2. Debounced scroll event on #feed — secondary desktop fallback
// Both call postSeen() which deduplicates via item.seen_at.
// ---------------------------------------------------------------------------

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { createDomContext, loadScript } from "./dom-mock.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../src/static");

// --- scroll-event test harness ---

function setupScrollHarness({ items = [{ id: "id42", seen_at: null }] } = {}) {
  const ctx = createDomContext();

  let scrollHandler = null;
  const feedEls = [];
  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  feed.addEventListener = (type, handler) => { if (type === "scroll") scrollHandler = handler; };
  feed.removeEventListener = () => {};
  feed.querySelectorAll = (_sel) => feedEls;
  feed.getBoundingClientRect = () => ({ top: 0 });
  ctx.document.getElementById = (id) => (id === "feed" ? feed : null);

  const callbacks = [];
  ctx.IntersectionObserver = class {
    constructor(cb) { callbacks.push(cb); }
    observe() {}
    unobserve() {}
    disconnect() {}
  };

  let timerId = 0;
  ctx.setTimeout = (fn) => { ++timerId; fn(); return timerId; };
  ctx.clearTimeout = () => {};

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
    ctx, feed, feedEls, markSeenCalls, fetchCalls, items,
    getMainCb: () => callbacks[0],
    getSeenCb: () => callbacks[1],
    fireScroll: () => { if (scrollHandler) scrollHandler(); },
  };
}

function makeEl(ctx, id, className, bottom) {
  const el = ctx.document.createElement("div");
  el.className = className;
  el.dataset.id = id;
  el.getBoundingClientRect = () => ({ bottom });
  return el;
}

// --- IntersectionObserver threshold-0 test harness ---

function setupIOHarness({ items = [{ id: "id42", seen_at: null }] } = {}) {
  const ctx = createDomContext();

  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  feed.addEventListener = () => {};
  ctx.document.getElementById = () => feed;

  const callbacks = [];
  ctx.IntersectionObserver = class {
    constructor(cb) { callbacks.push(cb); }
    observe() {}
    unobserve() {}
    disconnect() {}
  };

  ctx.setTimeout = () => 1;
  ctx.clearTimeout = () => {};

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
    ctx, markSeenCalls, fetchCalls, items,
    getSeenCb: () => callbacks[1],
  };
}

// --------------------------------------------------------------------
// Scroll event tests
// --------------------------------------------------------------------

test("scroll: items above viewport trigger seen POST", async () => {
  const { feedEls, fetchCalls, markSeenCalls, fireScroll, ctx } = setupScrollHarness({
    items: [
      { id: "id1", seen_at: null },
      { id: "id2", seen_at: null },
    ],
  });
  feedEls.push(makeEl(ctx, "id1", "media-item", -10));
  feedEls.push(makeEl(ctx, "id2", "placeholder", 200));

  fireScroll();
  await new Promise((r) => setImmediate(r));

  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0].url, "/api/items/id1/seen");
  assert.ok(
    markSeenCalls.some((c) => c.who === "store" && c.id === "id1" && c.ts === "2026-06-11T12:00:00"),
  );
  assert.ok(markSeenCalls.some((c) => c.who === "feed" && c.id === "id1"));
});

test("scroll: no items above viewport = no POST", async () => {
  const { feedEls, fetchCalls, fireScroll, ctx } = setupScrollHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  feedEls.push(makeEl(ctx, "id1", "media-item", 100));
  fireScroll();
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 0);
});

test("scroll: 1px tolerance handles sub-pixel bottom", async () => {
  const { feedEls, fetchCalls, fireScroll, ctx } = setupScrollHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  feedEls.push(makeEl(ctx, "id1", "media-item", 0.5)); // 0.5 ≤ 0+1 = true
  fireScroll();
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 1);
});

test("scroll: positive bottom beyond tolerance is not marked", async () => {
  const { feedEls, fetchCalls, fireScroll, ctx } = setupScrollHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  feedEls.push(makeEl(ctx, "id1", "media-item", 5)); // 5 > 0+1 = false
  fireScroll();
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 0);
});

test("scroll: already seen item deduplicates", async () => {
  const { feedEls, fetchCalls, fireScroll, ctx } = setupScrollHarness({
    items: [
      { id: "id1", seen_at: "2026-06-10T00:00:00" },
      { id: "id2", seen_at: null },
    ],
  });
  feedEls.push(makeEl(ctx, "id1", "media-item", -10));
  feedEls.push(makeEl(ctx, "id2", "media-item", -5));
  fireScroll();
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0].url, "/api/items/id2/seen");
});

test("scroll: debounce timer reset on successive scrolls", async () => {
  let clearCalls = 0;
  let setTimeoutCalls = 0;

  const ctx = createDomContext();
  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  let scrollHandler = null;
  feed.addEventListener = (type, handler) => { if (type === "scroll") scrollHandler = handler; };
  feed.getBoundingClientRect = () => ({ top: 0 });
  feed.querySelectorAll = () => [];
  ctx.document.getElementById = () => feed;

  let timerId = 0;
  ctx.setTimeout = () => { setTimeoutCalls++; return ++timerId; };
  ctx.clearTimeout = (id) => { clearCalls++; };
  ctx.IntersectionObserver = class { constructor() {} observe() {} unobserve() {} disconnect() {} };
  ctx.window.MRR.itemStore = { getItems: () => [], getCurrentIndex: () => 0, getItemAt: () => null, findIndexById: () => -1, setCurrentIndex: () => {} };
  ctx.window.MRR.feedView = { markSeen: () => {}, setCurrentMedia: () => {} };
  ctx.window.MRR.cacheQueue = { rebuild: () => {} };
  ctx.window.MRR.autoscrollController = { reset: () => {} };
  ctx.window.MRR.config = { feedInitialCount: 10 };

  loadScript(resolve(STATIC, "scroll-controller.js"), ctx);
  ctx.window.MRR.scrollController.init();

  scrollHandler();
  assert.equal(setTimeoutCalls, 1);
  scrollHandler();
  assert.equal(clearCalls, 2);
  assert.equal(setTimeoutCalls, 2);
});

// --------------------------------------------------------------------
// IntersectionObserver threshold-0 tests (mobile fallback)
// --------------------------------------------------------------------

function ioEntry(ctx, id, mediaType, isIntersecting, bottom) {
  const el = ctx.document.createElement("div");
  el.dataset.id = id;
  if (mediaType) el.dataset.mediaType = mediaType;
  el.getBoundingClientRect = () => ({ bottom });
  return { target: el, isIntersecting, boundingClientRect: { bottom } };
}

test("IO: item leaving viewport upward triggers seen POST", async () => {
  const { ctx, fetchCalls, markSeenCalls, getSeenCb } = setupIOHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  getSeenCb()([ioEntry(ctx, "id1", "image", false, -10)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0].url, "/api/items/id1/seen");
  assert.ok(markSeenCalls.some((c) => c.who === "store" && c.id === "id1"));
  assert.ok(markSeenCalls.some((c) => c.who === "feed" && c.id === "id1"));
});

test("IO: item still intersecting does NOT trigger POST", async () => {
  const { fetchCalls, getSeenCb, ctx } = setupIOHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  getSeenCb()([ioEntry(ctx, "id1", "image", true, 200)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 0);
});

test("IO: item below viewport (bottom > 0, not intersecting) does NOT trigger POST", async () => {
  const { fetchCalls, getSeenCb, ctx } = setupIOHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  getSeenCb()([ioEntry(ctx, "id1", "image", false, 50)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 0);
});

test("IO: placeholder (no mediaType) skipped", async () => {
  const { fetchCalls, getSeenCb, ctx } = setupIOHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  getSeenCb()([ioEntry(ctx, "id1", null, false, -10)]); // no mediaType
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 0);
});

test("IO: already seen item deduplicates", async () => {
  const { fetchCalls, getSeenCb, ctx } = setupIOHarness({
    items: [{ id: "id1", seen_at: "2026-06-10T00:00:00" }],
  });
  getSeenCb()([ioEntry(ctx, "id1", "image", false, -10)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(fetchCalls.length, 0);
});
