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

// ---------------------------------------------------------------------------
// Visible-media rule across the whole feed.
//
// Each video is created with autoplay=true and starts as soon as it lands
// in the DOM. The OLD setCurrentMedia only paused the previous
// currentVisibleEl, so off-screen videos kept playing in the background.
// Unmuting (via the global mute toggle) would then leak audio from
// non-visible items. Fix: setCurrentMedia pauses every video in the feed.
// ---------------------------------------------------------------------------

test("setCurrentMedia pauses every video in the feed, not just the previous currentVisibleEl", () => {
  // Build a feed with three videos and per-video play/pause spies.
  const ctx = createDomContext();
  const items = [
    makeItem("id0", "video"),
    makeItem("id1", "video"),
    makeItem("id2", "video"),
  ];
  ctx.window.MRR.itemStore = {
    items,
    getItems: () => items,
    getCurrentIndex: () => 0,
    getItemAt: (i) => items[i],
    findIndexById: (id) => items.findIndex((it) => it.id === id),
    setCurrentIndex: () => {},
  };
  ctx.window.MRR.config = { autoscroll: false, mutedDefault: true };
  ctx.window.MRR.autoscrollController = {
    bindIfVisible() {},
    reset() {},
    setAutoscroll() {},
  };
  ctx.window.MRR.scrollController = { observe() {} };
  loadScript(resolve(STATIC, "feed-view.js"), ctx);

  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  ctx.document.register(feed);
  ctx.window.MRR.feedView.renderInitial(items);

  // Three videos with per-video pause spies.
  const pauses = { v0: 0, v1: 0, v2: 0 };
  const makes = (id) => {
    const v = ctx.document.createElement("video");
    v.paused = false;  // simulate the browser having started playback
    const origPause = v.pause.bind(v);
    v.pause = () => { pauses[id] += 1; return origPause(); };
    return v;
  };
  const v0 = makes("v0"); ctx.window.MRR.feedView.onItemLoaded("id0", v0);
  const v1 = makes("v1"); ctx.window.MRR.feedView.onItemLoaded("id1", v1);
  const v2 = makes("v2"); ctx.window.MRR.feedView.onItemLoaded("id2", v2);

  // Reset spy counts (onItemLoaded may have side-effects we don't care about).
  pauses.v0 = 0; pauses.v1 = 0; pauses.v2 = 0;

  // Transition to v1. v0 (the previous currentVisibleEl would be) AND
  // every other video in the feed must be paused, even though v0 is
  // already currentVisibleEl — actually no, on first transition, the
  // loop pauses all videos (including the one we're about to play).
  // That's correct: the new currentVisibleEl.play() will start it.
  ctx.window.MRR.feedView.setCurrentMedia(v1);
  assert.equal(pauses.v0, 1, "v0 must be paused on the first transition");
  assert.equal(pauses.v1, 1, "v1 must be paused-then-played on transition");
  assert.equal(pauses.v2, 1, "v2 must be paused even though it was never current");
});

test("setCurrentMedia on a non-current video pauses the old currentVisibleEl AND every other video in the feed", () => {
  // Same harness, but focus on the transition pattern: a → c, skipping b.
  const ctx = createDomContext();
  const items = [
    makeItem("id0", "video"),
    makeItem("id1", "video"),
    makeItem("id2", "video"),
  ];
  ctx.window.MRR.itemStore = {
    items,
    getItems: () => items,
    getCurrentIndex: () => 0,
    getItemAt: (i) => items[i],
    findIndexById: (id) => items.findIndex((it) => it.id === id),
    setCurrentIndex: () => {},
  };
  ctx.window.MRR.config = { autoscroll: false, mutedDefault: true };
  ctx.window.MRR.autoscrollController = {
    bindIfVisible() {},
    reset() {},
    setAutoscroll() {},
  };
  ctx.window.MRR.scrollController = { observe() {} };
  loadScript(resolve(STATIC, "feed-view.js"), ctx);

  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  ctx.document.register(feed);
  ctx.window.MRR.feedView.renderInitial(items);

  const pauses = { v0: 0, v1: 0, v2: 0 };
  const makes = (id) => {
    const v = ctx.document.createElement("video");
    v.paused = false;
    const origPause = v.pause.bind(v);
    v.pause = () => { pauses[id] += 1; return origPause(); };
    return v;
  };
  const v0 = makes("v0"); ctx.window.MRR.feedView.onItemLoaded("id0", v0);
  const v1 = makes("v1"); ctx.window.MRR.feedView.onItemLoaded("id1", v1);
  const v2 = makes("v2"); ctx.window.MRR.feedView.onItemLoaded("id2", v2);
  pauses.v0 = 0; pauses.v1 = 0; pauses.v2 = 0;

  // Set v0 as current first, then jump straight to v2 (skips v1).
  ctx.window.MRR.feedView.setCurrentMedia(v0);
  pauses.v0 = 0; pauses.v1 = 0; pauses.v2 = 0;

  ctx.window.MRR.feedView.setCurrentMedia(v2);
  assert.equal(pauses.v0, 1, "previous currentVisibleEl (v0) must be paused");
  assert.equal(pauses.v1, 1, "the skipped-over video (v1) must be paused too — no audio leak");
  assert.equal(pauses.v2, 1, "the new currentVisibleEl (v2) must be paused-then-played");
});

