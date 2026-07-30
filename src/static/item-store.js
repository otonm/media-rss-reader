// ---------------------------------------------------------------------------
// itemStore
//
// Owns the list of items. Pulls metadata from /api/items (paginated) and
// /api/items/count. Exposes a small event-emitter API:
//   on('items-appended', cb)
//   on('currentindex-changed', cb)
//   getItems(), getCurrentIndex(), getTotal(), hasMoreItems(), getItemAt(idx),
//   findIndexById(id), setCurrentIndex(idx),
//   fetchPage(), fetchCount()
//
// No build step. Module attaches to window.MRR.itemStore.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  // ponytail: debug trace for /api/items calls. Strip when no longer needed.
  function dbg(...args) { console.debug("[item-store]", ...args); }

  const state = {
    items: [],
    currentIndex: 0,
    total: 0,
    page: 0,
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

  async function fetchPage() {
    if (state.fetching || !state.hasMore) return;
    state.fetching = true;
    try {
      const cfg = MRR.config;
      const url = `/api/items?unseen=${unseenParam()}&page=${state.page}&size=${cfg.feedInitialCount}`;
      dbg("fetchPage unseen=" + unseenParam() + " page=" + state.page
          + " size=" + cfg.feedInitialCount);
      const resp = await fetch(url);
      if (!resp.ok) return;
      const newItems = await resp.json();
      if (!newItems.length) {
        state.hasMore = false;
        return;
      }
      state.items = state.items.concat(newItems);
      dbg("fetchPage got " + newItems.length + " items, total=" + state.items.length
          + " hasMore=" + state.hasMore);
      state.page += 1;
    } finally {
      state.fetching = false;
    }
  }

  async function fetchCount() {
    const resp = await fetch(`/api/items/count?unseen=${unseenParam()}`);
    if (!resp.ok) return 0;
    const data = await resp.json();
    state.total = data.count;
    dbg("fetchCount unseen=" + unseenParam() + " -> " + state.total);
    return state.total;
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

  // Called by scroll-controller after a successful POST /api/items/{id}/seen.
  // Updates the in-memory item so the next render (or live markSeen) reflects
  // the new state. No-op if the item isn't loaded.
  function markSeen(id, seenAt) {
    const it = state.items.find((i) => i.id === id);
    if (it) it.seen_at = seenAt;
  }

  // Called by app.reloadFeed() when the show-seen toggle is flipped or
  // any other reason to refetch from page 0. Clears the in-memory list
  // and resets the page counter; the next fetchPage will start from
  // page 0 with the current showSeen setting.
  function resetForReload() {
    state.items = [];
    state.currentIndex = 0;
    state.total = 0;
    state.page = 0;
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
    markSeen,
    resetForReload,
    fetchPage,
    fetchCount,
  };
})();
