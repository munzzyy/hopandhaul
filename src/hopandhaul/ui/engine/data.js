// data.js — loads the shipped airport/gateway databases the same way geo.py's
// _read_package_json() does, so the browser engine sees exactly the data the Python engine
// sees. No network geocoding, no live API — just the two static JSON files this repo already
// ships (src/hopandhaul/data/airports.json, gateways.json), fetched once and cached.
//
// The default loader fetches "./data/<file>" relative to this module (i.e. ui/data/<file> —
// see ../../../.github/workflows/pages.yml for how that directory gets populated in the
// published Pages artifact). Callers that aren't a browser (the Node parity harness) pass a
// custom `loader` to loadData() that reads the real src/hopandhaul/data/*.json off disk
// instead — same JSON, no duplicated copy required for tests.

let _airports = null;
let _gatewaysDb = null;
let _byIata = null;
let _ferries = null;
let _landgrid = null;
let _anchors = null;
let _anchorsAsof = "";
let _loadPromise = null;

async function defaultLoader(filename) {
  // import.meta.url is this file's own location (ui/engine/data.js) — the data files are
  // staged one level up, at ui/data/ (see .github/workflows/pages.yml), so "../data/" reaches
  // ui/data/<file> regardless of what path the page itself was loaded from.
  const url = new URL(`../data/${filename}`, import.meta.url);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`failed to load ${filename}: HTTP ${res.status}`);
  return res.json();
}

/** Fetch + cache airports.json/gateways.json. Safe to call more than once — later calls
 * reuse the same in-flight/resolved promise instead of re-fetching. */
export function loadData(loader = defaultLoader) {
  if (_loadPromise) return _loadPromise;
  _loadPromise = (async () => {
    const [airportsDoc, gatewaysDoc, ferriesDoc, landgridDoc, anchorsDoc] = await Promise.all([
      loader("airports.json"),
      loader("gateways.json"),
      loader("ferries.json"),
      loader("landgrid.json"),
      loader("fareanchors.json"),
    ]);
    _airports = airportsDoc.airports;
    _gatewaysDb = gatewaysDoc;
    _ferries = ferriesDoc.corridors;
    // packed land/water bitmap: base64 -> bytes. atob exists in every modern browser AND
    // Node 16+ (the parity harness), so one decode path serves both.
    const bin = atob(landgridDoc.b64);
    const bits = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bits[i] = bin.charCodeAt(i);
    _landgrid = { bits, w: landgridDoc.w, h: landgridDoc.h, res: landgridDoc.res_deg };
    _anchors = anchorsDoc.anchors;
    _anchorsAsof = anchorsDoc.asof || "";
    _byIata = new Map();
    for (const a of _airports) _byIata.set(a.iata, a);
    return { airports: _airports, gatewaysDb: _gatewaysDb };
  })();
  return _loadPromise;
}

export function isLoaded() {
  return _airports !== null;
}

function ensureLoaded() {
  if (!_airports) {
    throw new Error("hopandhaul engine: data not loaded yet — call loadData() and await it first");
  }
}

/** The full airport list, in file order — callers that scan it (nearest_airport, gateway
 * discovery) rely on that order matching the Python side's iteration order exactly. */
export function airports() {
  ensureLoaded();
  return _airports;
}

export function gatewaysDb() {
  ensureLoaded();
  return _gatewaysDb;
}

/** Mirrors geo.by_iata: case-insensitive lookup, null (not undefined) when missing. */
export function byIata(code) {
  ensureLoaded();
  return _byIata.get(String(code || "").toUpperCase()) ?? null;
}

/** Real ferry corridors, in file order — mirrors geo.ferry_corridors(). */
export function ferryCorridors() {
  ensureLoaded();
  return _ferries;
}

/** Packed land/water bitmap — mirrors geo._landgrid(). */
export function landgrid() {
  ensureLoaded();
  return _landgrid;
}

/** REAL BTS city-pair fare anchors, in file order — mirrors geo.fare_anchors(). */
export function fareAnchors() {
  ensureLoaded();
  return _anchors;
}

export function fareAnchorsAsof() {
  ensureLoaded();
  return _anchorsAsof;
}
