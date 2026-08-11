// ---------------------------------------------------------------------------
// controls
//
// Wires up the three control buttons (autoscroll, mute, show-seen).
//
// Mute is global: when toggled, every <video> in the feed gets el.muted
// set to the new value. The visible video continues to play (per the
// visible-media rule in feedView).
//
// Show-seen is a filter toggle: when on, the feed is refetched with
// ?unseen=false so previously-viewed items appear in the feed with a
// checkmark. Persisted to localStorage.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const statusModal = document.getElementById("status-modal");
  const statusBody = document.getElementById("status-modal-body");

  let pollHandle = null;

  function showSpinner() {
    statusBody.innerHTML =
      '<div class="status-loading"><div class="spinner"></div></div>';
  }

  function refreshStatus(initial) {
    return fetch("/api/reddit-feeds/status")
      .then((r) => r.text().then((t) => ({ ok: r.ok, body: t })))
      .then(({ ok, body }) => {
        if (!ok) {
          if (initial) {
            const msg = (tryParse(body) || {}).detail || "Reddit Feeds API error";
            statusBody.innerHTML = '<div class="status-error">' + msg + "</div>";
          }
          return;
        }
        const data = tryParse(body);
        if (data && data.feeds != null) {
          const next = document.createElement("div");
          renderInto(next, data);
          statusBody.replaceChildren(...Array.from(next.childNodes));
        } else if (initial) {
          statusBody.innerHTML = '<div class="status-error">Unexpected response format</div>';
        }
      })
      .catch(() => {
        if (initial) {
          statusBody.innerHTML =
            '<div class="status-error">Failed to reach status endpoint</div>';
        }
      });
  }

  function openStatusModal() {
    statusModal.setAttribute("aria-hidden", "false");
    statusModal.classList.add("open");
    showSpinner();
    refreshStatus(true);
    // 1 s polling. The Reddit Feeds upstream is a JSON file rewritten
    // atomically; SSE would only move this loop one hop back. Revisit if
    // the upstream starts pushing natively.
    pollHandle = setInterval(() => refreshStatus(false), 1000);
    collapseControls();
  }

  function tryParse(text) {
    try { return JSON.parse(text); } catch (e) { return null; }
  }

  function renderInto(container, data) {
    const feeds = data.feeds || [];
    if (feeds.length === 0) {
      const empty = document.createElement("div");
      empty.className = "status-empty";
      empty.textContent = "No feed data yet — first run hasn't completed";
      container.appendChild(empty);
      return;
    }
    const rows = feeds.map((f) => {
      const tr = document.createElement("tr");
      const dot = document.createElement("span");
      dot.className = "status-dot " + f.last_status;
      const statusCell = document.createElement("td");
      statusCell.appendChild(dot);
      statusCell.appendChild(document.createTextNode(f.last_status));
      tr.append(
        cell(f.name),
        statusCell,
        cell(f.last_fetch ? new Date(f.last_fetch).toLocaleString() : "\u2014"),
        cell(f.last_item_count != null ? f.last_item_count : "\u2014"),
        cell(f.total_items != null ? f.total_items : "\u2014"),
      );
      return tr;
    });
    const table = document.createElement("table");
    table.className = "status-table";
    const thead = document.createElement("thead");
    thead.appendChild(headerRow(["Feed", "Status", "Last Fetch", "Last Count", "Total"]));
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    rows.forEach((r) => tbody.appendChild(r));
    table.appendChild(tbody);
    container.appendChild(table);
    if (data.last_run) {
      const footer = document.createElement("div");
      footer.className = "status-footer";
      footer.textContent = "Last run: " + new Date(data.last_run).toLocaleString();
      container.appendChild(footer);
    }
  }

  function cell(text) {
    const td = document.createElement("td");
    td.textContent = text;
    return td;
  }

  function headerRow(cells) {
    const tr = document.createElement("tr");
    cells.forEach((t) => {
      const th = document.createElement("th");
      th.textContent = t;
      tr.appendChild(th);
    });
    return tr;
  }

  function closeStatusModal() {
    statusModal.classList.remove("open");
    statusModal.setAttribute("aria-hidden", "true");
    if (pollHandle !== null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
  }

  const state = {
    muted: true,
    showSeen: false,
  };

  // -------------------------------------------------------------------------
  // UI_DEBUG overlay
  //
  // Off unless UI_DEBUG=1. Names the item the feed is currently snapped to and
  // how it got there, which is what distinguishes "never cached" from "cached
  // but undecodable" from "stuck behind a stalled download".
  // -------------------------------------------------------------------------
  const debug = {
    el: null,
    feedTitles: {},   // feed_id -> title, fetched once
    loadMs: {},       // item id -> ms taken by the cache queue
  };

  function initDebugOverlay() {
    if (!MRR.config.uiDebug) return;
    debug.el = document.createElement("div");
    debug.el.id = "debug-overlay";
    document.body.appendChild(debug.el);
    // /api/items carries feed_id but not the feed's name; fetch the mapping once.
    fetch("/api/feeds")
      .then((r) => (r.ok ? r.json() : []))
      .then((feeds) => {
        feeds.forEach((f) => { debug.feedTitles[f.id] = f.title; });
        renderDebug();
      })
      .catch(() => {});
    renderDebug();
  }

  function recordLoadMs(id, ms) {
    if (MRR.config.uiDebug) debug.loadMs[id] = ms;
  }

  // "photo.jpg?width=900" -> "jpg". Empty when the URL carries no extension,
  // which is common for CDN asset IDs.
  function fileExt(url) {
    const path = String(url || "").split(/[?#]/)[0];
    const last = path.slice(path.lastIndexOf("/") + 1);
    const dot = last.lastIndexOf(".");
    return dot > 0 ? last.slice(dot + 1).toLowerCase() : "";
  }

  function row(label, value) {
    const line = document.createElement("div");
    line.className = "debug-row";
    const k = document.createElement("span");
    k.className = "debug-key";
    k.textContent = label;
    const v = document.createElement("span");
    v.className = "debug-val";
    v.textContent = value;
    line.appendChild(k);
    line.appendChild(v);
    return line;
  }

  function renderDebug() {
    if (!debug.el) return;
    const item = MRR.itemStore.getItemAt(MRR.itemStore.getCurrentIndex());
    debug.el.replaceChildren();
    if (!item) {
      debug.el.appendChild(row("item", "none"));
      return;
    }
    const ext = fileExt(item.media_url);
    const type = ext ? `${item.media_type} · ${ext}` : item.media_type;
    const count = Array.isArray(item.media) ? item.media.length : 1;
    const ms = debug.loadMs[item.id];
    const stats = MRR.cacheQueue.getStats();

    debug.el.appendChild(row("feed", debug.feedTitles[item.feed_id] || item.feed_id));
    debug.el.appendChild(row("title", item.title || "(untitled)"));
    debug.el.appendChild(row("type", count > 1 ? `${type} · ${count} slides` : type));
    debug.el.appendChild(row("pubdate", item.pub_date || "—"));
    debug.el.appendChild(
      row("cache", (item.cached ? "HIT" : "MISS") + (ms === undefined ? "" : ` · ${ms}ms`))
    );
    debug.el.appendChild(row("queue", `${stats.loading} loading · ${stats.queued} queued`));
  }

  function collapseControls() {
    document.getElementById("controls")?.classList.remove("expanded");
  }

  function setMuted(muted) {
    state.muted = muted;
    MRR.config.mutedDefault = muted;
    document.querySelectorAll("#feed video").forEach((v) => { v.muted = muted; });
    const btn = document.getElementById("btn-mute");
    btn.setAttribute("aria-pressed", String(muted));
    btn.textContent = muted ? "🔇" : "🔊";
    collapseControls();
  }

  function setAutoscroll(on) {
    MRR.autoscrollController.setAutoscroll(on);
    MRR.config.autoscroll = on;
    const btn = document.getElementById("btn-autoscroll");
    btn.setAttribute("aria-pressed", String(on));
    collapseControls();
  }

  function setShowSeen(on) {
    state.showSeen = !!on;
    const btn = document.getElementById("btn-show-seen");
    btn.setAttribute("aria-pressed", String(on));
    MRR.app.setShowSeen(on);
    collapseControls();
  }

  function init() {
    const fab = document.getElementById("btn-fab");
    if (fab) {
      fab.addEventListener("click", () => {
        document.getElementById("controls").classList.toggle("expanded");
      });
    }
    document.getElementById("btn-autoscroll").addEventListener("click", () => {
      const next = document.getElementById("btn-autoscroll").getAttribute("aria-pressed") !== "true";
      setAutoscroll(next);
    });
    document.getElementById("btn-mute").addEventListener("click", () => {
      setMuted(!state.muted);
    });
    document.getElementById("btn-show-seen").addEventListener("click", () => {
      const next = document.getElementById("btn-show-seen").getAttribute("aria-pressed") !== "true";
      setShowSeen(next);
    });
    document.getElementById("btn-status").addEventListener("click", () => {
      if (statusModal.classList.contains("open")) {
        closeStatusModal();
      } else {
        openStatusModal();
      }
    });
    document.getElementById("status-modal-close").addEventListener("click", closeStatusModal);
    statusModal.addEventListener("click", (e) => {
      if (e.target === statusModal) closeStatusModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && statusModal.classList.contains("open")) {
        closeStatusModal();
      }
    });
    setMuted(true);
    setAutoscroll(false);
    // The show-seen button starts in 'off' state. The actual filter
    // is read from localStorage in app.init() before the first fetch;
    // sync the button's aria-pressed to that value here so the UI
    // matches the on-disk preference after a reload.
    let stored = false;
    try { stored = localStorage.getItem("showSeen") === "1"; } catch (e) { /* ignore */ }
    setShowSeen(stored);
    initDebugOverlay();
  }

  MRR.controls = { init, initDebugOverlay, renderDebug, recordLoadMs };
})();
