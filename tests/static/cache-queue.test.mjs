// ---------------------------------------------------------------------------
// cache-queue.test.mjs — tests for cache-queue side-effects.
//
// The user clarified they want the natural feed order preserved (image, then
// next video, then next image, etc.). The browser-side cache-queue already
// downloads in feed order, so the only behavioural change here is firing
// POST /api/prefetch/hint on every rebuild so the server-side disk cache
// gets pre-warmed for items further ahead.
// ---------------------------------------------------------------------------

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { createDomContext, fakeTimeout, loadScript } from "./dom-mock.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../src/static");

function makeItems(n, types) {
  // types: "v"=video, "i"=image, "g"=gif
  return Array.from({ length: n }, (_, i) => ({
    id: `id${i}`,
    media_type:
      types[i] === "v" ? "video" : types[i] === "g" ? "gif" : "image",
    media_url: `https://example/${i}.mp4`,
  }));
}

// Harness: install a fake MRR.itemStore with the items + a fetch spy so
// we can assert that rebuild() hits /api/prefetch/hint.
function makeContext(items) {
  const ctx = createDomContext();
  // The per-download deadline must not arm a real 10s timer in tests.
  ctx.clock = fakeTimeout(ctx);
  // Track fetch calls.
  ctx.fetchCalls = [];
  ctx.fetch = (url, opts) => {
    ctx.fetchCalls.push({ url, opts });
    return Promise.resolve({ ok: true, json: async () => ({ count: items.length }) });
  };
  // itemStore stub
  ctx.window.MRR.itemStore = {
    getItems: () => items,
    getCurrentIndex: () => 0,
    getItemAt: (i) => items[i],
    getShowSeen: () => false,
  };
  // config stub
  ctx.window.MRR.config = { feedInitialCount: 10 };
  loadScript(resolve(STATIC, "cache-queue.js"), ctx);
  return ctx;
}

// item_id query param of each proxy URL requested so far, in request order.
function requestedIds(ctx) {
  return ctx._images
    .filter((img) => img.src)
    .map((img) => decodeURIComponent(img.src.split("item_id=")[1]));
}

test("rebuild fires POST /api/prefetch/hint with the current item id", async () => {
  const items = makeItems(5, "iiiii");
  const ctx = makeContext(items);
  // start the queue so the worker runs (rebuild also calls processNext)
  ctx.window.MRR.cacheQueue.start();
  // Give the microtask queue a chance to run before rebuild.
  await Promise.resolve();
  ctx.window.MRR.cacheQueue.rebuild(0, 10, items);
  // The hint is debounced; advance the clock to fire it.
  ctx.clock.advance(300);
  // The hint call is fire-and-forget; the rebuild doesn't await it, but
  // the fetch is invoked after the debounce timeout.
  const hintCalls = ctx.fetchCalls.filter((c) => c.url === "/api/prefetch/hint");
  assert.equal(hintCalls.length, 1, "expected exactly one /api/prefetch/hint call");
  assert.equal(hintCalls[0].opts.method, "POST");
  const body = JSON.parse(hintCalls[0].opts.body);
  assert.equal(body.item_id, "id0");
});

test("already-cached items are downloaded before uncached ones", async () => {
  // Items the server reports as on-disk decode in milliseconds; items that are
  // not make the browser wait on the origin. Ordering the queue by that turns
  // a screen of spinners into a screen of pictures.
  const items = makeItems(5, "iiiii");
  items[2].cached = true;
  items[4].cached = true;
  const ctx = makeContext(items);

  ctx.window.MRR.cacheQueue.start();
  ctx.window.MRR.cacheQueue.rebuild(0, 10, items);
  await Promise.resolve();

  // Three workers start three downloads immediately: the current item first
  // regardless of cache state, then the cached ones ahead of the uncached.
  assert.deepEqual(requestedIds(ctx), ["id0", "id2", "id4"]);
});

test("three downloads run concurrently, so one slow item cannot block the rest", async () => {
  const items = makeItems(6, "iiiiii");
  const ctx = makeContext(items);

  ctx.window.MRR.cacheQueue.start();
  ctx.window.MRR.cacheQueue.rebuild(0, 10, items);
  await Promise.resolve();

  assert.equal(requestedIds(ctx).length, 3);
  assert.equal(ctx.window.MRR.cacheQueue.getStats().loading, 3);
});

