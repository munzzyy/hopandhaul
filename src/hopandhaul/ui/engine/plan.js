// plan.js - the browser-native orchestrator. This is a faithful port of the ESTIMATE branch of
// server.py's plan(): same option generation (direct + one split per discovered gateway),
// same notes, same response shape - just with every live-network path (Duffel fares, OpenWeather,
// Geoapify) removed, because none of those have a key on GitHub Pages and server.py's plan()
// already degrades to this exact estimate path whenever allow_live=False. That's the contract
// this file has to hold: `plan(...)` here must equal Python's `plan(..., allow_live=False,
// fetch_weather=False)` for the same inputs - see tests/web_parity/.
import * as geo from "./geo.js";
import * as trip from "./trip.js";
import * as emissions from "./emissions.js";
import * as itinerary from "./itinerary.js";
import { byIata } from "./data.js";
import { pyRound } from "./pyround.js";

function pt(a, full = false) {
  const base = { iata: a.iata, lat: a.lat, lng: a.lng };
  if (full) Object.assign(base, { name: a.name, city: a.city ?? null, hub: a.hub });
  return base;
}

function gw(g) {
  const out = {
    iata: g.iata, name: g.name, city: g.city ?? null, lat: g.lat, lng: g.lng, hub: g.hub,
    ground_mode: g.ground_mode, ground_hours: g.ground_hours, ground_cost: g.ground_cost,
    source: g.source, notes: g.notes || "", fly: g.fly ?? null,
  };
  if (g.ferry) out.ferry = g.ferry;
  if (g.transit) out.transit = g.transit;
  return out;
}

/** Estimate-only flight leg pricing - mirrors server.py's _price_flight() with `session=None`
 * (i.e. the branch it always falls into once allow_live=False, which the Pages build always is).
 * Keeps `estimateDetail` (the outbound-leg estimate only, same as the Python side) so the
 * itinerary can narrate where the fare came from - never discarded just because the option
 * string only needs the price. */
function priceFlightEstimate(origin, dest, date, ret, travelers, ctx) {
  const est = geo.estimateFlight(origin, dest, date);
  let price = est.price * Math.max(1, travelers);
  let rt = false;
  noteDateBasis(ctx, est);
  if (ret) {
    const estBack = geo.estimateFlight(dest, origin, ret);
    price += estBack.price * Math.max(1, travelers);
    rt = true;
    noteDateBasis(ctx, estBack);
  }
  return { price: pyRound(price, 2), hours: est.hours, source: "estimate", rt, estimate_detail: est };
}

/** Mirrors server.py's _note_date_basis(): did the user's date actually move this estimate? */
function noteDateBasis(ctx, est) {
  if (est.past_date) ctx.past_date = true;
  else if (est.date_mult) ctx.date_applied = true;
}

/** Build a structured plan() note: {key, params} - mirrors itinerary.note(). Notes used to be
 * hardcoded English strings spliced untranslated into every non-English locale's trust box;
 * the browser's own i18n layer now looks the key up in its own language file. */
function note(key, params = {}) {
  return { key, params };
}

/** Mirrors server.py's _estimate_note() - the note KEY is compared by the web-parity gate, so
 * these two functions have to stay in lockstep. */
function estimateNote(date, ctx) {
  if (ctx.past_date) return note("notes.estimatePastDate");
  if (ctx.date_applied) return note("notes.estimateDateApplied");
  if (date) return note("notes.estimateNeutralWindow");
  // No livePossible branch here on purpose: this build has no Duffel code path and the page
  // CSP won't allow one, so the "add a date for live fares" case can never be reached. It
  // matches server.py's plan(allow_live=False), which is what the parity gate generates.
  return note("notes.estimateNoProvider");
}

/** itinerary.js leg spec for a flight leg - mirrors server.py's _flight_leg_spec(), estimate
 * branch only (the Pages build never has a live provider). */
function flightLegSpec(origin, dest, f, cost, date) {
  return {
    mode: "fly", cost: pyRound(cost, 2), hours: f.hours, from: origin, to: dest,
    price_basis: itinerary.flightProvenanceEstimate(f.estimate_detail, date),
    verify_url: itinerary.verifyLink("fly", origin, dest, date),
    is_live: false, segments: null,
  };
}

/** itinerary.js leg spec for a ground leg - mirrors server.py's _ground_leg_spec(); ground legs
 * are always an estimate (see README: no free, open multimodal fares API worth calling here). */
