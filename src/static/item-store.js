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
    // Bumped by resetForReload. A fetch that started under an older generation
    // belongs to a feed the user has already navigated away from, so it must
    // not write into the new one — see resetForReload.
    generation: 0,
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
    const generation = state.generation;
    try {
      const cfg = MRR.config;
      const paginating = state.items.length > 0;
      // Set when a page comes back as pure duplicates (see below): the anchor
      // for the next request, overriding cursorItem(back) until it either 410s
      // or a page appends something.
      let reanchor = null;
      // A correct server converges in one or two re-anchor rounds: each
      // round's tail is the page's max (rn, feed_id, id) tuple — the same
      // ordering the keyset predicate compares against — so the next request
      // strictly advances. This caps a server that doesn't hold that
      // invariant, so a misbehaving one can't hot-loop fetch with no escape.
      let reanchorAttempts = 0;
      const MAX_REANCHOR_ATTEMPTS = 5;
      // back only advances on a 410 (an anchor is confirmed gone). A
      // re-anchor round is not a walk-back step — advancing back there would
      // inflate the stride the walk-back uses once it takes over, stepping
      // past anchors that are still perfectly good.
      let back = 0;
      for (;;) {
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
        // A reload landed while this request was in flight. Its page belongs to
        // the feed the user has already left; writing it would merge two pages
        // into one store and hand renderInitial an item the top-up loop had
        // already put on screen.
        if (generation !== state.generation) return;
        if (resp.status === 410) {
          if (!paginating) break; // page one cannot 410; nothing left to step back to
          reanchor = null; // that anchor is gone too; fall back to walking held items
          back = back === 0 ? 1 : back * 2;
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
          if (++reanchorAttempts > MAX_REANCHOR_ATTEMPTS) break; // not converging; stop rather than hot-loop
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
      console.warn("itemStore: cursor exhausted (dead anchors or non-converging duplicates), stopping pagination");
    } finally {
      // Only the current generation owns the flag; a superseded fetch clearing
      // it would green-light a second concurrent request into the new feed.
      if (generation === state.generation) state.fetching = false;
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

  // Report media the browser could not load. The server marks the URL dead and,
  // once every URL of the item is dead, deletes the row and tombstones its guid
  // so no future feed poll re-inserts it.
  //
  // Must run BEFORE feedView.onItemFailed splices the entry — media_url only
  // exists on the store item, and there is nowhere else to recover it from.
  //
  // sendBeacon, like the seen mark: fire-and-forget, and queued by the browser
  // even if the tab is closing.
  function reportUnusable(id) {
    const item = state.items.find((i) => i.id === id);
    if (!item || !item.media_url) return;
    const q = `url=${encodeURIComponent(item.media_url)}&item_id=${encodeURIComponent(id)}`;
    navigator.sendBeacon(`/api/media/failed?${q}`);
  }

  // Called by app.reloadFeed() when the show-seen toggle is flipped or
  // any other reason to refetch from the start. Clearing the item list is
  // enough to reset the cursor, which is derived from it.
  function resetForReload() {
    state.items = [];
    state.currentIndex = 0;
    state.hasMore = true;
    // Clearing `fetching` on its own used to let reloadFeed start a second
    // fetchPage beside one still in flight. Both wrote state.items, so the
    // store ended up holding two different pages and every item the top-up
    // loop had already rendered got a second node from renderInitial. The
    // generation bump is what makes clearing the flag safe: the in-flight
    // fetch now discards its own result instead of merging into the new feed.
    state.fetching = false;
    state.generation += 1;
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
    reportUnusable,
    resetForReload,
    fetchPage,
  };
})();
