// ---------------------------------------------------------------------------
// seen-toggle.test.mjs — tests for the showSeen toggle routing in
// item-store.
//
// itemStore.fetchPage() must read its `unseen` filter from the
// `showSeen` state, NOT hard-code `unseen=true`:
//   showSeen=true  → ?unseen=false  (all items)
//   showSeen=false → ?unseen=true   (only unseen — current default)
// ---------------------------------------------------------------------------

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { createDomContext, loadScript } from "./dom-mock.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../src/static");

function setupHarness({ showSeen }) {
  const ctx = createDomContext();
  ctx.fetchCalls = [];
  ctx.fetch = (url, opts) => {
    ctx.fetchCalls.push({ url, opts });
    return Promise.resolve({ ok: true, json: async () => [{ id: "x" }] });
  };
  ctx.window.MRR.config = { feedInitialCount: 10 };
  loadScript(resolve(STATIC, "item-store.js"), ctx);
  ctx.window.MRR.itemStore.setShowSeen(showSeen);
  return ctx;
}

test("fetchPage issues ?unseen=true when showSeen=false (default)", async () => {
  const ctx = setupHarness({ showSeen: false });
  await ctx.window.MRR.itemStore.fetchPage();
  const itemsCalls = ctx.fetchCalls.filter((c) => c.url.startsWith("/api/items?"));
  assert.equal(itemsCalls.length, 1);
  assert.ok(itemsCalls[0].url.includes("unseen=true"), `expected unseen=true in URL, got: ${itemsCalls[0].url}`);
});

test("fetchPage issues ?unseen=false when showSeen=true", async () => {
  const ctx = setupHarness({ showSeen: true });
  await ctx.window.MRR.itemStore.fetchPage();
  const itemsCalls = ctx.fetchCalls.filter((c) => c.url.startsWith("/api/items?"));
  assert.equal(itemsCalls.length, 1);
  assert.ok(itemsCalls[0].url.includes("unseen=false"), `expected unseen=false in URL, got: ${itemsCalls[0].url}`);
});

test("setShowSeen flips the routing without requiring a re-load of the module", async () => {
  const ctx = setupHarness({ showSeen: false });
  await ctx.window.MRR.itemStore.fetchPage();
  ctx.window.MRR.itemStore.setShowSeen(true);
  await ctx.window.MRR.itemStore.fetchPage();
  const itemsCalls = ctx.fetchCalls.filter((c) => c.url.startsWith("/api/items?"));
  assert.equal(itemsCalls.length, 2);
  assert.ok(itemsCalls[0].url.includes("unseen=true"));
  assert.ok(itemsCalls[1].url.includes("unseen=false"));
});

test("fetchPage offsets by the unseen items it holds, not by a page number", async () => {
  // With unseen=true the server's result set shrinks as items are marked
  // seen. A page number multiplied by size therefore over-shoots and skips
  // items, which then only resurfaced on the next reload.
  const ctx = setupHarness({ showSeen: false });
  const store = ctx.window.MRR.itemStore;

  await store.fetchPage();
  let itemsCalls = ctx.fetchCalls.filter((c) => c.url.startsWith("/api/items?"));
  assert.ok(itemsCalls[0].url.includes("offset=0"), itemsCalls[0].url);

  // One item held and still unseen → offset 1.
  await store.fetchPage();
  itemsCalls = ctx.fetchCalls.filter((c) => c.url.startsWith("/api/items?"));
  assert.ok(itemsCalls[1].url.includes("offset=1"), itemsCalls[1].url);

  // Mark it seen: it drops out of the server's set, so the offset drops too.
  store.markSeen("x", "2026-06-11T12:00:00");
  await store.fetchPage();
  itemsCalls = ctx.fetchCalls.filter((c) => c.url.startsWith("/api/items?"));
  assert.ok(itemsCalls[2].url.includes("offset=0"), itemsCalls[2].url);
});

test("fetchPage offsets by every held item when showSeen is on", async () => {
  const ctx = setupHarness({ showSeen: true });
  const store = ctx.window.MRR.itemStore;
  await store.fetchPage();
  store.markSeen("x", "2026-06-11T12:00:00");
  await store.fetchPage();
  const itemsCalls = ctx.fetchCalls.filter((c) => c.url.startsWith("/api/items?"));
  assert.ok(itemsCalls[1].url.includes("offset=1"), itemsCalls[1].url);
});

test("fetchPage does not append an item it already holds", async () => {
  const ctx = setupHarness({ showSeen: true });
  const store = ctx.window.MRR.itemStore;
  await store.fetchPage();
  await store.fetchPage(); // the stub always returns the same item id
  assert.equal(store.getItems().length, 1);
  assert.equal(store.getItems()[0].id, "x");
});

test("markSeen updates the in-memory item's seen_at so the next render shows the checkmark", () => {
  const ctx = setupHarness({ showSeen: false });
  const store = ctx.window.MRR.itemStore;
  store.getItems().push({ id: "i1", seen_at: null });
  store.markSeen("i1", "2026-06-11T12:00:00");
  assert.equal(store.getItems()[0].seen_at, "2026-06-11T12:00:00");
});
