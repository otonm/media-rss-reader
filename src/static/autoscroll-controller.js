// ---------------------------------------------------------------------------
// autoscrollController
//
// When autoscroll is on, the visible item's media drives a snap-to-next:
//
//   - image:  setTimeout(IMAGE_AUTOSCROLL_DELAY_S)
//   - gif:    setTimeout(getGifDuration(src))
//   - video:  addEventListener('ended', ...) once; then snapToNext
//
// When the current item changes (scrollController fires), the timer is
// reset for the new visible item.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});

  const state = {
    autoscroll: false,
    boundItem: null,
    boundType: null,
    timerId: null,
    videoEndedHandler: null,
    onAutoscrollChanged: null,
  };

  function setAutoscroll(on) {
    state.autoscroll = on;
    document.querySelectorAll("#feed video").forEach((v) => { v.loop = !on; });
    if (on) {
      const current = currentVisibleWrap();
      if (current) bindIfVisible(current);
    } else {
      unbind();
    }
  }

  function currentVisibleWrap() {
    const idx = MRR.itemStore.getCurrentIndex();
    const items = document.querySelectorAll("#feed .media-item");
    return items[idx] || null;
  }

  function bindIfVisible(wrap) {
    if (!state.autoscroll) return;
    if (state.boundItem === wrap) return;
    unbind();
    state.boundItem = wrap;
    const type = wrap.dataset.mediaType;
    state.boundType = type;
    if (type === "video") {
      const v = wrap.querySelector("video");
      if (v) {
        state.videoEndedHandler = () => {
          if (state.boundItem === wrap) MRR.feedView.snapToNext();
        };
        v.addEventListener("ended", state.videoEndedHandler, { once: true });
      }
    } else if (type === "image") {
      const cfg = MRR.config;
      state.timerId = setTimeout(() => {
        if (state.boundItem === wrap) MRR.feedView.snapToNext();
      }, cfg.imageAutoscrollDelayMs);
    } else if (type === "gif") {
      getGifDuration(wrap.querySelector("img").src).then((ms) => {
        if (state.boundItem !== wrap) return;
        state.timerId = setTimeout(() => {
          if (state.boundItem === wrap) MRR.feedView.snapToNext();
        }, ms);
      });
    }
  }

  function reset(wrap) {
    if (state.autoscroll) bindIfVisible(wrap);
  }

  function unbind() {
    if (state.timerId !== null) { clearTimeout(state.timerId); state.timerId = null; }
    if (state.boundItem && state.boundType === "video" && state.videoEndedHandler) {
      const v = state.boundItem.querySelector("video");
      if (v) v.removeEventListener("ended", state.videoEndedHandler);
    }
    state.boundItem = null;
    state.boundType = null;
    state.videoEndedHandler = null;
  }

  function getGifDuration(url) {
    if (!url.startsWith("/api/media/proxy?")) return Promise.resolve(MRR.config.imageAutoscrollDelayMs);
    return fetch(url)
      .then((r) => r.arrayBuffer())
      .then((buf) => {
        const u = new Uint8Array(buf);
        let ms = 0;
        for (let i = 0; i + 5 < u.length; i++) {
          if (u[i] === 0x21 && u[i + 1] === 0xF9 && u[i + 2] === 0x04) {
            ms += (u[i + 4] + u[i + 5] * 256) * 10;
            i += 5;
          }
        }
        return ms > 0 ? Math.min(Math.max(ms, 50), 60000) : MRR.config.imageAutoscrollDelayMs;
      })
      .catch(() => MRR.config.imageAutoscrollDelayMs);
  }

  MRR.autoscrollController = { setAutoscroll, bindIfVisible, reset };
})();
