// Hand-rolled offline-shell service worker — no Workbox, no build step.
// Caches the static app shell (HTML/JS/CSS/vendor/icons) so the UI loads offline. Planning
// itself now runs entirely client-side too (see ./engine/) — api.js only reaches the network
// for /api/* when a real python -m hopandhaul.server happens to be present (still always
// network-first, no cache fallback: a stale live fare is worse than falling back to the
// engine's own estimate, which api.js already does on its own). Precaching the engine modules
// and the airport/gateway data JSON here means a full trip plan works offline immediately
// after the first install, not just "the page loads but clicking anywhere needs a network."
// Only en.json is precached out of the 46 i18n catalogs (all 46 do exist) — that's the
// guaranteed fallback every t() call can lean on, so it's worth the install-time cost.
// The other 45 aren't worth bloating the install shell for: they're picked up by the
// generic fetch handler below on first successful load, so whichever language a visitor
// actually chooses works offline from then on, and they're dropped on every version bump
// along with the rest of the runtime cache.
const CACHE_VERSION = "hopandhaul-shell-v7";
const SHELL_FILES = [
  "./",
  "./index.html",
  "./styles.css",
  "./app.js",
  "./state.js",
  "./api.js",
  "./map.js",
  "./geo-labels.js",
  "./results.js",
  "./search.js",
  "./format.js",
  "./theme.js",
  "./theme-boot.js",
  "./i18n.js",
  "./lang.js",
  "./i18n/en.json",
  "./manifest.webmanifest",
  "./vendor/leaflet.js",
  "./vendor/leaflet.css",
  "./vendor/b612mono-regular.ttf",
  "./vendor/b612mono-bold.ttf",
  "./icons/favicon.svg",
  "./icons/icon.svg",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/icon-maskable-512.png",
  "./engine/pyround.js",
  "./engine/data.js",
  "./engine/geo.js",
  "./engine/trip.js",
  "./engine/emissions.js",
  "./engine/plan.js",
  "./engine/search.js",
  "./engine/validate.js",
];
// Staged into ./data/ for the published Pages artifact (see .github/workflows/pages.yml) —
// absent when ui/ is served straight from a repo checkout with no staging step (e.g.
// python -m hopandhaul.server run directly against source, or a plain static server pointed
// at the checkout). Cached individually, best-effort, so a missing data file there doesn't
// fail cache.addAll's all-or-nothing install for the (always-present) files above it.
const OPTIONAL_DATA_FILES = ["./data/airports.json", "./data/gateways.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then(async (cache) => {
        await cache.addAll(SHELL_FILES);
        await Promise.all(OPTIONAL_DATA_FILES.map((f) => cache.add(f).catch(() => {})));
      })
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;

  // API calls need live data — never serve a stale plan from cache.
  if (url.pathname.startsWith("/api/")) return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(event.request, copy));
          }
          return res;
        })
        .catch(() => cached);
      return cached || network;
    }),
  );
});
