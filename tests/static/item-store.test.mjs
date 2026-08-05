// ---------------------------------------------------------------------------
// item-store.test.mjs — pagination cursor behaviour.
//
// The cursor is the id of an item we hold. When the server answers 410 the
// anchor row is gone (pruned, or its feed left the OPML and the rows
// cascaded), and the store steps the anchor back towards items we received
// earlier rather than reloading from page one and throwing the user's scroll
// position away.
// ---------------------------------------------------------------------------

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { createDomContext, loadScript } from "./dom-mock.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../src/static");

function makeStore(fetchImpl) {
  const ctx = createDomContext();
  ctx.window.MRR.config = { feedInitialCount: 2 };
  ctx.fetch = fetchImpl;
  loadScript(resolve(STATIC, "item-store.js"), ctx);
  return ctx.window.MRR.itemStore;
}

function jsonResponse(body) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}

function goneResponse() {
  return Promise.resolve({ ok: false, status: 410, json: () => Promise.resolve({}) });
}

test("sends after_id only, never after_feed_id or after_pub_date", async () => {
  const urls = [];
  const store = makeStore((url) => {
    urls.push(url);
    return urls.length === 1
      ? jsonResponse([{ id: "a", feed_id: "f", pub_date: null }])
      : jsonResponse([]);
  });

  await store.fetchPage();
  await store.fetchPage();

  assert.equal(urls.length, 2);
  assert.ok(!urls[0].includes("after_id"), "page one carries no cursor");
  assert.ok(urls[1].includes("after_id=a"), `expected after_id=a in ${urls[1]}`);
  assert.ok(!urls[1].includes("after_pub_date"), "pub_date is no longer part of the cursor");
  assert.ok(!urls[1].includes("after_feed_id"), "feed_id is no longer part of the cursor");
});

test("410 walks the anchor back without resetting held items", async () => {
  const urls = [];
  const store = makeStore((url) => {
    urls.push(url);
    if (urls.length === 1) {
      return jsonResponse([
        { id: "a", feed_id: "f", pub_date: null },
        { id: "b", feed_id: "f", pub_date: null },
        { id: "c", feed_id: "f", pub_date: null },
      ]);
    }
    if (urls.length <= 3) return goneResponse(); // c and b are both gone
    return jsonResponse([{ id: "d", feed_id: "f", pub_date: null }]);
  });

  await store.fetchPage();
  await store.fetchPage();

  assert.equal(urls.length, 4, "one page-one request plus three cursor attempts");
  assert.ok(urls[1].includes("after_id=c"));
  assert.ok(urls[2].includes("after_id=b"));
  assert.ok(urls[3].includes("after_id=a"));
  assert.deepEqual(
    store.getItems().map((i) => i.id).join(","),
    "a,b,c,d",
    "items already held survive the walk-back",
  );
  assert.equal(store.getCurrentIndex(), 0);
});

test("exhausting the walk-back stops pagination instead of looping", async () => {
  let calls = 0;
  const store = makeStore(() => {
    calls++;
    return calls === 1
      ? jsonResponse([{ id: "a", feed_id: "f", pub_date: null }])
      : goneResponse();
  });

  await store.fetchPage();
  await store.fetchPage();

  assert.ok(store.hasMoreItems() === false, "hasMore must go false, not retry forever");
  assert.ok(calls <= 8, `walk-back must be bounded; made ${calls} requests`);
});

test("a long run of dead anchors is crossed in log(n) requests, not abandoned", async () => {
  // 40 items held, the newest 20 all gone — a feed leaving the OPML cascades
  // its whole item set, so the run is not small. The old fixed cap of 5 gave
  // up here and stopped pagination for good; walking one at a time would cost
  // 20 requests.
  const held = Array.from({ length: 40 }, (_, i) => ({ id: `i${i}`, feed_id: "f", pub_date: null }));
  const dead = new Set(held.slice(20).map((i) => i.id));

  const anchors = [];
  const store = makeStore((url) => {
    const m = url.match(/after_id=([^&]+)/);
    if (!m) return jsonResponse(held);
    anchors.push(m[1]);
    return dead.has(m[1]) ? goneResponse() : jsonResponse([{ id: "new", feed_id: "f", pub_date: null }]);
  });

  await store.fetchPage();
  await store.fetchPage();

  assert.deepEqual(anchors, ["i39", "i38", "i37", "i35", "i31", "i23", "i7"], "back doubles: 0,1,2,4,8,16,32");
  assert.equal(anchors.length, 7, "log(n), not 21");
  assert.ok(store.hasMoreItems(), "pagination must survive the dead run");
  assert.equal(store.getItems().at(-1).id, "new");
});
