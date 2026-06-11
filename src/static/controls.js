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

  const state = {
    muted: true,
    showSeen: false,
  };

  function setMuted(muted) {
    state.muted = muted;
    MRR.config.mutedDefault = muted;
    document.querySelectorAll("#feed video").forEach((v) => { v.muted = muted; });
    const btn = document.getElementById("btn-mute");
    btn.setAttribute("aria-pressed", String(muted));
    btn.textContent = muted ? "🔇" : "🔊";
  }

  function setAutoscroll(on) {
    MRR.autoscrollController.setAutoscroll(on);
    MRR.config.autoscroll = on;
    const btn = document.getElementById("btn-autoscroll");
    btn.setAttribute("aria-pressed", String(on));
  }

  function setShowSeen(on) {
    state.showSeen = !!on;
    const btn = document.getElementById("btn-show-seen");
    btn.setAttribute("aria-pressed", String(on));
    MRR.app.setShowSeen(on);
  }

  function init() {
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