test("setCurrentMedia marks every paused video as JS-paused so the pause handler doesn't treat it as a user pause", () => {
  // Regression: the OLD code only set _pausedByJs on the previous
  // currentVisibleEl. The new code must set it on every paused video
  // because a user's prior unmute + autoplay would otherwise re-flag
  // every off-screen video as userPaused via the pause event.
  const ctx = createDomContext();
  const items = [
    makeItem("id0", "video"),
    makeItem("id1", "video"),
  ];
  ctx.window.MRR.itemStore = {
    items,
    getItems: () => items,
    getCurrentIndex: () => 0,
    getItemAt: (i) => items[i],
    findIndexById: (id) => items.findIndex((it) => it.id === id),
    setCurrentIndex: () => {},
  };
  ctx.window.MRR.config = { autoscroll: false, mutedDefault: true };
  ctx.window.MRR.autoscrollController = { bindIfVisible() {}, reset() {}, setAutoscroll() {} };
  ctx.window.MRR.scrollController = { observe() {} };
  loadScript(resolve(STATIC, "feed-view.js"), ctx);

  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  ctx.document.register(feed);
  ctx.window.MRR.feedView.renderInitial(items);

  const v0 = ctx.document.createElement("video");
  const v1 = ctx.document.createElement("video");
  ctx.window.MRR.feedView.onItemLoaded("id0", v0);
  ctx.window.MRR.feedView.onItemLoaded("id1", v1);

  ctx.window.MRR.feedView.setCurrentMedia(v1);
  assert.equal(v0._pausedByJs, true, "v0 must be marked JS-paused");
  assert.equal(v1._pausedByJs, true, "v1 must be marked JS-paused even though it's the new current");
});

// ---------------------------------------------------------------------------
// Regression: gallery rendering with ≥2 slides.
//
// Pre-fix: buildGallery referenced `item.id` outside its scope, throwing
// ReferenceError on the second slide. cache-queue's try/catch then emitted
// item-failed, removing the placeholder. The user saw an empty feed with no
// console error. This test fails before the fix in feed-view.js and passes
// after buildGallery receives itemId as a parameter.
// ---------------------------------------------------------------------------

test("onItemLoaded for a multi-slide gallery does NOT throw", () => {
  const ctx = createDomContext();
  const item = {
    id: "abc",
    media_type: "image",
    media_url: "https://example/1.jpg",
    media: [
      { url: "https://example/1.jpg", type: "image" },
      { url: "https://example/2.jpg", type: "image" },
      { url: "https://example/3.gif", type: "gif" },
    ],
    seen_at: null,
  };
  ctx.window.MRR.itemStore = {
    getItems: () => [item],
    getCurrentIndex: () => 0,
    getItemAt: () => item,
    findIndexById: () => 0,
    setCurrentIndex: () => {},
  };
  ctx.window.MRR.config = { autoscroll: false, mutedDefault: true };
  ctx.window.MRR.autoscrollController = { bindIfVisible() {}, reset() {}, setAutoscroll() {} };
  ctx.window.MRR.scrollController = { observe() {} };
  loadScript(resolve(STATIC, "feed-view.js"), ctx);
  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  ctx.document.register(feed);
  ctx.window.MRR.feedView.renderInitial([item]);

  const firstEl = ctx.document.createElement("img");
  // Must not throw.
  ctx.window.MRR.feedView.onItemLoaded("abc", firstEl);

  // Wrap must be in the feed with a gallery of three slides.
  const wrap = feed.children.find((c) => c.dataset.id === "abc");
  assert.ok(wrap, "wrap must replace the placeholder");
  const slides = wrap.querySelector(".gallery").children;
  assert.equal(slides.length, 3, "gallery must have one slide per media entry");
  // The non-first slide's <img> must carry the proxy URL with item_id.
  const nonFirst = slides[1].children[0];
  assert.match(nonFirst.src, /item_id=abc/, "slide >1 must include item_id in proxy URL");
});

