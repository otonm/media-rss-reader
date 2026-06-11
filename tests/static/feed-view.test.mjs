// ---------------------------------------------------------------------------
// feed-view.test.mjs — tests for the listener-leak fix in onItemLoaded.
//
// Bug: feedView.onItemLoaded called MRR.autoscrollController.bindIfVisible
// for every newly loaded item, even lookahead items the user has not
// scrolled to. bindIfVisible is not a "bind if visible" check; it
// rebinds unconditionally and `unbind()`s the previous one. With autoscroll
// on, this meant the currently-playing video A lost its `ended` listener
// the moment lookahead video B finished loading — a "listener steal".
// Worse, the stolen listener attached to B could fire later when B
// became the visible item, regardless of the current autoscroll state.
//
// Fix: only call bindIfVisible when the newly loaded item IS the current
// item. Other modules (scroll-controller) already call bindIfVisible via
// reset() when the visible item changes.
// ---------------------------------------------------------------------------

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { createDomContext, loadScript } from "./dom-mock.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../src/static");

function makeItem(id, mediaType) {
  return { id, media_type: mediaType, media_url: `https://example/${id}` };
}

function setupHarness() {
  const ctx = createDomContext();
  const items = [
    makeItem("id0", "video"),
    makeItem("id1", "image"),
    makeItem("id2", "video"),
  ];
  // itemStore stub
  ctx.window.MRR.itemStore = {
    items,
    getItems: () => items,
    getCurrentIndex: () => 0,
    getItemAt: (i) => items[i],
    findIndexById: (id) => items.findIndex((it) => it.id === id),
    setCurrentIndex: () => {},
  };
  // config stub
  ctx.window.MRR.config = { autoscroll: true, mutedDefault: true };
  // autoscrollController stub — track bindIfVisible/unbind calls
  const ctrl = {
    bindCalls: [],
    bindIfVisible(wrap) { this.bindCalls.push(wrap.dataset.id); },
    reset() {},
    setAutoscroll() {},
  };
  ctx.window.MRR.autoscrollController = ctrl;
  // scrollController stub
  ctx.window.MRR.scrollController = { observe() {} };

  // Load the feed-view module FIRST so MRR.feedView is populated, then
  // build the real feed container and call renderInitial() to set
  // state.feed.
  loadScript(resolve(STATIC, "feed-view.js"), ctx);

  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  ctx.document.register(feed);
  ctx.window.MRR.feedView.renderInitial(items);

  return { ctx, ctrl, feed, items };
}

test("onItemLoaded for a non-current (lookahead) item does NOT call bindIfVisible", () => {
  const { ctx, ctrl } = setupHarness();
  const img = ctx.document.createElement("img");
  img.src = "https://example/id1.jpg";
  ctx.window.MRR.feedView.onItemLoaded("id1", img);
  assert.deepEqual(
    ctrl.bindCalls,
    [],
    "bindIfVisible must not be called for a lookahead (non-current) item",
  );
});

test("onItemLoaded for the CURRENT item DOES call bindIfVisible", () => {
  const { ctx, ctrl, feed } = setupHarness();
  // id0 is the current item; its placeholder is in the feed (created by
  // renderInitial). Simulate the cache loading the media for the current
  // item: feedView.onItemLoaded replaces the placeholder with a media wrap
  // and should call bindIfVisible.
  const video0 = ctx.document.createElement("video");
  ctrl.bindCalls.length = 0;
  ctx.window.MRR.feedView.onItemLoaded("id0", video0);
  assert.deepEqual(ctrl.bindCalls, ["id0"]);
});

