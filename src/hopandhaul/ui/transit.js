// transit.js - REAL ground schedules for the BROWSER build, via Transitous
// (api.transitous.org, CORS-open, keyless, free for open-source/non-commercial use).
//
// Mirrors hopandhaul/transit.py's summarize/describe logic so a live-schedule line reads the
// same whether the server or the static build produced it. Browsers can't set User-Agent
// (it's a forbidden header) - Transitous's policy accepts the automatically-sent Referer for
// browser-only callers, which is exactly what this is. Fails silent and fast: a short
// timeout, a session circuit-breaker, and null on anything unexpected - live schedules are
// an upgrade, never a blocker.
//
// This file is NOT part of the parity surface (tests/web_parity covers ./engine/ only) - 
// it's the browser twin of the server's own live layer.

const BASE = "https://api.transitous.org/api/v2/plan";
const TIMEOUT_MS = 6000;

const MODE_MAP = {
  HIGHSPEED_RAIL: "train", LONG_DISTANCE: "train", NIGHT_RAIL: "train",
  REGIONAL_RAIL: "train", REGIONAL_FAST_RAIL: "train", RAIL: "train",
  METRO: "transit", SUBWAY: "transit", TRAM: "transit",
  BUS: "bus", COACH: "bus",
  FERRY: "ferry",
  WALK: "walk", BIKE: "walk", CAR: "drive", ODM: "bus",
  AIRPLANE: "fly",
};

let _failures = 0;
let _openUntil = 0;

function breakerOpen() {
  return Date.now() < _openUntil;
}

function breakerRecord(ok) {
  if (ok) { _failures = 0; return; }
  _failures += 1;
  if (_failures >= 2) {
    _openUntil = Date.now() + 10 * 60 * 1000;
    _failures = 0;
  }
}

function legSummary(leg) {
  const mode = MODE_MAP[String(leg.mode || "").toUpperCase()] || "transit";
  return {
    mode,
    agency: leg.agencyName || null,
    route: leg.routeShortName || null,
    duration_h: Math.round(((leg.duration || 0) / 3600) * 100) / 100,
    depart: (leg.startTime || "").slice(11, 16) || null,
  };
}

function summarize(itin) {
  const legs = (itin.legs || []).map(legSummary);
  const riding = legs.filter((x) => x.mode !== "walk");
  const main = riding.length
    ? riding.reduce((a, b) => (b.duration_h > a.duration_h ? b : a))
    : null;
  const firstRide = riding[0] || null;
  return {
    duration_h: Math.round(((itin.duration || 0) / 3600) * 100) / 100,
    transfers: itin.transfers ?? Math.max(0, riding.length - 1),
    legs,
    main_mode: main ? main.mode : null,
    main_agency: main ? main.agency : null,
    main_route: main ? main.route : null,
    depart: firstRide ? firstRide.depart : null,
  };
}

/** One honest provenance line - same wording as transit.py's describe(). */
export function describe(t) {
  const riding = t.legs.filter((x) => x.mode !== "walk");
  const hops = riding
    .map((x) => ((x.agency || x.route) ? `${x.agency || ""} ${x.route || ""}`.trim() : x.mode))
    .join(" + ") || t.main_mode || "transit";
  const dep = t.depart ? ` departing ${t.depart}` : "";
  return `live schedule (Transitous, ${t.date}): ${hops}, `
    + `${t.duration_h}h door-to-door${dep}; `
    + `${t.n_options} scheduled option(s) found`;
}

function defaultDate() {
  const d = new Date(Date.now() + 7 * 86400000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Real scheduled journeys between two points, or null. Mirrors transit.ground_options. */
export async function groundOptions(fromLat, fromLng, toLat, toLng, date = null, preferMode = null) {
  if (breakerOpen()) return null;
  const day = date || defaultDate();
  const q = new URLSearchParams({
    fromPlace: `${fromLat},${fromLng}`,
    toPlace: `${toLat},${toLng}`,
    time: `${day}T06:00:00Z`,
  });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  let out;
  try {
    const res = await fetch(`${BASE}?${q}`, { signal: controller.signal });
    if (!res.ok) { breakerRecord(false); return null; }
    out = await res.json();
    breakerRecord(true);
  } catch {
    breakerRecord(false);
    return null;
  } finally {
    clearTimeout(timer);
  }
  let itins = (out.itineraries || []).map(summarize)
    .filter((i) => i.duration_h > 0 && i.main_mode);
  if (!itins.length) return null;
  let pool = itins;
  if (preferMode) {
    const matching = itins.filter((i) => i.main_mode === preferMode);
    if (matching.length) pool = matching;
  }
  const best = pool.reduce((a, b) => (b.duration_h < a.duration_h ? b : a));
  const result = { ...best, n_options: itins.length, date: day, source: "transitous" };
  result.line = describe(result);
  return result;
}
