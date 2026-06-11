// ---------------------------------------------------------------------------
// autoscrollController
//
// When autoscroll is on, the visible item's media drives a snap-to-next:
//
//   - image:  setTimeout(IMAGE_AUTOSCROLL_DELAY_S)
//   - gif:    swap <img> for a <canvas> showing the first frame, then
//             setTimeout(getGifDuration(src)) for snap-to-next. The canvas
//             gives a "play once" feel — the GIF stops visually looping
//             and the user advances after the autoscroll window.
//   - video:  addEventListener('ended', ...) once; then snapToNext
//
// When the current item changes (scrollController fires), the timer is
// reset for the new visible item.
//
// When autoscroll is turned OFF, any swapped GIF <canvas> is replaced with
// a fresh <img> so the GIF resumes looping naturally.
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
    swappedGifWraps: new Set(),  // wraps currently showing a <canvas> instead of <img>
  };

  function setAutoscroll(on) {
    state.autoscroll = on;
    document.querySelectorAll("#feed video").forEach((v) => { v.loop = !on; });
    if (on) {
      const current = currentVisibleWrap();
      if (current) bindIfVisible(current);
    } else {
      unbind();
      restoreAllSwappedGifs();
    }
  }

  function currentVisibleWrap() {
    const item = MRR.itemStore.getItemAt(MRR.itemStore.getCurrentIndex());
    return item ? document.querySelector(`#feed .media-item[data-id="${item.id}"]`) : null;
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
      swapGifToCanvas(wrap);
      getGifDuration(wrap.querySelector("img,canvas").src).then((ms) => {
        if (state.boundItem !== wrap) return;
        state.timerId = setTimeout(() => {
          if (state.boundItem === wrap) MRR.feedView.snapToNext();
        }, ms);
      });
    }
  }

  // Replace the looping <img> of a GIF wrap with a <canvas> showing the
  // first frame. The original src is preserved on the wrap as
  // `_gifOriginalSrc` so we can restore the <img> when autoscroll turns off.
  // If the image has not yet decoded, the swap is skipped silently — the
  // next bindIfVisible call (or autoscroll toggle) will retry.
  function swapGifToCanvas(wrap) {
    const img = wrap.querySelector("img");
    if (!img) return;
    const src = img.src;
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth || img.width || 320;
    canvas.height = img.naturalHeight || img.height || 240;
    try {
      const ctx = canvas.getContext("2d");
      if (ctx) ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    } catch (e) {
      // image not yet decoded — skip
      return;
    }
    wrap._gifOriginalSrc = src;
    img.replaceWith(canvas);
    state.swappedGifWraps.add(wrap);
  }

  // Iterate all swapped GIF wraps and replace their <canvas> with a fresh
  // <img> carrying the original src, so the GIF loops again.
  function restoreAllSwappedGifs() {
    state.swappedGifWraps.forEach((wrap) => {
      const canvas = wrap.querySelector("canvas");
      if (!canvas) return;
      const src = wrap._gifOriginalSrc || "";
      const img = document.createElement("img");
      img.src = src;
      canvas.replaceWith(img);
      delete wrap._gifOriginalSrc;
    });
    state.swappedGifWraps.clear();
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

  MRR.autoscrollController = { setAutoscroll, bindIfVisible, reset, getGifDuration };
})();