// ---------------------------------------------------------------------------
// Listener-steal regression test using the real autoscrollController.
// Before the fix, onItemLoaded(lookahead) would unbind() the currently
// playing video's `ended` listener. After the fix, that listener must
// survive a lookahead load.
// ---------------------------------------------------------------------------
test("onItemLoaded for a lookahead item does NOT remove the currently-playing video's `ended` listener", () => {
  // Set up a real autoscrollController and the full feed-view module.
  const ctx = createDomContext();
  const items = [
    makeItem("id0", "video"),
    makeItem("id1", "image"),
  ];
  ctx.window.MRR.itemStore = {
    items,
    getItems: () => items,
    getCurrentIndex: () => 0,
    getItemAt: (i) => items[i],
    findIndexById: (id) => items.findIndex((it) => it.id === id),
    setCurrentIndex: () => {},
  };
  ctx.window.MRR.config = { autoscroll: true, mutedDefault: true, imageAutoscrollDelayMs: 2000 };
  // Load autoscroll-controller first so MRR.autoscrollController exists.
  loadScript(resolve(STATIC, "autoscroll-controller.js"), ctx);
  ctx.window.MRR.autoscrollController.setAutoscroll(true);
  // scrollController stub.
  ctx.window.MRR.scrollController = { observe() {} };
  // Now load feed-view.
  loadScript(resolve(STATIC, "feed-view.js"), ctx);

  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  ctx.document.register(feed);
  ctx.window.MRR.feedView.renderInitial(items);

  // Manually mount the current item (id0) as a real <video> in a media-item
  // wrap, simulating a previous onItemLoaded call.
  const wrap0 = ctx.document.createElement("div");
  wrap0.className = "media-item";
  wrap0.dataset.id = "id0";
  wrap0.dataset.mediaType = "video";
  const video0 = ctx.document.createElement("video");
  wrap0.appendChild(video0);
  // Replace id0's placeholder with wrap0.
  const ph0 = feed.children.find((c) => c.dataset.id === "id0");
  ph0.replaceWith(wrap0);
  // Now bind id0 as the visible (autoscroll attaches its `ended` listener).
  ctx.window.MRR.autoscrollController.bindIfVisible(wrap0);
  // Sanity: id0's video has an `ended` listener attached.
  assert.equal(video0._listeners.get("ended")?.length, 1, "id0 should have an `ended` listener after binding");

  // Now load id1 (lookahead, image). With the bug, this would call
  // bindIfVisible(id1-wrap), which would unbind() id0 (removing its
  // `ended` listener). With the fix, it should not.
  const img1 = ctx.document.createElement("img");
  ctx.window.MRR.feedView.onItemLoaded("id1", img1);
  assert.equal(
    video0._listeners.get("ended")?.length,
    1,
    "id0's `ended` listener must survive a lookahead load",
  );
});

// ---------------------------------------------------------------------------
// Seen-badge tests: createMediaWrap tags wraps with `seen` class + .seen-badge
// when item.seen_at is non-null; markSeen(id) does the same live.
// ---------------------------------------------------------------------------

test("onItemLoaded for an item with seen_at set tags the wrap with the seen class and badge", () => {
  const { ctx, feed, items } = setupHarness();
  // Make id0 a seen item (e.g. the user had previously scrolled past it
  // and the DB still has the row).
  items[0].seen_at = "2026-06-10T09:00:00";
  const video0 = ctx.document.createElement("video");
  ctx.window.MRR.feedView.onItemLoaded("id0", video0);
  const wrap = feed.children.find((c) => c.dataset.id === "id0");
  assert.ok(wrap.className.includes("seen"), `wrap should have 'seen' class, got: ${wrap.className}`);
  const badge = wrap.querySelector(".seen-badge");
  assert.ok(badge, "wrap should contain a .seen-badge child");
});

test("onItemLoaded for an unseen item does NOT add the seen class or badge", () => {
  const { ctx, feed, items } = setupHarness();
  items[0].seen_at = null;
  const video0 = ctx.document.createElement("video");
  ctx.window.MRR.feedView.onItemLoaded("id0", video0);
  const wrap = feed.children.find((c) => c.dataset.id === "id0");
  assert.ok(!wrap.className.includes("seen"), `wrap should NOT have 'seen' class, got: ${wrap.className}`);
  assert.equal(wrap.querySelector(".seen-badge"), null, "wrap should NOT contain a .seen-badge child");
});

