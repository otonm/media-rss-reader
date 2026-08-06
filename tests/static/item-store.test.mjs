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

test("fetchPage sends the rank the server issued with the anchor", async () => {
  const urls = [];
  const store = makeStore((url) => {
    urls.push(url);
    return urls.length === 1
      ? jsonResponse([{ id: "a", feed_id: "f", pub_date: null, rn: 3 }])
      : jsonResponse([]);
  });

  await store.fetchPage();
  await store.fetchPage();

  assert.ok(!urls[0].includes("after_rn"), "page one has no anchor");
  assert.ok(urls[1].includes("after_id=a"), "page two anchors on the last item");
  assert.ok(urls[1].includes("after_rn=3"), "and carries the rank it was issued");
});

test("a page of pure duplicates re-anchors instead of stalling", async () => {
  const urls = [];
  const held = [
    { id: "a", feed_id: "f", pub_date: null, rn: 1 },
    { id: "b", feed_id: "f", pub_date: null, rn: 2 },
  ];
  const store = makeStore((url) => {
    urls.push(url);
    // Call 1 seeds state.items with the held rows. Call 2 (the first cursored
    // request of the next fetchPage) comes back as the exact same rows — the
    // bound resolved beneath our position. Call 3 is the first genuinely new row.
    if (urls.length === 1) return jsonResponse(held);
    if (urls.length === 2) return jsonResponse(held);
    return jsonResponse([{ id: "c", feed_id: "f", pub_date: null, rn: 3 }]);
  });

  await store.fetchPage(); // page one: seeds a, b
  await store.fetchPage(); // page two: an all-duplicate page, then re-anchors onto c

  assert.equal(urls.length, 3, "an all-duplicate page must not end the turn");
  assert.ok(urls[2].includes("after_id=b"), "re-anchored on the response's last row");
  assert.ok(urls[2].includes("after_rn=2"));
  assert.ok(store.getItems().some((i) => i.id === "c"), "pagination advanced");
});

test("a 410 after a re-anchor walks back from the true held position, not a doubled one", async () => {
  // Regression for a bug where `back` (the walk-back stride) was advanced by
  // the for-loop's update expression on every continue, including the
  // re-anchor continue below. A re-anchor round is not a walk-back step; if
  // it inflates the stride anyway, a 410 right after it skips past anchors
  // that are still perfectly good — here, straight past b to a, instead of
  // retrying from b as it should.
  const urls = [];
  const held = [
    { id: "a", feed_id: "f", pub_date: null, rn: 1 },
    { id: "b", feed_id: "f", pub_date: null, rn: 2 },
    { id: "c", feed_id: "f", pub_date: null, rn: 3 },
  ];
  const store = makeStore((url) => {
    urls.push(url);
    if (urls.length === 1) return jsonResponse(held); // seed a, b, c
    if (urls.length === 2) return jsonResponse(held); // anchored on c: bound resolved lower, comes back as duplicates
    if (urls.length === 3) return goneResponse(); // re-anchored on c again: that row is gone too
    return jsonResponse([{ id: "d", feed_id: "f", pub_date: null, rn: 4 }]); // walk-back from b succeeds
  });

  await store.fetchPage(); // seeds a, b, c
  await store.fetchPage(); // duplicate page, then a 410, then the walk-back

  assert.equal(urls.length, 4);
  assert.ok(urls[3].includes("after_id=b"), `walk-back must retry from b, not skip past it: ${urls[3]}`);
  assert.ok(urls[3].includes("after_rn=2"));
  assert.ok(store.getItems().some((i) => i.id === "d"), "pagination recovered instead of stalling");
  assert.ok(store.hasMoreItems());
});

test("re-anchoring twice in a row still reaches fresh data", async () => {
  const urls = [];
  const held = [
    { id: "a", feed_id: "f", pub_date: null, rn: 1 },
    { id: "b", feed_id: "f", pub_date: null, rn: 2 },
  ];
  const store = makeStore((url) => {
    urls.push(url);
    if (urls.length === 1) return jsonResponse(held); // seed a, b
    if (urls.length === 2) return jsonResponse([held[0]]); // anchored on b: comes back holding only a
    if (urls.length === 3) return jsonResponse([held[1]]); // re-anchored on a: comes back holding only b
    return jsonResponse([{ id: "c", feed_id: "f", pub_date: null, rn: 3 }]); // re-anchored on b again: fresh at last
  });

  await store.fetchPage(); // seeds a, b
  await store.fetchPage(); // two duplicate rounds, then fresh

  assert.equal(urls.length, 4, "two re-anchor rounds plus the seed and the final fresh page");
  assert.ok(urls[2].includes("after_id=a") && urls[2].includes("after_rn=1"), "first re-anchor lands on a");
  assert.ok(urls[3].includes("after_id=b") && urls[3].includes("after_rn=2"), "second re-anchor lands on b");
  assert.ok(store.getItems().some((i) => i.id === "c"), "pagination reached fresh data after two re-anchors");
});