function groundLegSpec(g, dest, cost, roadKm) {
  return {
    mode: g.ground_mode, cost: pyRound(cost, 2), hours: g.ground_hours, from: g, to: dest,
    price_basis: itinerary.groundProvenance(g, roadKm),
    verify_url: itinerary.verifyLink(g.ground_mode, g, dest),
    is_live: false, segments: null,
  };
}

/**
 * @param {object} params
 * @param {number} params.destLat
 * @param {number} params.destLng
 * @param {string} [params.originIata]
 * @param {string|null} [params.date] YYYY-MM-DD
 * @param {number|null} [params.vot]
 * @param {number} [params.threshold]
 * @param {number} [params.maxGroundH]
 * @param {boolean} [params.roundtrip]
 * @param {number} [params.travelers]
 * @param {string|null} [params.ret] YYYY-MM-DD return date
 * @param {number} [params.transferBuffer]
 * @returns {object} same shape as server.py's plan(..., allow_live=False, fetch_weather=False)
 */
export function plan({
  destLat, destLng, originIata = "JFK", date = null, vot = null, threshold = 200.0,
  maxGroundH = 6.0, roundtrip = false, travelers = 1, ret = null, transferBuffer = 1.0,
  transitByIata = null,
}) {
  const origin = byIata(originIata);
  if (!origin) {
    return { ok: false, error: `unknown origin airport '${originIata}'`, code: "unknown_origin" };
  }
  if (geo.isSuspended(origin)) {
    // text must match ui/i18n/en.json's notes.originSuspended template exactly - server.py
    // renders the same key server-side (itinerary.render_note), and the web-parity gate pins
    // both engines' JSON output byte for byte.
    return {
      ok: false,
      error: `${origin.iata} has no bookable service right now because of closed or `
        + "restricted airspace or ground crossings. Pick a different origin.",
      code: "origin_suspended",
    };
  }
  const { airport: dest, note: suspendedNote } = geo.nearestServedOrNote(destLat, destLng, { preferHub: true });
  if (!dest) {
    if (suspendedNote) {
      // text must match ui/i18n/en.json's notes.airportSuspended template exactly (see above).
      return {
        ok: false,
        error: "No served airport is near this point. Airspace or service there is "
          + "currently suspended.",
        code: "airport_suspended",
      };
    }
    return { ok: false, error: "no airport found near that point", code: "no_airport_near_point" };
  }
  if (dest.iata === origin.iata) {
    return {
      ok: false,
      error: "that point resolves to your origin airport, so there is no flight to plan",
      code: "origin_is_destination",
    };
  }

  travelers = Math.max(1, Math.min(9, Math.trunc(travelers)));
  if (ret) roundtrip = true;
  const rtMult = roundtrip ? 2 : 1;

  const gws = geo.discoverGateways(dest, origin, { maxGroundH });

  // Live-schedule injection (browser twin of server.py's Transitous enrichment): api.js runs
  // this plan once offline, fetches real timetables for the gateway legs it found, then runs
  // it again with the results - a real door-to-door time replaces the leg's formula duration
  // before ranking. Never set by the parity harness, so the offline contract is untouched.
  if (transitByIata) {
    for (const g of gws) {
      const tr = transitByIata[g.iata];
      if (tr) {
        g.transit = tr;
        g.ground_hours = tr.duration_h;
      }
    }
  }

  const flightTargets = [dest, ...gws];
  const ctx = {};
  const priced = flightTargets.map((t) => priceFlightEstimate(origin, t, date, ret, travelers, ctx));

  const options = [];
  const geoByName = {};
  const emissionsLegsByName = {};
  const legSpecsByName = {};

  function flightCost(f) {
    if (roundtrip && !f.rt) return f.price * 2;
    return f.price;
  }

  // direct
  const df = priced[0];
  const directName = `Fly direct to ${dest.iata}`;
  const directCost = flightCost(df);
  options.push(trip.parseOption(`${directName} | fly ${directCost} ${df.hours}`));
  geoByName[directName] = [{ type: "flight", from: pt(origin), to: pt(dest) }];
  legSpecsByName[directName] = [flightLegSpec(origin, dest, df, directCost, date)];
  const directKm = geo.haversineKm(origin.lat, origin.lng, dest.lat, dest.lng) * rtMult;
  emissionsLegsByName[directName] = [{ mode: "fly", distance_km: directKm }];

  // splits (fly to a cheaper hub, then ground it)
  gws.forEach((g, i) => {
    const gf = priced[i + 1];
    const groundCost = trip.scaleLegCost(g.ground_mode, g.ground_cost, travelers) * rtMult;
    // A real-corridor ferry leg uses the actual port-to-port crossing distance - boats sail
    // the strait, they don't follow a winding road. Mirrors server.py's plan().
    const groundKm = g.ferry
      ? g.ferry.crossing_km * rtMult
      : geo.haversineKm(g.lat, g.lng, dest.lat, dest.lng) * geo.ROAD_WINDING * rtMult;
    // the gateway curated for this destination IS the user's own origin airport - there is no
    // flight to take, just the ground leg from home. Mirrors server.py's plan(). Skip pricing
    // a same-airport "flight" entirely (no invented fare on top of the real bus/train).
    if (g.iata === origin.iata) {
      const name = `${g.ground_mode.charAt(0).toUpperCase()}${g.ground_mode.slice(1)} only from ${origin.iata}`;
      options.push(trip.parseOption(`${name} | ${g.ground_mode} ${groundCost} ${g.ground_hours}`));
      geoByName[name] = [
        { type: "ground", mode: g.ground_mode, from: pt(origin), to: pt(dest) },
      ];
      emissionsLegsByName[name] = [{ mode: g.ground_mode, road_km: groundKm }];
      legSpecsByName[name] = [
        groundLegSpec(g, dest, groundCost, groundKm / Math.max(rtMult, 1)),
      ];
      return;
    }
    g.fly = gf;
    const flyCost = flightCost(gf);
    const name = `${g.iata} + ${g.ground_mode}`;
    options.push(trip.parseOption(
      `${name} | fly ${flyCost} ${gf.hours} ; ${g.ground_mode} ${groundCost} ${g.ground_hours}`,
    ));
    geoByName[name] = [
      { type: "flight", from: pt(origin), to: pt(g) },
      { type: "ground", mode: g.ground_mode, from: pt(g), to: pt(dest) },
    ];
    const flyKm = geo.haversineKm(origin.lat, origin.lng, g.lat, g.lng) * rtMult;
    emissionsLegsByName[name] = [
      { mode: "fly", distance_km: flyKm },
      { mode: g.ground_mode, road_km: groundKm },
    ];
    legSpecsByName[name] = [
      flightLegSpec(origin, g, gf, flyCost, date),
      groundLegSpec(g, dest, groundCost, groundKm / Math.max(rtMult, 1)),
    ];
  });

  const res = trip.evaluate(options, { threshold, vot, transferBuffer, travelers });

  const clean = {};
  for (const [k, v] of Object.entries(res)) {
    if (!k.startsWith("_")) clean[k] = v;
  }
  for (const o of clean.options) {
    o.geo = geoByName[o.name] || [];
    o.co2e_kg = emissions.co2eForOption(emissionsLegsByName[o.name] || [], travelers);
    o.itinerary = itinerary.buildTimeline(legSpecsByName[o.name] || [], {
      date, transferBufferH: transferBuffer,
    });
  }
  clean.greenest = clean.options.length
    ? clean.options.reduce((best, o) => (o.co2e_kg < best.co2e_kg ? o : best)).name
    : null;

  // allow_live is always false on Pages - pricing_source is always "estimate", the same value
  // server.py's plan() would produce when it can't (or won't) reach a live provider.
  const source = "estimate";
  const notes = [];
  notes.push(estimateNote(date, ctx));
  if (travelers > 1) {
    notes.push(note("notes.groupTotals", { travelers, vehicles: trip.vehiclesNeeded(travelers) }));
  }
  if (roundtrip) {
    if (ret) {
      notes.push(note("notes.roundtripEstimatedSeparate", { return_date: ret }));
    } else {
      notes.push(note("notes.roundtripEstimated2x"));
    }
  }
  if (gws.some((g) => g.ferry)) {
    notes.push(note("notes.ferryRealCorridor"));
  }
  if (gws.some((g) => g.transit)) {
    notes.push(note("notes.transitLiveSchedule"));
  }
  if ((dest.dist_km || 0) > 120) {
    notes.push(note("notes.lastMileGap", { iata: dest.iata, km: Math.trunc(dest.dist_km) }));
  }
  notes.push(note("notes.co2eEstimate"));

  return {
    ok: true,
    pricing_source: source,
    date,
    return_date: ret,
    roundtrip,
    travelers,
    threshold,
    vot,
    origin: pt(origin, true),
    dest: { ...pt(dest, true), dist_km: dest.dist_km ?? null, click: { lat: destLat, lng: destLng } },
    gateways: gws.map(gw),
    direct: df,
    result: clean,
    weather: null, // no OpenWeather key on Pages - the UI already treats a null weather block as "no data"
    notes,
  };
}
