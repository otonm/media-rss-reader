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

import { createDomContext, loadScript } from "./dom-mock.mjs";

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
  };
  // config stub
  ctx.window.MRR.config = { feedInitialCount: 10 };
  loadScript(resolve(STATIC, "cache-queue.js"), ctx);
  return ctx;
}

test("rebuild fires POST /api/prefetch/hint with the current item id", async () => {
  const items = makeItems(5, "iiiii");
  const ctx = makeContext(items);
  // start the queue so the worker runs (rebuild also calls processNext)
  ctx.window.MRR.cacheQueue.start();
  // Give the microtask queue a chance to run before rebuild.
  await Promise.resolve();
  ctx.window.MRR.cacheQueue.rebuild(0, 10, items);
  // The hint call is fire-and-forget; the rebuild doesn't await it, but
  // the fetch is invoked synchronously inside rebuild.
  const hintCalls = ctx.fetchCalls.filter((c) => c.url === "/api/prefetch/hint");
  assert.equal(hintCalls.length, 1, "expected exactly one /api/prefetch/hint call");
  assert.equal(hintCalls[0].opts.method, "POST");
  const body = JSON.parse(hintCalls[0].opts.body);
  assert.equal(body.item_id, "id0");
});
