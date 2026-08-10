const CACHE = "mrr-v1";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      cache.addAll([
        "/",
        "/static/style.css",
        "/static/app.js",
        "/static/controls.js",
        "/static/feed-view.js",
        "/static/item-store.js",
        "/static/scroll-controller.js",
        "/static/autoscroll-controller.js",
        "/static/cache-queue.js",
        "/static/zoom-controller.js",
        "/static/manifest.json",
        "/static/icon-192.png",
        "/static/icon-512.png",
        "/static/icon-512-maskable.png",
      ])
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  if (url.pathname.startsWith("/api/")) {
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
    return;
  }

  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});