// ---------------------------------------------------------------------------
// A failed item leaves the feed entirely.
//
// This inverts an earlier deliberate decision: onItemFailed used to leave a
// visible error tile so failures were not silent. But the tile had no store
// entry — the item was spliced out at the same time — and reaching it made
// onIntersect's findIndexById return -1 and bail before rebuilding the cache
// queue or re-arming autoscroll. Nothing after it loaded and autoscroll never
// fired again. The item is now reported to /api/media/failed, deleted server
// side and dropped from the feed, so the mismatch cannot exist.
// ---------------------------------------------------------------------------

function failedHarness() {
  const ctx = createDomContext();
  const items = [
    { id: "id0", media_type: "image", media_url: "https://example/0.jpg", title: "A broken picture" },
    { id: "id1", media_type: "image", media_url: "https://example/1.jpg", title: "Fine" },
  ];
  ctx.window.MRR.itemStore = {
    getItems: () => items,
    getCurrentIndex: () => 0,
    getItemAt: (i) => items[i],
    findIndexById: (id) => items.findIndex((i) => i.id === id),
    setCurrentIndex: () => {},
  };
  ctx.window.MRR.config = { autoscroll: false, mutedDefault: true };
  ctx.window.MRR.autoscrollController = { bindIfVisible() {}, reset() {}, setAutoscroll() {} };
  ctx.window.MRR.scrollController = { observe() {} };
  loadScript(resolve(STATIC, "feed-view.js"), ctx);
  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  ctx.document.register(feed);
  ctx.window.MRR.feedView.renderInitial(items);
  return { ctx, feed, items };
}

test("onItemFailed removes the node and the store entry together", () => {
  const { ctx, feed, items } = failedHarness();

  withSilencedWarn(() => ctx.window.MRR.feedView.onItemFailed("id0", "timed out after 10s"));

  assert.equal(feed.children.find((c) => c.dataset.id === "id0"), undefined, "the node must be gone");
  assert.deepEqual(
    items.map((i) => i.id),
    ["id1"],
    "an orphan node with no store entry is what wedged the feed",
  );
});

test("failing the currently-snapped item advances onto its neighbour", () => {
  const { ctx, feed } = failedHarness();
  const view = ctx.window.MRR.feedView;
  const doomed = feed.children[0];
  const survivor = feed.children[1];
  view.setCurrentEl(doomed);

  withSilencedWarn(() => view.onItemFailed("id0", "timed out after 10s"));

  assert.equal(survivor.scrolledIntoView, 1, "the feed closes up onto the next item");
  // snapToNext must now walk from the survivor, not from a detached node.
  view.setCurrentEl(survivor);
  assert.equal(feed.children.length, 1);
});

// ---------------------------------------------------------------------------
// Duplicate-render regression.
//
// Bug: the feed rendered items 1-10, then item 1 again (without its seen
// checkmark), then item 2 — the whole block a second time. Two paths appended
// to #feed with different guards: renderInitial had none at all, and app.js's
// pagination top-up filtered against a snapshot of feed.children taken before
// its loop. A reload racing an in-flight page could therefore paint a second
// node for an item already on screen.
//
// Fix: appendItem is the only door into #feed and checks the live DOM.
// ---------------------------------------------------------------------------

// The guard warns so the producer names itself in a real browser console;
// swallow it here so the suite output stays readable.
function withSilencedWarn(fn) {
  const original = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args);
  try {
    fn();
  } finally {
    console.warn = original;
  }
  return warnings;
}

