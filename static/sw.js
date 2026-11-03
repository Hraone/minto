// Minto service worker.
// Static assets are cached for fast loading, and navigations get a branded
// offline fallback when the network is unavailable. Authenticated pages are
// never cached or served from cache.
const CACHE = "minto-static-v7";
const OFFLINE_URL = "/static/offline.html";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.add(OFFLINE_URL))
      .catch(() => {})
      .then(() => self.skipWaiting())
  );
});

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
  if (url.origin !== self.location.origin) return;

  // Only use the offline screen for full page navigations. Never intercept
  // API calls, form submissions, or authentication requests.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(async () => {
        const cache = await caches.open(CACHE);
        const offline = await cache.match(OFFLINE_URL);
        if (offline) return offline;
        return new Response("You are offline. Please reconnect and try again.", {
          status: 503,
          headers: { "Content-Type": "text/plain; charset=utf-8" }
        });
      })
    );
    return;
  }

  const isStaticFile = url.pathname.startsWith("/static/") &&
    !url.pathname.endsWith("sw.js") &&
    !url.pathname.endsWith("manifest.json");
  if (!isStaticFile) return;

  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put(req, copy));
        }
        return res;
      });
    })
  );
});


// Web Push notifications are delivered by the server even when the app is closed.
self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_) {
    payload = { body: event.data ? event.data.text() : "" };
  }
  const title = payload.title || "Minto";
  const options = {
    body: payload.body || "Open Minto to review your expenses.",
    tag: payload.tag || "minto-daily-expenses",
    renotify: false,
    data: { url: payload.url || "/" }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(
    event.notification.data && event.notification.data.url || "/",
    self.location.origin
  ).href;
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        if (client.url.startsWith(self.location.origin) && "focus" in client) {
          return client.navigate(target).then(() => client.focus());
        }
      }
      return clients.openWindow(target);
    })
  );
});
