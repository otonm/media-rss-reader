// ---------------------------------------------------------------------------
// controls
//
// Wires up the two control buttons (autoscroll, mute) and the counter.
//
// Mute is global: when toggled, every <video> in the feed gets el.muted
// set to the new value. The visible video continues to play (per the
// visible-media rule in feedView).
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    muted: true,
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

  function updateCounter() {
    const cur = MRR.itemStore.getCurrentIndex() + 1;
    const total = MRR.itemStore.getTotal();
    document.getElementById("counter").textContent = total > 0 ? `${cur} / ${total}` : "— / —";
  }

  function init() {
    document.getElementById("btn-autoscroll").addEventListener("click", () => {
      const next = document.getElementById("btn-autoscroll").getAttribute("aria-pressed") !== "true";
      setAutoscroll(next);
    });
    document.getElementById("btn-mute").addEventListener("click", () => {
      setMuted(!state.muted);
    });
    MRR.itemStore.on("items-appended", updateCounter);
    MRR.itemStore.on("currentindex-changed", updateCounter);
    setMuted(true);
    setAutoscroll(false);
    updateCounter();
  }

  MRR.controls = { init, setMuted, setAutoscroll, updateCounter };
})();
