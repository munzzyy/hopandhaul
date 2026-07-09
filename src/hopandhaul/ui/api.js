// api.js — same fetchConfig/fetchGeocode/fetchNearest/fetchPlan contract app.js/search.js
// already call. Two backends, tried in this order:
//
//   1. A real python -m hopandhaul.server on this origin (LIVE fares/weather/typed-geocode
//      when it's keyed). Detected once via a same-origin /api/config probe — see probeServer().
//   2. The in-browser engine (./engine/) + the shipped airport DB — no network beyond fetching
//      the two static JSON data files, no keys, works on GitHub Pages or any static host.
//
// GitHub Pages has no /api/* routes (a plain 404), same as `python -m http.server` used for
// local static preview — both look identical to the probe: "no server, use the engine". Only
// a real hopandhaul server answers /api/config with {ok:true, ...}, and even then only for
// whichever of geocode/weather/live-fares it actually has keys for — everything else still
// falls back to the engine per-call (see fetchGeocode).
import { plan as enginePlan } from "./engine/plan.js";
import { loadData } from "./engine/data.js";
import { nearestAirport } from "./engine/geo.js";
import { searchAirports } from "./engine/search.js";
import { parsePlanParams, parseNearestParams, ValidationError } from "./engine/validate.js";
import { groundOptions as transitGroundOptions } from "./transit.js";

const SERVER_PROBE_TIMEOUT_MS = 1500;

let _dataPromise = null;
function ensureData() {
  if (!_dataPromise) _dataPromise = loadData();
  return _dataPromise;
}
// Warm the fallback data in the background as soon as this module loads, in parallel with the
// server probe below — whichever backend actually ends up serving a given call, the engine's
// data is ready by the time anything needs it instead of adding its own load latency later.
ensureData().catch(() => {}); // a real failure surfaces later, when a caller actually awaits it

function err(code, message) {
  return { ok: false, error: message, code };
}

async function getJson(path, signal) {
  const res = await fetch(path, { signal });
  return res.json();
}

// ---- server detection --------------------------------------------------------------------
let _serverProbe = null;

/** Resolves to the server's /api/config payload if a real hopandhaul server answered, else
 * null. Memoized — every caller below awaits the same one probe. */
function probeServer() {
  if (!_serverProbe) {
    _serverProbe = (async () => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), SERVER_PROBE_TIMEOUT_MS);
      try {
        const res = await fetch("/api/config", { signal: controller.signal });
        if (!res.ok) return null;
        const c = await res.json();
        return c && c.ok === true ? c : null;
      } catch {
        return null; // network error, timeout, non-JSON body, or a Pages/static-host 404
      } finally {
        clearTimeout(timer);
      }
    })();
  }
  return _serverProbe;
}

export async function fetchConfig() {
  const server = await probeServer();
  if (server) return server;
  try {
    await ensureData();
  } catch {
    return err("internal_error", "could not load the airport database");
  }
  return {
    ok: true,
    has_live_keys: false,
    flights_provider: null,
    // Backed by the local airport DB (engine/search.js), not a live geocoder — there's no key
    // for one on Pages — but it does work, so this is honestly true, not a degraded "off".
    has_geocode: true,
    has_weather: false,
    default_origin: "JFK",
    default_threshold: 200,
    default_travelers: 1,
    supports_return_date: true,
  };
}

export async function fetchGeocode(query, signal) {
  const server = await probeServer();
  if (server && server.has_geocode) {
    try {
      const q = new URLSearchParams({ q: query, limit: "6" });
      return await getJson(`/api/geocode?${q}`, signal);
    } catch {
      // server vanished mid-session (stopped, network blip) — fall back to the local search
    }
  }
  try {
    await ensureData();
  } catch {
    return err("internal_error", "could not load the airport database");
  }
  const local = searchAirports(query, 6);
  // A code or a known airport city resolves locally — keep that instant and offline. For
  // anything the airport DB can't answer (an address, a village, a landmark), Photon
  // (photon.komoot.io — keyless, CORS-open, OSM data) turns the static build's search box
  // into a real geocoder. Best-effort: any failure falls back to the local matches.
  if (!local.length && String(query || "").trim().length >= 3) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 2500);
      if (signal) signal.addEventListener("abort", () => controller.abort(), { once: true });
      const res = await fetch(
        `https://photon.komoot.io/api/?${new URLSearchParams({ q: query, limit: "6" })}`,
        { signal: controller.signal },
      );
      clearTimeout(timer);
      if (res.ok) {
        const out = await res.json();
        const results = (out.features || []).map((f) => {
          const c = (f.geometry || {}).coordinates || [];
          const pr = f.properties || {};
          if (c.length < 2 || !pr.name) return null;
          const bits = [pr.name, pr.city !== pr.name ? pr.city : null, pr.state, pr.country]
            .filter(Boolean);
          return { lat: c[1], lng: c[0], label: bits.join(", "), city: pr.city || pr.name,
                   country: pr.country || null };
        }).filter(Boolean);
        if (results.length) return { ok: true, results };
      }
    } catch {
      // offline, blocked, or slow — the local airport search below still answers
    }
  }
  return { ok: true, results: local };
}

