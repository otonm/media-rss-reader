// ---------------------------------------------------------------------------
// scroll-controller.test.mjs — tests for the seen-marking flow.
//
// scrollController marks items seen via two triggers:
//   1. IntersectionObserver threshold 0 — fires when element leaves viewport
//   2. pagehide — the item on screen when the tab closes
// Both call postSeen() which deduplicates via item.seen_at.
// ---------------------------------------------------------------------------

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { createDomContext, loadScript } from "./dom-mock.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../src/static");

// --- pagehide test harness ---

function setupPagehideHarness({ items = [{ id: "id42", seen_at: null }] } = {}) {
  const ctx = createDomContext();

  const callbacks = [];
  ctx.IntersectionObserver = class {
    constructor(cb) { callbacks.push(cb); }
    observe() {}
    unobserve() {}
    disconnect() {}
  };

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
  };
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
// pagehide — the last item of a session
// --------------------------------------------------------------------

test("pagehide marks the item on screen, which never leaves the viewport", () => {
  const { ctx, beaconCalls, items } = setupPagehideHarness({
    items: [{ id: "id1", seen_at: null }],
  });
  ctx.window.MRR.itemStore.getItemAt = (idx) => items[idx];

  ctx.window.dispatchEvent({ type: "pagehide" });

  assert.deepEqual(beaconCalls.map((c) => c.url), ["/api/items/id1/seen"]);
});

test("pagehide does not re-mark an item already seen", () => {
  const { ctx, beaconCalls, items } = setupPagehideHarness({
    items: [{ id: "id1", seen_at: "2026-06-10T00:00:00" }],
  });
  ctx.window.MRR.itemStore.getItemAt = (idx) => items[idx];

  ctx.window.dispatchEvent({ type: "pagehide" });

  assert.equal(beaconCalls.length, 0);
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
