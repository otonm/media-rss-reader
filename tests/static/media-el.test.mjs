// ---------------------------------------------------------------------------
// media-el.test.mjs — the single media-element factory.
// ---------------------------------------------------------------------------

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { createDomContext, loadScript } from "./dom-mock.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../src/static");

function setup() {
  const ctx = createDomContext();
  loadScript(resolve(STATIC, "media-el.js"), ctx);
  return ctx;
}

test("proxyUrl encodes both parameters", () => {
  const ctx = setup();
  const u = ctx.window.MRR.mediaEl.proxyUrl("https://e.com/a b.jpg", "id/1");
  assert.equal(u, "/api/media/proxy?url=https%3A%2F%2Fe.com%2Fa%20b.jpg&item_id=id%2F1");
});

test("create returns a video carrying playsinline", () => {
  const ctx = setup();
  const el = ctx.window.MRR.mediaEl.create({ url: "https://e.com/v.mp4", type: "video" }, "i1", {});
  assert.equal(el.tagName, "VIDEO");
  assert.ok(el.getAttribute("playsinline") !== null);
});

test("create defers offscreen slides", () => {
  const ctx = setup();
  const img = ctx.window.MRR.mediaEl.create({ url: "https://e.com/a.jpg", type: "image" }, "i1", { defer: true });
  assert.equal(img.loading, "lazy");
});
