// ---------------------------------------------------------------------------
// scroll-controller.test.mjs — tests for the seen-marking flow.
//
// scrollController uses an IntersectionObserver (threshold 0.6) for
// currentIndex tracking, and a debounced scroll event listener on #feed
// to mark items above the viewport as seen via POST /api/items/{id}/seen.
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

function setupHarness({ items = [{ id: "id42", seen_at: null }] } = {}) {
  const ctx = createDomContext();

  // Feed element — mock addEventListener to capture the scroll handler,
  // and getBoundingClientRect / querySelectorAll for position checks.
  let scrollHandler = null;
  const feedEls = [];
  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  feed.addEventListener = (type, handler) => { if (type === "scroll") scrollHandler = handler; };
  feed.removeEventListener = () => {};
  feed.querySelectorAll = (_sel) => feedEls;
  feed.getBoundingClientRect = () => ({ top: 0 });
  ctx.document.getElementById = (id) => (id === "feed" ? feed : null);

  const mainCallbacks = [];
  ctx.IntersectionObserver = class {
    constructor(cb) { mainCallbacks.push(cb); }
    observe() {}
    unobserve() {}
    disconnect() {}
  };

  // Fake setTimeout: call the callback immediately and return a fake timer id.
  let timerId = 0;
  const timerIds = [];
  ctx.setTimeout = (fn) => { const id = ++timerId; timerIds.push(id); fn(); return id; };
  ctx.clearTimeout = (_id) => {};

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
    feed,
    feedEls,
    markSeenCalls,
    fetchCalls,
    items,
    getMainCb: () => mainCallbacks[0],
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

test("items scrolled above the viewport trigger seen POST", async () => {
  const { feedEls, fetchCalls, markSeenCalls, fireScroll, ctx } = setupHarness({
    items: [
      { id: "id1", seen_at: null },
      { id: "id2", seen_at: null },
      { id: "id3", seen_at: null },
    ],
  });
  // id1 above viewport (bottom < feedTop=0), id2 and id3 below
  feedEls.push(makeEl(ctx, "id1", "media-item", -10));
  feedEls.push(makeEl(ctx, "id2", "placeholder", 200));
  feedEls.push(makeEl(ctx, "id3", "media-item", 500));

  fireScroll();
  await new Promise((r) => setImmediate(r));

  assert.equal(fetchCalls.length, 1, "only item above viewport gets POST");
  assert.equal(fetchCalls[0].url, "/api/items/id1/seen");
  assert.equal(fetchCalls[0].opts.method, "POST");
  assert.ok(
    markSeenCalls.some((c) => c.who === "store" && c.id === "id1" && c.ts === "2026-06-11T12:00:00"),
    "itemStore.markSeen for id1",
  );
  assert.ok(
    markSeenCalls.some((c) => c.who === "feed" && c.id === "id1"),
    "feedView.markSeen for id1",
  );
});

test("no items above viewport = no POST", async () => {
  const { feedEls, fetchCalls, fireScroll, ctx } = setupHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  feedEls.push(makeEl(ctx, "id1", "media-item", 100));

  fireScroll();
  await new Promise((r) => setImmediate(r));

  assert.equal(fetchCalls.length, 0, "no items above viewport — no POST");
});

test("multiple items above viewport each get a POST", async () => {
  const { feedEls, fetchCalls, fireScroll, ctx } = setupHarness({
    items: [
      { id: "id1", seen_at: null },
      { id: "id2", seen_at: null },
    ],
  });
  feedEls.push(makeEl(ctx, "id1", "media-item", -50));
  feedEls.push(makeEl(ctx, "id2", "placeholder", -20));

  fireScroll();
  await new Promise((r) => setImmediate(r));

  assert.equal(fetchCalls.length, 2, "both items above viewport get POST");
  assert.equal(fetchCalls[0].url, "/api/items/id1/seen");
  assert.equal(fetchCalls[1].url, "/api/items/id2/seen");
});

test("already seen item does NOT trigger a second POST", async () => {
  const { feedEls, fetchCalls, fireScroll, ctx } = setupHarness({
    items: [
      { id: "id1", seen_at: "2026-06-10T00:00:00" },
      { id: "id2", seen_at: null },
    ],
  });
  feedEls.push(makeEl(ctx, "id1", "media-item", -10));
  feedEls.push(makeEl(ctx, "id2", "media-item", -5));

  fireScroll();
  await new Promise((r) => setImmediate(r));

  assert.equal(fetchCalls.length, 1, "only unseen item gets POST");
  assert.equal(fetchCalls[0].url, "/api/items/id2/seen");
});

test("failed POST does NOT trigger markSeen callbacks", async () => {
  const ctx = createDomContext();

  let scrollHandler = null;
  const feedEls = [];
  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  feed.addEventListener = (type, handler) => { if (type === "scroll") scrollHandler = handler; };
  feed.querySelectorAll = () => feedEls;
  feed.getBoundingClientRect = () => ({ top: 0 });
  ctx.document.getElementById = () => feed;

  let timerId = 0;
  ctx.setTimeout = (fn) => { ++timerId; fn(); return timerId; };
  ctx.clearTimeout = () => {};

  ctx.IntersectionObserver = class { constructor() {} observe() {} unobserve() {} disconnect() {} };
  ctx.fetch = () => Promise.resolve({ ok: false, json: async () => ({ detail: "fail" }) });

  const markSeenCalls = [];
  ctx.window.MRR.itemStore = {
    getItems: () => [
      { id: "id1", seen_at: null },
    ],
    getCurrentIndex: () => 0,
    getItemAt: () => null,
    findIndexById: () => 0,
    setCurrentIndex: () => {},
    markSeen: (id, ts) => { markSeenCalls.push({ who: "store", id, ts }); },
  };
  ctx.window.MRR.feedView = { markSeen: () => {}, setCurrentMedia: () => {} };
  ctx.window.MRR.cacheQueue = { rebuild: () => {} };
  ctx.window.MRR.autoscrollController = { reset: () => {} };
  ctx.window.MRR.config = { feedInitialCount: 10 };

  loadScript(resolve(STATIC, "scroll-controller.js"), ctx);
  ctx.window.MRR.scrollController.init();

  const el = ctx.document.createElement("div");
  el.className = "media-item";
  el.dataset.id = "id1";
  el.getBoundingClientRect = () => ({ bottom: -10 });
  feedEls.push(el);

  scrollHandler();
  await new Promise((r) => setImmediate(r));

  assert.equal(markSeenCalls.length, 0, "no markSeen callbacks on failed POST");
});

test("scroll handler is debounced (timer reset on each scroll)", async () => {
  let clearCalls = 0;
  let setTimeoutCalls = 0;
  let lastClearArg = undefined;

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
  ctx.clearTimeout = (id) => { clearCalls++; lastClearArg = id; };

  ctx.IntersectionObserver = class { constructor() {} observe() {} unobserve() {} disconnect() {} };
  ctx.window.MRR.itemStore = { getItems: () => [], getCurrentIndex: () => 0, getItemAt: () => null, findIndexById: () => -1, setCurrentIndex: () => {} };
  ctx.window.MRR.feedView = { markSeen: () => {}, setCurrentMedia: () => {} };
  ctx.window.MRR.cacheQueue = { rebuild: () => {} };
  ctx.window.MRR.autoscrollController = { reset: () => {} };
  ctx.window.MRR.config = { feedInitialCount: 10 };

  loadScript(resolve(STATIC, "scroll-controller.js"), ctx);
  ctx.window.MRR.scrollController.init();

  scrollHandler();
  // First scroll: clearTimeout(null) + setTimeout → 1 set
  assert.equal(setTimeoutCalls, 1);

  scrollHandler();
  // Second scroll: clearTimeout(prevTimerId) + setTimeout → 2 sets
  assert.equal(clearCalls, 2);
  assert.equal(lastClearArg, timerId - 1);
  assert.equal(setTimeoutCalls, 2);
});