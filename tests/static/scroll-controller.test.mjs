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

  const beaconCalls = [];
  ctx.navigator = {
    sendBeacon: (url) => {
      beaconCalls.push({ url });
      return true;
    },
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
    ctx, feed, feedEls, markSeenCalls, beaconCalls, items,
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

  const beaconCalls = [];
  ctx.navigator = {
    sendBeacon: (url) => {
      beaconCalls.push({ url });
      return true;
    },
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
    ctx, markSeenCalls, beaconCalls, items,
    getSeenCb: () => callbacks[1],
  };
}

// --------------------------------------------------------------------
// Scroll event tests
// --------------------------------------------------------------------

test("scroll: items above viewport trigger seen POST", async () => {
  const { feedEls, beaconCalls, markSeenCalls, fireScroll, ctx } = setupScrollHarness({
    items: [
      { id: "id1", seen_at: null },
      { id: "id2", seen_at: null },
    ],
  });
  feedEls.push(makeEl(ctx, "id1", "media-item", -10));
  feedEls.push(makeEl(ctx, "id2", "placeholder", 200));

  fireScroll();
  await new Promise((r) => setImmediate(r));

  assert.equal(beaconCalls.length, 1);
  assert.equal(beaconCalls[0].url, "/api/items/id1/seen");
  // Marked locally before the beacon leaves, so the timestamp is client-side.
  assert.ok(
    markSeenCalls.some((c) => c.who === "store" && c.id === "id1" && typeof c.ts === "string"),
  );
  assert.ok(markSeenCalls.some((c) => c.who === "feed" && c.id === "id1"));
});

test("scroll: no items above viewport = no POST", async () => {
  const { feedEls, beaconCalls, fireScroll, ctx } = setupScrollHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  feedEls.push(makeEl(ctx, "id1", "media-item", 100));
  fireScroll();
  await new Promise((r) => setImmediate(r));
  assert.equal(beaconCalls.length, 0);
});

test("scroll: 1px tolerance handles sub-pixel bottom", async () => {
  const { feedEls, beaconCalls, fireScroll, ctx } = setupScrollHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  feedEls.push(makeEl(ctx, "id1", "media-item", 0.5)); // 0.5 ≤ 0+1 = true
  fireScroll();
  await new Promise((r) => setImmediate(r));
  assert.equal(beaconCalls.length, 1);
});

test("scroll: positive bottom beyond tolerance is not marked", async () => {
  const { feedEls, beaconCalls, fireScroll, ctx } = setupScrollHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  feedEls.push(makeEl(ctx, "id1", "media-item", 5)); // 5 > 0+1 = false
  fireScroll();
  await new Promise((r) => setImmediate(r));
  assert.equal(beaconCalls.length, 0);
});

test("scroll: already seen item deduplicates", async () => {
  const { feedEls, beaconCalls, fireScroll, ctx } = setupScrollHarness({
    items: [
      { id: "id1", seen_at: "2026-06-10T00:00:00" },
      { id: "id2", seen_at: null },
    ],
  });
  feedEls.push(makeEl(ctx, "id1", "media-item", -10));
  feedEls.push(makeEl(ctx, "id2", "media-item", -5));
  fireScroll();
  await new Promise((r) => setImmediate(r));
  assert.equal(beaconCalls.length, 1);
  assert.equal(beaconCalls[0].url, "/api/items/id2/seen");
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
  const { ctx, beaconCalls, markSeenCalls, getSeenCb } = setupIOHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  getSeenCb()([ioEntry(ctx, "id1", "image", false, -10)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(beaconCalls.length, 1);
  assert.equal(beaconCalls[0].url, "/api/items/id1/seen");
  assert.ok(markSeenCalls.some((c) => c.who === "store" && c.id === "id1"));
  assert.ok(markSeenCalls.some((c) => c.who === "feed" && c.id === "id1"));
});

test("seen beacon carries media_url so the mark survives a pruned row", async () => {
  // prune_items evicts oldest-first and the feed is served oldest-first, so the
  // row is routinely gone by the time this fires. The server needs the URL to
  // write seen_media, which is the record meant to outlive pruning.
  const { ctx, beaconCalls, getSeenCb } = setupIOHarness({
    items: [{ id: "id1", seen_at: null, media_url: "https://i.redd.it/a b.jpg" }],
  });
  getSeenCb()([ioEntry(ctx, "id1", "image", false, -10)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(beaconCalls.length, 1);
  assert.equal(beaconCalls[0].url, "/api/items/id1/seen?media_url=https%3A%2F%2Fi.redd.it%2Fa%20b.jpg");
});

test("seen beacon omits media_url rather than sending the string 'undefined'", async () => {
  const { ctx, beaconCalls, getSeenCb } = setupIOHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  getSeenCb()([ioEntry(ctx, "id1", "image", false, -10)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(beaconCalls[0].url, "/api/items/id1/seen");
});

test("IO: item still intersecting does NOT trigger POST", async () => {
  const { beaconCalls, getSeenCb, ctx } = setupIOHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  getSeenCb()([ioEntry(ctx, "id1", "image", true, 200)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(beaconCalls.length, 0);
});

test("IO: item below viewport (bottom > 0, not intersecting) does NOT trigger POST", async () => {
  const { beaconCalls, getSeenCb, ctx } = setupIOHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  getSeenCb()([ioEntry(ctx, "id1", "image", false, 50)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(beaconCalls.length, 0);
});

test("IO: placeholder (no mediaType) skipped", async () => {
  const { beaconCalls, getSeenCb, ctx } = setupIOHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  getSeenCb()([ioEntry(ctx, "id1", null, false, -10)]); // no mediaType
  await new Promise((r) => setImmediate(r));
  assert.equal(beaconCalls.length, 0);
});

test("IO: already seen item deduplicates", async () => {
  const { beaconCalls, getSeenCb, ctx } = setupIOHarness({
    items: [{ id: "id1", seen_at: "2026-06-10T00:00:00" }],
  });
  getSeenCb()([ioEntry(ctx, "id1", "image", false, -10)]);
  await new Promise((r) => setImmediate(r));
  assert.equal(beaconCalls.length, 0);
});

// --------------------------------------------------------------------
// pagehide — the last item of a session
// --------------------------------------------------------------------

test("pagehide marks the item on screen, which never leaves the viewport", () => {
  const { ctx, beaconCalls, items } = setupScrollHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  ctx.window.MRR.itemStore.getItemAt = (idx) => items[idx];

  ctx.window.dispatchEvent({ type: "pagehide" });

  assert.deepEqual(beaconCalls.map((c) => c.url), ["/api/items/id1/seen"]);
});

test("pagehide does not re-mark an item already seen", () => {
  const { ctx, beaconCalls, items } = setupScrollHarness({
    items: [{ id: "id1", seen_at: "2026-06-10T00:00:00" }],
  });
  ctx.window.MRR.itemStore.getItemAt = (idx) => items[idx];

  ctx.window.dispatchEvent({ type: "pagehide" });

  assert.equal(beaconCalls.length, 0);
});
