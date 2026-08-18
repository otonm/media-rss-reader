// ---------------------------------------------------------------------------
// mediaEl — the single place a media element or a proxy URL is constructed.
//
// Both the download queue and the gallery builder need an <img>/<video>
// pointed at the media proxy with the same attribute set. Written twice, the
// two drifted: only one of them set playsinline.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  const MRR = (window.MRR = window.MRR || {});
  const PROXY_PREFIX = "/api/media/proxy?";

  function proxyUrl(url, itemId) {
    return `${PROXY_PREFIX}url=${encodeURIComponent(url)}&item_id=${encodeURIComponent(itemId)}`;
  }

  // opts.defer: offscreen gallery slides hand loading to the browser. A
  // 20-slide gallery opening 20 connections at once exhausts the ~6-per-host
  // budget and starves the cache queue and /api/items behind it.
  //
  // opts.eager: the download queue prefetching a video. preload must be set
  // before src — the UA picks a resource-selection mode when src lands, and
  // setting preload afterward does not re-trigger it, so a browser whose
  // default is "metadata" (Safari, Firefox) never downloads the body and the
  // prefetch caches nothing.
  function create(media, itemId, opts) {
    const o = opts || {};
    const isVideo = media.type === "video";
    const el = isVideo ? document.createElement("video") : new Image();
    if (isVideo) {
      el.setAttribute("playsinline", "");
      el.setAttribute("webkit-playsinline", "");
      el.setAttribute("controls", "");
      if (o.eager) {
        el.autoplay = true;
        el.muted = true;
        el.preload = "auto";
      } else if (o.defer) {
        el.preload = "none";
      }
    } else if (o.defer) {
      el.loading = "lazy";
    }
    el.src = proxyUrl(media.url, itemId);
    return el;
  }

  MRR.mediaEl = { proxyUrl, create, PROXY_PREFIX };
})();
