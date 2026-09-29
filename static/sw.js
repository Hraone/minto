// Minto service worker.
// Caches static files only (icons, logos). It never touches pages, forms,
// login or logout, so what you see always comes from the server and
// your login state can never be out of sync.
const CACHE = "minto-static-v1";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  const isStaticFile =
    url.origin === self.location.origin &&
    url.pathname.startsWith("/static/") &&
    !url.pathname.endsWith("sw.js") &&
    !url.pathname.endsWith("manifest.json");

  // Everything else (pages, login, logout, forms) goes straight to the network.
  if (!isStaticFile) return;

  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      });
    })
  );
});
