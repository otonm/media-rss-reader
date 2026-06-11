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

  const state = {
    items: [],
    currentIndex: 0,
    total: 0,
    page: 0,
    hasMore: true,
    fetching: false,
  };

  const listeners = { "items-appended": [], "currentindex-changed": [] };

  function on(event, cb) {
    if (!listeners[event]) throw new Error("unknown event: " + event);
    listeners[event].push(cb);
  }
  function emit(event, ...args) {
    listeners[event].forEach((cb) => cb(...args));
  }

  async function fetchPage() {
    if (state.fetching || !state.hasMore) return;
    state.fetching = true;
    try {
      const cfg = MRR.config;
      const url = `/api/items?unseen=true&page=${state.page}&size=${cfg.feedInitialCount}`;
      const resp = await fetch(url);
      if (!resp.ok) return;
      const newItems = await resp.json();
      if (!newItems.length) {
        state.hasMore = false;
        return;
      }
      state.items = state.items.concat(newItems);
      state.page += 1;
      emit("items-appended", newItems);
    } finally {
      state.fetching = false;
    }
  }

  async function fetchCount() {
    const resp = await fetch("/api/items/count?unseen=true");
    if (!resp.ok) return 0;
    const data = await resp.json();
    state.total = data.count;
    return state.total;
  }

  function getItems() { return state.items; }
  function getCurrentIndex() { return state.currentIndex; }
  function getTotal() { return state.total; }
  function hasMoreItems() { return state.hasMore; }
  function getItemAt(idx) { return state.items[idx]; }
  function findIndexById(id) { return state.items.findIndex((i) => i.id === id); }

  function setCurrentIndex(idx) {
    if (idx === state.currentIndex) return;
    if (idx < 0 || idx >= state.items.length) return;
    state.currentIndex = idx;
    emit("currentindex-changed", idx);
  }

  MRR.itemStore = {
    on,
    getItems,
    getCurrentIndex,
    getTotal,
    hasMoreItems,
    getItemAt,
    findIndexById,
    setCurrentIndex,
    fetchPage,
    fetchCount,
  };
})();
