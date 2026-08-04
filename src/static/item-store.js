// ---------------------------------------------------------------------------
// itemStore
//
// Owns the list of items. Pulls metadata from /api/items (paginated).
// Exposes a small API:
//   on('items-appended', cb)
//   on('currentindex-changed', cb)
//   getItems(), getCurrentIndex(), hasMoreItems(), getItemAt(idx),
//   findIndexById(id), setCurrentIndex(idx), fetchPage()
//
// No build step. Module attaches to window.MRR.itemStore.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    items: [],
    currentIndex: 0,
    hasMore: true,
    fetching: false,
    showSeen: false,
  };

  // When showSeen is true we ask the API for ALL items (unseen=false),
  // so seen items appear in the feed with their checkmark. Otherwise the
  // default is unseen-only.
  function unseenParam() {
    return state.showSeen ? "false" : "true";
  }

  // The cursor is the (feed_id, pub_date, id) of the last item we hold — three
  // immutable column values. rn is NOT sent: the server recomputes it per
  // request, so a pruned row beneath the cursor would shift it and we would
  // skip exactly as many items as were pruned (R3). The server derives the
  // anchor's rank from these three instead.
  function nextCursor() {
    if (state.items.length === 0) return null;
    const last = state.items[state.items.length - 1];
    return { feed_id: last.feed_id, pub_date: last.pub_date, id: last.id };
  }

  async function fetchPage() {
    if (state.fetching || !state.hasMore) return;
    state.fetching = true;
    try {
      const cfg = MRR.config;
      let url = `/api/items?unseen=${unseenParam()}&size=${cfg.feedInitialCount}`;
      const c = nextCursor();
      if (c) {
        url += `&after_feed_id=${encodeURIComponent(c.feed_id)}&after_pub_date=${encodeURIComponent(c.pub_date)}&after_id=${encodeURIComponent(c.id)}`;
      }
      const resp = await fetch(url);
      if (!resp.ok) return;
      const newItems = await resp.json();
      if (!newItems.length) {
        state.hasMore = false;
        return;
      }
      // Guard the append: a new feed appearing shifts the interleave and can
      // hand back an item we already hold, which would give findIndexById two
      // candidates and desync currentIndex from the DOM.
      const known = new Set(state.items.map((i) => i.id));
      state.items = state.items.concat(newItems.filter((i) => !known.has(i.id)));
    } finally {
      state.fetching = false;
    }
  }

  function getItems() { return state.items; }
  function getCurrentIndex() { return state.currentIndex; }
  function hasMoreItems() { return state.hasMore; }
  function getItemAt(idx) { return state.items[idx]; }
  function findIndexById(id) { return state.items.findIndex((i) => i.id === id); }

  function setCurrentIndex(idx) {
    if (idx === state.currentIndex) return;
    if (idx < 0 || idx >= state.items.length) return;
    state.currentIndex = idx;
  }

  function setShowSeen(on) {
    state.showSeen = !!on;
  }

  function getShowSeen() { return state.showSeen; }

  // Called by scroll-controller after a successful POST /api/items/{id}/seen.
  // Updates the in-memory item so the next render (or live markSeen) reflects
  // the new state. No-op if the item isn't loaded.
  function markSeen(id, seenAt) {
    const it = state.items.find((i) => i.id === id);
    if (it) it.seen_at = seenAt;
  }

  // Called by app.reloadFeed() when the show-seen toggle is flipped or
  // any other reason to refetch from the start. Clearing the item list is
  // enough to reset the cursor, which is derived from it.
  function resetForReload() {
    state.items = [];
    state.currentIndex = 0;
    state.hasMore = true;
    state.fetching = false;
  }

  MRR.itemStore = {
    getItems,
    getCurrentIndex,
    hasMoreItems,
    getItemAt,
    findIndexById,
    setCurrentIndex,
    setShowSeen,
    getShowSeen,
    markSeen,
    resetForReload,
    fetchPage,
  };
})();