function feedHarness(items) {
  const ctx = createDomContext();
  ctx.window.MRR.itemStore = {
    getItems: () => items,
    getCurrentIndex: () => 0,
    getItemAt: (i) => items[i],
    findIndexById: (id) => items.findIndex((it) => it.id === id),
    setCurrentIndex: () => {},
  };
  ctx.window.MRR.config = { autoscroll: false, mutedDefault: true };
  ctx.window.MRR.autoscrollController = { bindIfVisible() {}, reset() {}, setAutoscroll() {} };
  ctx.window.MRR.scrollController = { observe() {} };
  loadScript(resolve(STATIC, "feed-view.js"), ctx);
  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  ctx.document.register(feed);
  return { ctx, feed };
}

const renderedIds = (feed) => feed.children.map((el) => el.dataset.id);

test("appendItem refuses a second node for an id already on screen", () => {
  const items = [makeItem("id1", "image")];
  const { ctx, feed } = feedHarness(items);
  const view = ctx.window.MRR.feedView;

  let second;
  const warnings = withSilencedWarn(() => {
    view.appendItem(items[0]);
    second = view.appendItem(items[0]);
  });

  assert.equal(feed.children.length, 1, "one node per item");
  assert.equal(second, null, "the refused append returns null so the caller skips observe()");
  assert.equal(warnings.length, 1, "the refusal is reported so the producer is identifiable");
});

test("renderInitial over an already-populated feed does not duplicate", () => {
  // Exactly the reload-racing-an-in-flight-page shape: the top-up loop has
  // already rendered part of the store when renderInitial paints all of it.
  const items = ["id1", "id2", "id3", "id4", "id5"].map((id) => makeItem(id, "image"));
  const { ctx, feed } = feedHarness(items);
  const view = ctx.window.MRR.feedView;

  items.slice(0, 3).forEach((it) => view.appendItem(it));
  withSilencedWarn(() => view.renderInitial(items));

  assert.equal(feed.children.length, 5);
  assert.deepEqual(renderedIds(feed), ["id1", "id2", "id3", "id4", "id5"]);
});

test("re-running the pagination top-up over the same store appends nothing", () => {
  const items = ["id1", "id2"].map((id) => makeItem(id, "image"));
  const { ctx, feed } = feedHarness(items);
  const view = ctx.window.MRR.feedView;

  withSilencedWarn(() => {
    items.forEach((it) => view.appendItem(it));
    items.forEach((it) => view.appendItem(it));
  });

  assert.equal(feed.children.length, 2);
});

test("snapToNext follows the DOM even when the store index is stale", () => {
  // onItemFailed splices the store while leaving its error tile in the DOM, so
  // after any failed media load the store is shorter than the feed and a store
  // index no longer addresses the right node.
  const items = ["id1", "id2", "id3"].map((id) => makeItem(id, "image"));
  const { ctx, feed } = feedHarness(items);
  const view = ctx.window.MRR.feedView;
  items.forEach((it) => view.appendItem(it));

  // The store loses an entry; the DOM keeps all three nodes.
  items.splice(0, 1);
  view.setCurrentEl(feed.children[1]); // sitting on id2, store index 0 now

  view.snapToNext();

  assert.equal(feed.children[2].scrolledIntoView, 1, "must advance to the next ELEMENT (id3)");
  assert.ok(!feed.children[0].scrolledIntoView, "must not jump back to the first node");
});

test("snapToPrev walks back one element rather than one store index", () => {
  const items = ["id1", "id2", "id3"].map((id) => makeItem(id, "image"));
  const { ctx, feed } = feedHarness(items);
  const view = ctx.window.MRR.feedView;
  items.forEach((it) => view.appendItem(it));

  view.setCurrentEl(feed.children[2]);
  view.snapToPrev();

  assert.equal(feed.children[1].scrolledIntoView, 1);
});

// ---------------------------------------------------------------------------
// Regression: a gallery slide change drops the zoom.
//
// Zooming a slide to 100%, then stepping to the next slide with the on-screen
// arrow, left the zoom applied to the slide that scrolled out of view — coming
// back showed it still zoomed, while the ←/→ keys (which reset in app.js)
// zoomed out correctly. A slide change is a navigation like any other, so it
// resets, and onGalleryScroll is the one place every way of changing a slide
// passes through.
// ---------------------------------------------------------------------------