test("markSeen(id) tags the wrap live, idempotently", () => {
  const { ctx, feed, items } = setupHarness();
  items[0].seen_at = null;
  const video0 = ctx.document.createElement("video");
  ctx.window.MRR.feedView.onItemLoaded("id0", video0);
  const wrap = feed.children.find((c) => c.dataset.id === "id0");
  assert.ok(!wrap.className.includes("seen"));

  // User scrolls past id0; the scroll-controller will call markSeen.
  ctx.window.MRR.feedView.markSeen("id0");
  assert.ok(wrap.className.includes("seen"), "wrap should have 'seen' class after markSeen");
  assert.ok(wrap.querySelector(".seen-badge"), "wrap should have a .seen-badge after markSeen");

  // Idempotent: calling again doesn't add a second badge.
  const badgesBefore = wrap.children.filter((c) => c.className === "seen-badge").length;
  ctx.window.MRR.feedView.markSeen("id0");
  const badgesAfter = wrap.children.filter((c) => c.className === "seen-badge").length;
  assert.equal(badgesAfter, badgesBefore, "markSeen must be idempotent");
});

test("markSeen(id) is a no-op for an id not in the DOM", () => {
  const { ctx } = setupHarness();
  // No throw, no error.
  ctx.window.MRR.feedView.markSeen("does-not-exist");
});

// ---------------------------------------------------------------------------
// Video controls + userInteracted tests.
// ---------------------------------------------------------------------------

test("video wraps have the `controls` attribute set", () => {
  const { ctx, feed, items } = setupHarness();
  const video = ctx.document.createElement("video");
  ctx.window.MRR.feedView.onItemLoaded("id0", video);
  const wrap = feed.children.find((c) => c.dataset.id === "id0");
  const v = wrap.querySelector("video");
  assert.equal(v.getAttribute("controls"), "", "video must have the `controls` attribute set");
});

test("image wraps do NOT have any video-specific attributes leak onto the <img>", () => {
  const { ctx, feed } = setupHarness();
  const img = ctx.document.createElement("img");
  ctx.window.MRR.feedView.onItemLoaded("id1", img);
  const wrap = feed.children.find((c) => c.dataset.id === "id1");
  // Make sure no stray video controls attr ends up on a non-video wrap.
  assert.equal(wrap.querySelector("video"), null, "image wrap should not contain a <video>");
});

test("a `pause` event WITHOUT a preceding JS pause marks the video as userPaused", () => {
  const { ctx, feed } = setupHarness();
  const video = ctx.document.createElement("video");
  ctx.window.MRR.feedView.onItemLoaded("id0", video);
  const wrap = feed.children.find((c) => c.dataset.id === "id0");
  const v = wrap.querySelector("video");
  // Simulate the user clicking the browser's pause button: a `pause` event
  // is dispatched without the JS having set _pausedByJs first.
  v.pause();
  v.dispatchEvent({ type: "pause" });
  assert.equal(v.userPaused, true, "user pause should set userPaused = true");
});

test("a `pause` event from a JS-initiated pause does NOT mark the video as userPaused", () => {
  const { ctx, feed } = setupHarness();
  const video = ctx.document.createElement("video");
  ctx.window.MRR.feedView.onItemLoaded("id0", video);
  const wrap = feed.children.find((c) => c.dataset.id === "id0");
  const v = wrap.querySelector("video");
  // Simulate what setCurrentMedia does: set the flag, then pause, then the
  // browser fires the event.
  v._pausedByJs = true;
  v.pause();
  v.dispatchEvent({ type: "pause" });
  assert.notEqual(v.userPaused, true, "JS pause must not set userPaused");
  assert.equal(v._pausedByJs, false, "_pausedByJs flag must be cleared by the pause handler");
});

test("a `play` event clears userPaused (so manual play survives scroll-back)", () => {
  const { ctx, feed } = setupHarness();
  const video = ctx.document.createElement("video");
  ctx.window.MRR.feedView.onItemLoaded("id0", video);
  const wrap = feed.children.find((c) => c.dataset.id === "id0");
  const v = wrap.querySelector("video");
  v.userPaused = true;
  v.dispatchEvent({ type: "play" });
  assert.equal(v.userPaused, false, "play must clear userPaused");
});

