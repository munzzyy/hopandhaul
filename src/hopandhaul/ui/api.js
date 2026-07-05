// fetch() wrappers for the server's JSON API. Every call target is a same-origin relative
// path — never build a URL from user input, there's nothing here for an attacker to redirect.

async function getJson(path, signal) {
  const res = await fetch(path, { signal });
  return res.json();
}

export function fetchConfig(signal) {
  return getJson("/api/config", signal);
}

export function fetchGeocode(query, signal) {
  const q = new URLSearchParams({ q: query, limit: "6" });
  return getJson(`/api/geocode?${q}`, signal);
}

export function fetchNearest(lat, lng, signal) {
  const q = new URLSearchParams({ lat: String(lat), lng: String(lng) });
  return getJson(`/api/nearest?${q}`, signal);
}

// Tracks the in-flight plan request so a new click cancels the previous one instead of
// racing it — last-requested-wins, not last-resolved-wins.
let _planAbort = null;

export function fetchPlan(params) {
  if (_planAbort) _planAbort.abort();
  const controller = new AbortController();
  _planAbort = controller;

  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === "") continue;
    q.set(k, String(v));
  }
  return fetch(`/api/plan?${q}`, { signal: controller.signal }).then((r) => r.json());
}
