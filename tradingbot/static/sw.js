/* SolEntry service worker.
 *
 * Caches ONLY the app shell and static assets (CSS, JS, images) so
 * the installed app opens fast and the shell works offline. Live
 * data is NEVER cached: navigations and /api/* always go to the
 * network first, so prices, balances and auth state are never stale.
 * A trading app must never show yesterday's numbers.
 */
const CACHE = "solentry-v1";
const SHELL = [
  "/static/style.css",
  "/static/fox-red-480.jpg",
  "/static/fox-gold-480.jpg",
  "/static/live.js",
  "/static/ui.js",
  "/static/quick_trade.js",
  "/static/coin_tick.js",
  "/static/copy.js",
  "/static/chart.js",
  "/static/donate.js",
  "/static/passkey.js"
];

self.addEventListener("install", (event) => {
  // One missing asset (e.g. an image not deployed yet) must never
  // block the whole install — cache each file independently.
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      Promise.all(
        SHELL.map((url) => cache.add(url).catch(() => null))
      )
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // Live data and auth: network only, never cached, never served stale.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) return;
  if (req.mode === "navigate") {
    // Pages always come from the network (they carry per-session
    // CSRF tokens and live balances). No offline page fallback.
    event.respondWith(fetch(req));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    // Same-origin static assets: serve the cache, refresh it behind.
    event.respondWith(
      caches.match(req).then((cached) => {
        const network = fetch(req)
          .then((resp) => {
            if (resp.ok) {
              const copy = resp.clone();
              caches.open(CACHE).then((c) => c.put(req, copy));
            }
            return resp;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
  }
});