test("a `volumechange` event does NOT mark the video as userPaused", () => {
  // Regression: prior to the fix, volumechange on the old video (caused by
  // setCurrentMedia writing `muted = true` to it) was treated as user
  // interaction and suppressed autoplay. With the fix, volumechange is
  // not tracked at all.
  const { ctx, feed } = setupHarness();
  const video = ctx.document.createElement("video");
  ctx.window.MRR.feedView.onItemLoaded("id0", video);
  const wrap = feed.children.find((c) => c.dataset.id === "id0");
  const v = wrap.querySelector("video");
  v.dispatchEvent({ type: "volumechange" });
  assert.notEqual(v.userPaused, true, "volumechange must not set userPaused");
});

test("setCurrentMedia does not auto-play a video the user has paused", async () => {
  const { ctx, feed, items } = setupHarness();
  const video = ctx.document.createElement("video");
  // Pre-mark the video as user-paused.
  video.userPaused = true;
  let playCalls = 0;
  video.play = () => { playCalls += 1; return Promise.resolve(); };
  ctx.window.MRR.feedView.onItemLoaded("id0", video);
  const wrap = feed.children.find((c) => c.dataset.id === "id0");
  const v = wrap.querySelector("video");
  v.userPaused = true;
  ctx.window.MRR.feedView.setCurrentMedia(v);
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(playCalls, 0, "userPaused video must not be auto-played");
});

test("scroll-away and scroll-back replays a video even if a volumechange fired in between", async () => {
  // Regression: prior to the fix, setCurrentMedia wrote `muted = true` on
  // the OLD video when transitioning away. That fired `volumechange` and
  // (under the old userInteracted flag) suppressed autoplay on scroll-back.
  // With the fix, the OLD video's mute state is not touched, volumechange
  // is not tracked, and the video plays again on the next visible transition.
  const { ctx, feed, items } = setupHarness();
  const video = ctx.document.createElement("video");
  let playCalls = 0;
  video.play = () => { playCalls += 1; return Promise.resolve(); };
  ctx.window.MRR.feedView.onItemLoaded("id0", video);
  const v = feed.querySelector("video");
  assert.equal(v, video);

  // Initial bind: setCurrentMedia(v) calls play() once.
  ctx.window.MRR.feedView.setCurrentMedia(v);
  assert.equal(playCalls, 1);

  // Transition AWAY to a different video (a sibling). setCurrentMedia must
  // NOT touch the old video's muted property.
  const otherVideo = ctx.document.createElement("video");
  otherVideo.play = () => { playCalls += 1; return Promise.resolve(); };
  ctx.window.MRR.feedView.onItemLoaded("id2", otherVideo);
  const v2 = feed.querySelectorAll("video")[1];
  ctx.window.MRR.feedView.setCurrentMedia(v2);
  assert.equal(playCalls, 2);

  // v's muted must be exactly what createMediaWrap set it to (default true).
  // No additional volumechange-causing write may have happened.
  assert.equal(v.muted, true, "old video's muted must be left at the createMediaWrap value");

  // Transition BACK. setCurrentMedia(v) must call play() again on v.
  ctx.window.MRR.feedView.setCurrentMedia(v);
  assert.equal(playCalls, 3, "play() must be called again on scroll-back");
});

test("setCurrentMedia logs a warning when play() rejects", async () => {
  const { ctx, feed, items } = setupHarness();
  const originalWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args);
  try {
    const video = ctx.document.createElement("video");
    video.play = () => Promise.reject(new Error("autoplay blocked"));
    ctx.window.MRR.feedView.onItemLoaded("id0", video);
    const v = feed.querySelector("video");
    ctx.window.MRR.feedView.setCurrentMedia(v);
    await new Promise((r) => setTimeout(r, 20));
    assert.ok(
      warnings.some((w) => w[0] === "video play rejected"),
      `expected a "video play rejected" warning, got: ${JSON.stringify(warnings)}`,
    );
  } finally {
    console.warn = originalWarn;
  }
});