function galleryHarness() {
  const ctx = createDomContext();
  const item = {
    id: "abc",
    media_type: "image",
    media_url: "https://example/1.jpg",
    media: [
      { url: "https://example/1.jpg", type: "image" },
      { url: "https://example/2.jpg", type: "image" },
    ],
    seen_at: null,
  };
  ctx.window.MRR.itemStore = {
    getItems: () => [item],
    getCurrentIndex: () => 0,
    getItemAt: () => item,
    findIndexById: () => 0,
    setCurrentIndex: () => {},
  };
  ctx.window.MRR.config = { autoscroll: false, mutedDefault: true };
  ctx.window.MRR.autoscrollController = { bindIfVisible() {}, reset() {}, setAutoscroll() {} };
  ctx.window.MRR.scrollController = { observe() {} };
  const zoom = { resets: 0 };
  ctx.window.MRR.zoomController = { reset() { zoom.resets += 1; }, isZoomed: () => false };
  loadScript(resolve(STATIC, "feed-view.js"), ctx);
  const feed = ctx.document.createElement("div");
  feed.id = "feed";
  ctx.document.register(feed);
  ctx.window.MRR.feedView.renderInitial([item]);
  ctx.window.MRR.feedView.onItemLoaded("abc", ctx.document.createElement("img"));
  const wrap = feed.children.find((c) => c.dataset.id === "abc");
  const gallery = wrap.querySelector(".gallery");
  gallery.scrollLeft = 0;
  gallery.clientWidth = 1000;
  gallery.scrollTo = ({ left }) => { gallery.scrollLeft = left; };
  return { ctx, wrap, gallery, zoom };
}

test("the gallery's next arrow resets the zoom before it scrolls", () => {
  const { wrap, gallery, zoom } = galleryHarness();
  const nextBtn = wrap.children.find((c) => c.className === "gallery-nav next");

  nextBtn.dispatchEvent({ type: "click", currentTarget: nextBtn, stopPropagation() {} });

  assert.equal(zoom.resets, 1, "zoom must drop before the slide moves");
  assert.equal(gallery.scrollLeft, 1000, "and the gallery still advances");
});

test("a swipe that lands on another slide resets the zoom", async () => {
  const { wrap, gallery, zoom } = galleryHarness();

  // A swipe scrolls the gallery natively; only the debounced scroll handler
  // hears about it.
  gallery.scrollLeft = 1000;
  gallery.dispatchEvent({ type: "scroll" });
  await new Promise((r) => setTimeout(r, 100));
  assert.equal(zoom.resets, 1);
  assert.equal(wrap.querySelector(".gallery").children[1].classList.contains("active"), true);

  // Settling on the slide already active is not a navigation.
  gallery.dispatchEvent({ type: "scroll" });
  await new Promise((r) => setTimeout(r, 100));
  assert.equal(zoom.resets, 1, "no slide change, no reset");
});

// ---------------------------------------------------------------------------
// The dots track the scroll position live and jump to their slide when pressed.
// ---------------------------------------------------------------------------

const dotWeight = (dots, i) => Number(dots.children[i].style.getPropertyValue("--t"));

test("the dots follow a half-finished swipe instead of waiting for it to settle", () => {
  const { wrap, gallery } = galleryHarness();
  const dots = wrap.querySelector(".gallery-dots");

  assert.equal(dotWeight(dots, 0), 1, "the first dot starts lit");
  assert.equal(dotWeight(dots, 1), 0);

  // Mid-swipe: no debounce to wait out, both dots sit halfway.
  gallery.scrollLeft = 500;
  gallery.dispatchEvent({ type: "scroll" });
  assert.equal(dotWeight(dots, 0), 0.5);
  assert.equal(dotWeight(dots, 1), 0.5);

  gallery.scrollLeft = 1000;
  gallery.dispatchEvent({ type: "scroll" });
  assert.equal(dotWeight(dots, 0), 0);
  assert.equal(dotWeight(dots, 1), 1);
});

test("pressing a dot scrolls to its slide and drops the zoom", () => {
  const { wrap, gallery, zoom } = galleryHarness();
  const dots = wrap.querySelector(".gallery-dots");

  dots.dispatchEvent({ type: "click", target: dots.children[1], stopPropagation() {} });

  assert.equal(gallery.scrollLeft, 1000, "the gallery lands on the second slide");
  assert.equal(zoom.resets, 1, "and the zoom drops before it moves");
});