export async function fetchNearest(lat, lng, signal) {
  const server = await probeServer();
  if (server) {
    try {
      const q = new URLSearchParams({ lat: String(lat), lng: String(lng) });
      return await getJson(`/api/nearest?${q}`, signal);
    } catch {
      // fall through to the local lookup below
    }
  }
  try {
    await ensureData();
    const params = parseNearestParams({ lat, lng });
    const a = nearestAirport(params.lat, params.lng, { preferHub: true });
    if (!a) return err("no_airport_found", "no airport found");
    return {
      ok: true,
      airport: { iata: a.iata, name: a.name, city: a.city ?? null, lat: a.lat, lng: a.lng, hub: a.hub },
    };
  } catch (e) {
    if (e instanceof ValidationError) return err("invalid_param", e.message);
    return err("internal_error", "could not resolve nearest airport");
  }
}

// Tracks the in-flight plan request so a new click supersedes the previous one instead of
// racing it — last-requested-wins, not last-resolved-wins. `_planAbort` cancels a real
// server-backed fetch; `_planToken` guards the local-engine path, which has no request to
// cancel (it's synchronous CPU work) but still needs to bail out if a newer click beat it here.
let _planAbort = null;
let _planToken = 0;

export async function fetchPlan(params) {
  const myToken = ++_planToken;
  const server = await probeServer();

  if (server) {
    if (_planAbort) _planAbort.abort();
    const controller = new AbortController();
    _planAbort = controller;
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v === null || v === undefined || v === "") continue;
      q.set(k, String(v));
    }
    try {
      const res = await fetch(`/api/plan?${q}`, { signal: controller.signal });
      return await res.json();
    } catch (e) {
      if (e?.name === "AbortError") throw e; // superseded by a newer click — propagate, don't fall back
      // a real network failure against a server that answered /api/config a moment ago —
      // degrade to the local engine rather than surfacing a dead end.
    }
  }

  try {
    await ensureData();
  } catch {
    return err("internal_error", "could not load the airport database");
  }
  if (myToken !== _planToken) {
    const abort = new Error("superseded by a newer plan request");
    abort.name = "AbortError";
    throw abort;
  }

  let parsed;
  try {
    parsed = parsePlanParams(params);
  } catch (e) {
    if (e instanceof ValidationError) return err("invalid_param", e.message);
    return err("internal_error", "could not parse those trip settings");
  }
  try {
    const engineParams = {
      destLat: parsed.dest_lat,
      destLng: parsed.dest_lng,
      originIata: parsed.origin_iata,
      date: parsed.date,
      vot: parsed.vot,
      threshold: parsed.threshold,
      maxGroundH: parsed.max_ground_h,
      roundtrip: parsed.roundtrip,
      travelers: parsed.travelers,
      ret: parsed.ret,
      transferBuffer: parsed.transfer_buffer,
    };
    let out = enginePlan(engineParams);
    if (!out.ok) return err(out.code || "plan_failed", out.error || "could not plan that route");

    // Live-schedule upgrade (browser twin of the server's Transitous enrichment): fetch real
    // timetables for the transit-able gateway legs the offline plan found, then re-run the
    // engine with the real door-to-door times injected so the ranking uses them too.
    const lookups = out.gateways.filter((g) => ["train", "bus", "ferry"].includes(g.ground_mode));
    if (lookups.length) {
      const settled = await Promise.allSettled(lookups.map((g) => transitGroundOptions(
        g.lat, g.lng, parsed.dest_lat, parsed.dest_lng, parsed.date, g.ground_mode,
      )));
      if (myToken !== _planToken) {
        const abort = new Error("superseded by a newer plan request");
        abort.name = "AbortError";
        throw abort;
      }
      const transitByIata = {};
      settled.forEach((s, i) => {
        if (s.status === "fulfilled" && s.value) transitByIata[lookups[i].iata] = s.value;
      });
      if (Object.keys(transitByIata).length) {
        out = enginePlan({ ...engineParams, transitByIata });
        if (!out.ok) return err(out.code || "plan_failed", out.error || "could not plan that route");
      }
    }
    return out;
  } catch (e) {
    if (e?.name === "AbortError") throw e; // superseded by a newer click — propagate
    // Mirrors server.py's _handle_plan: never leak internals to the UI, log for debugging.
    console.error("[hopandhaul] plan() failed:", e);
    return err("internal_error", "internal error planning that route");
  }
}
