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

  function openStatusModal() {
    statusModal.setAttribute("aria-hidden", "false");
    statusModal.classList.add("open");
    statusBody.innerHTML =
      '<div class="status-loading"><div class="spinner"></div></div>';
    fetch("/api/reddit-feeds/status")
      .then((r) => r.text().then((t) => ({ ok: r.ok, body: t })))
      .then(({ ok, body }) => {
        if (!ok) {
          const msg = (tryParse(body) || {}).detail || "Reddit Feeds API error";
          statusBody.innerHTML = '<div class="status-error">' + msg + "</div>";
          return;
        }
        const data = tryParse(body);
        if (data && data.feeds != null) {
          renderStatus(data);
        } else {
          statusBody.innerHTML = '<div class="status-error">Unexpected response format</div>';
        }
      })
      .catch(() => {
        statusBody.innerHTML =
          '<div class="status-error">Failed to reach status endpoint</div>';
      });
    requestAnimationFrame(() => collapseControls());
  }

  function tryParse(text) {
    try { return JSON.parse(text); } catch (e) { return null; }
  }

  function renderStatus(data) {
    const feeds = data.feeds || [];
    if (feeds.length === 0) {
      statusBody.innerHTML =
        '<div class="status-empty">No feed data yet — first run hasn\'t completed</div>';
      return;
    }
    const rows = feeds
      .map(
        (f) =>
          "<tr>" +
          "<td>" + f.name + "</td>" +
          "<td><span class='status-dot " + f.last_status + "'></span>" + f.last_status + "</td>" +
          "<td>" + (f.last_fetch ? new Date(f.last_fetch).toLocaleString() : "—") + "</td>" +
          "<td>" + (f.last_item_count != null ? f.last_item_count : "—") + "</td>" +
          "<td>" + (f.total_items != null ? f.total_items : "—") + "</td>" +
          "</tr>"
      )
      .join("");
    statusBody.innerHTML =
      '<table class="status-table">' +
      "<thead><tr>" +
      "<th>Feed</th><th>Status</th><th>Last Fetch</th><th>Last Count</th><th>Total</th>" +
      "</tr></thead>" +
      "<tbody>" + rows + "</tbody>" +
      "</table>" +
      (data.last_run
        ? '<div class="status-footer">Last run: ' + new Date(data.last_run).toLocaleString() + "</div>"
        : "");
  }

  function closeStatusModal() {
    statusModal.classList.remove("open");
    statusModal.setAttribute("aria-hidden", "true");
  }

  const state = {
    muted: true,
    showSeen: false,
  };

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
    const btnStatus = document.getElementById("btn-status");
    const diag = document.createElement("div");
    diag.id = "__diag_btn_status";
    diag.textContent = btnStatus ? "btn-status OK" : "btn-status NULL";
    Object.assign(diag.style, {
      position: "fixed", top: "10px", right: "10px", zIndex: "99999",
      background: btnStatus ? "green" : "red", color: "white",
      padding: "0.5rem", fontSize: "1rem", borderRadius: "4px"
    });
    document.body.appendChild(diag);
    try {
      btnStatus.addEventListener("click", () => {
        const indicator = document.createElement("div");
        indicator.textContent = "TAP " + Date.now();
        Object.assign(indicator.style, {
          position: "fixed", top: "50%", left: "50%", transform: "translate(-50%,-50%)",
          background: "red", color: "white", padding: "1rem", zIndex: "99999",
          fontSize: "2rem", borderRadius: "8px", pointerEvents: "none"
        });
        document.body.appendChild(indicator);
        setTimeout(() => indicator.remove(), 2000);
        if (statusModal.classList.contains("open")) {
          closeStatusModal();
        } else {
          openStatusModal();
        }
      });
    } catch (e) {
      diag.textContent = "btn-status err: " + e.message;
      diag.style.background = "orange";
    }
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
  }

  MRR.controls = { init, setMuted, setAutoscroll, setShowSeen };
})();
