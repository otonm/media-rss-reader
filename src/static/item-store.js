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

  // The cursor is the id of an item we hold, plus the rank (rn) the server
  // issued it with. rn is ROW_NUMBER recomputed per request, so on its own
  // it is not a valid cursor: a pruned row beneath it shifts it down, and a
  // row inserted with an older pub_date shifts it up. Sending both lets the
  // server bound the next page at min(after_rn, the anchor's resolved rank)
  // — a shift in either direction turns into duplicates instead of a silent
  // skip (R3), and the known-set guard below drops the duplicates. pub_date
  // is not sent: an undated item serialised as the string "null", which the
  // server compared as text against real dates.
  //
  // `back` steps the anchor towards items we received earlier. A 410 means that
  // anchor row is gone — pruned, or its feed left the OPML — and an earlier
  // item is the next best anchor. Reloading from page one instead would clear
  // state.items and drop the user back to the top of the scroll.
  //
  // The step doubles rather than walking one at a time. A feed leaving the OPML
  // cascades its whole item set, so the run of dead anchors is not small; a
  // fixed cap stopped pagination for good once the run outgrew it, and walking
  // one by one costs a request per dead row. Doubling finds a surviving anchor
  // in log(n) requests whenever one exists at all.
  function cursorItem(back) {
    const idx = state.items.length - 1 - back;
    return idx >= 0 ? state.items[idx] : null;
  }

  async function fetchPage() {
    if (state.fetching || !state.hasMore) return;
    state.fetching = true;
    try {
      const cfg = MRR.config;
      const paginating = state.items.length > 0;
      // Set when a page comes back as pure duplicates (see below): the anchor
      // for the next request, overriding cursorItem(back) until it either 410s
      // or a page appends something.
      let reanchor = null;
      for (let back = 0; ; back = back === 0 ? 1 : back * 2) {
        const anchor = reanchor !== null ? reanchor : paginating ? cursorItem(back) : null;
        if (paginating && anchor === null) break; // walked past the oldest item we hold
        let url = `/api/items?unseen=${unseenParam()}&size=${cfg.feedInitialCount}`;
        if (anchor !== null) {
          url += `&after_id=${encodeURIComponent(anchor.id)}`;
          // The rank the server issued with this item. rn is recomputed per
          // request, so without it a feed gaining an older row moves the
          // window past items we never received.
          if (anchor.rn !== undefined) url += `&after_rn=${anchor.rn}`;
        }
        const resp = await fetch(url);
        if (resp.status === 410) {
          if (!paginating) break; // page one cannot 410; nothing left to step back to
          reanchor = null; // that anchor is gone too; fall back to walking held items
          continue; // anchor gone, step further back
        }
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
        const fresh = newItems.filter((i) => !known.has(i.id));
        if (fresh.length === 0) {
          // Every row was one we already hold: the bound resolved beneath our
          // position and the page came back as duplicates. Re-anchor on the
          // response's own last row so the next request moves past them,
          // rather than re-sending the anchor that produced this page and
          // getting the same page forever.
          const tail = newItems[newItems.length - 1];
          reanchor = { id: tail.id, rn: tail.rn };
          continue;
        }
        state.items = state.items.concat(fresh);
        return;
      }
      state.hasMore = false;
      console.warn("itemStore: every cursor anchor is gone (410), stopping pagination");
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
