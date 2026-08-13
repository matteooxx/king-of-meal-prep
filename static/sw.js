const CACHE = "king-static-v7";
const ASSETS = [
  "/static/css/app.css",
  "/static/js/app.js",
  "/static/js/shell.js",
  "/static/js/feedback.js",
  "/static/js/cook.js",
  "/static/js/guided_cook.js",
  "/static/js/shopping.js",
  "/static/js/vendor/lucide.min.js",
  "/static/fonts/ibm-plex-sans-var.woff2",
  "/static/fonts/jetbrains-mono-var.woff2",
  "/static/manifest.webmanifest",
  "/favicon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))
    ))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (
    event.request.method === "GET"
    && event.request.mode === "navigate"
    && url.origin === self.location.origin
    && url.pathname === "/shopping"
  ) {
    event.respondWith(
      fetch(event.request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put("/shopping", copy));
        }
        return response;
      }).catch(() => caches.match("/shopping"))
    );
    return;
  }
  if (
    event.request.method !== "GET"
    || url.origin !== self.location.origin
    || !url.pathname.startsWith("/static/")
  ) return;
  event.respondWith(
    caches.match(event.request).then((cached) => (
      cached || fetch(event.request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
    ))
  );
});