test("a download that never loads fails on the deadline instead of spinning forever", async () => {
  const items = makeItems(1, "i");
  const ctx = makeContext(items);
  const failed = [];
  ctx.window.MRR.cacheQueue.on("item-failed", (id, reason) => failed.push({ id, reason }));

  ctx.window.MRR.cacheQueue.start();
  ctx.window.MRR.cacheQueue.rebuild(0, 10, items);
  await Promise.resolve();
  assert.deepEqual(failed, [], "must not fail before the deadline");

  ctx.clock.advance(10000);
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(failed.length, 1);
  assert.equal(failed[0].id, "id0");
  assert.match(failed[0].reason, /timed out/);
  // The element's src is cleared so the browser drops the connection.
  assert.equal(ctx._images[0].src, "");
});

test("a successful load clears the deadline and reports how long it took", async () => {
  const items = makeItems(1, "i");
  const ctx = makeContext(items);
  const loaded = [];
  ctx.window.MRR.cacheQueue.on("item-loaded", (id, el, ms) => loaded.push({ id, ms }));

  ctx.window.MRR.cacheQueue.start();
  ctx.window.MRR.cacheQueue.rebuild(0, 10, items);
  await Promise.resolve();

  ctx._images[0].dispatchEvent({ type: "load" });
  await Promise.resolve();

  assert.equal(loaded.length, 1);
  assert.equal(loaded[0].id, "id0");
  assert.equal(typeof loaded[0].ms, "number");
  // Fire the debounced hint timer before checking that all timers are cleared.
  ctx.clock.advance(300);
  assert.equal(ctx.clock.pending(), 0, "the deadline timer and hint timer must be cleared");
});

test("the prefetch hint is debounced across a burst of rebuilds", async () => {
  const hints = [];
  const ctx = createDomContext();
  ctx.clock = fakeTimeout(ctx);
  ctx.fetch = async (url, opts) => {
    if (opts && opts.method === "POST") {
      hints.push(JSON.parse(opts.body));
    }
    return { ok: true, json: async () => ({ status: "ok" }) };
  };

  const items = [
    { id: "a", media: [{ url: "http://x/a.jpg", type: "image" }] },
    { id: "b", media: [{ url: "http://x/b.jpg", type: "image" }] },
    { id: "c", media: [{ url: "http://x/c.jpg", type: "image" }] },
  ];
  ctx.window.MRR = { cacheQueue: {}, itemStore: { getShowSeen: () => false } };
  loadScript(resolve(STATIC, "cache-queue.js"), ctx);

  // Simulate a burst of scroll snaps changing the current item
  ctx.window.MRR.cacheQueue.rebuild(0, 5, items); // hints item "a"
  ctx.window.MRR.cacheQueue.rebuild(1, 5, items); // hints item "b"
  ctx.window.MRR.cacheQueue.rebuild(2, 5, items); // hints item "c"
  // Advance the clock past the debounce timeout (250ms)
  ctx.clock.advance(400);
  await Promise.resolve();

  assert.equal(hints.length, 1, "one hint per burst, not one per scroll snap");
  assert.equal(hints[0].item_id, "c", "the trailing call must use the newest item id");
  assert.equal(ctx.clock.pending(), 0, "timer must be cleared after hint is sent");
});

test("a successful load flips the item's cached flag", async () => {
  // item.cached is the server's disk snapshot from when the page was fetched,
  // and nothing else ever updates it — so an item downloaded seconds ago still
  // read MISS in the UI_DEBUG overlay when the user scrolled back to it.
  const items = makeItems(1, "i");
  assert.equal(items[0].cached, undefined, "starts as a miss");
  const ctx = makeContext(items);

  ctx.window.MRR.cacheQueue.start();
  ctx.window.MRR.cacheQueue.rebuild(0, 10, items);
  await Promise.resolve();

  ctx._images[0].dispatchEvent({ type: "load" });
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(items[0].cached, true, "the overlay must report HIT after the download");
  ctx.clock.advance(400);
});

test("a failed load leaves the item's cached flag alone", async () => {
  const items = makeItems(1, "i");
  const ctx = makeContext(items);

  ctx.window.MRR.cacheQueue.start();
  ctx.window.MRR.cacheQueue.rebuild(0, 10, items);
  await Promise.resolve();

  ctx._images[0].dispatchEvent({ type: "error" });
  await Promise.resolve();
  await Promise.resolve();

  assert.notEqual(items[0].cached, true);
  ctx.clock.advance(400);
});
