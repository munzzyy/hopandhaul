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
function priceFlightEstimate(origin, dest, date, ret, travelers) {
  const est = geo.estimateFlight(origin, dest, date);
  let price = est.price * Math.max(1, travelers);
  let rt = false;
  if (ret) {
    const estBack = geo.estimateFlight(dest, origin, ret);
    price += estBack.price * Math.max(1, travelers);
    rt = true;
  }
  return { price: pyRound(price, 2), hours: est.hours, source: "estimate", rt, estimate_detail: est };
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
  const dest = geo.nearestAirport(destLat, destLng, { preferHub: true });
  if (!dest) {
    return { ok: false, error: "no airport found near that point", code: "no_airport_near_point" };
  }
  if (dest.iata === origin.iata) {
    return {
      ok: false,
      error: "that point resolves to your origin airport — no flight needed",
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
  const priced = flightTargets.map((t) => priceFlightEstimate(origin, t, date, ret, travelers));

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
    g.fly = gf;
    const groundCost = trip.scaleLegCost(g.ground_mode, g.ground_cost, travelers) * rtMult;
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
    // A real-corridor ferry leg uses the actual port-to-port crossing distance - boats sail
    // the strait, they don't follow a winding road. Mirrors server.py's plan().
    const groundKm = g.ferry
      ? g.ferry.crossing_km * rtMult
      : geo.haversineKm(g.lat, g.lng, dest.lat, dest.lng) * geo.ROAD_WINDING * rtMult;
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
  notes.push(
    "Fares are distance-based ESTIMATES"
    + (date ? " (date-adjusted for booking window/season)" : "")
    + " — add a date for live fares. Verify before booking.",
  );
  if (travelers > 1) {
    notes.push(`Costs are GROUP TOTALS for ${travelers} travelers — per-person fares `
      + `×${travelers}; drive/rental legs are per vehicle.`);
  }
  if (roundtrip) {
    if (ret) {
      notes.push(`Round-trip: outbound + return (${ret}) estimated separately; `
        + "times are the outbound leg.");
    } else {
      notes.push("Round-trip: fares shown are ~2× one-way; add a return date for real "
        + "RT pricing. Times are for the outbound leg.");
    }
  }
  if (gws.some((g) => g.ferry)) {
    notes.push("Ferry legs are REAL corridors (bundled research, operators + typical "
      + "fares + sailings/day as of the data's date) — schedules vary by day and "
      + "season, so check the operator before relying on a connection.");
  }
  if (gws.some((g) => g.transit)) {
    notes.push("Ground legs marked 'live schedule' use real timetables via Transitous "
      + "(transitous.org — community GTFS/OSM data): real operators, departures "
      + "and door-to-door times. Fares on those legs are still estimates.");
  }
  if ((dest.dist_km || 0) > 120) {
    notes.push(`Nearest airport ${dest.iata} is ~${Math.trunc(dest.dist_km)} km from the `
      + "clicked point — the last mile to your exact spot isn't included.");
  }
  notes.push(
    "co2e_kg per option is a rough ESTIMATE from flight/ground distance, not a "
    + "certified footprint — see docs/api.md for the factor basis. The lowest-carbon "
    + "option is flagged as 'greenest' but never auto-recommended over the cheapest.",
  );

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
