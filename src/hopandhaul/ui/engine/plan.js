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
 * branch only (the Pages build never has a live provider). `ret` is the return date, when this
 * fare covers a real round trip - only ever changes the verify link. */
function flightLegSpec(origin, dest, f, cost, date, ret = null) {
  return {
    mode: "fly", cost: pyRound(cost, 2), hours: f.hours, from: origin, to: dest,
    price_basis: itinerary.flightProvenanceEstimate(f.estimate_detail, date),
    basis_parts: itinerary.basisPartsFlightEstimate(f.estimate_detail, date),
    verify_url: itinerary.verifyLink("fly", origin, dest, date, ret),
    is_live: false, segments: null,
    likely_connection: Boolean(f.estimate_detail && f.estimate_detail.likely_connection),
  };
}

/** itinerary.js leg spec for a ground leg - mirrors server.py's _ground_leg_spec(); ground legs
 * are always an estimate (see README: no free, open multimodal fares API worth calling here).
 * `frm` overrides the leg's own start point when it isn't `g` itself (an origin-side split's
 * ground leg runs origin -> g, not g -> dest). */
function groundLegSpec(g, dest, cost, roadKm, frm = null) {
  const originPt = frm ?? g;
  return {
    mode: g.ground_mode, cost: pyRound(cost, 2), hours: g.ground_hours, from: originPt, to: dest,
    price_basis: itinerary.groundProvenance(g, roadKm),
    basis_parts: itinerary.basisPartsGround(g, roadKm),
    verify_url: itinerary.verifyLink(g.ground_mode, originPt, dest),
    is_live: false, segments: null,
  };
}

/** itinerary.js leg spec for the last-mile leg from the resolved destination AIRPORT onward to
 * the actual clicked/searched point - mirrors server.py's _final_leg_spec(). */
function finalLegSpec(dest, place, final, cost) {
  const gwLike = { ground_mode: final.mode, ferry: final.ferry ?? null, source: "auto",
    notes: final.notes || "" };
  return {
    mode: final.mode, cost: pyRound(cost, 2), hours: final.hours, from: dest, to: place,
    price_basis: itinerary.groundProvenance(gwLike, final.distance_km),
    basis_parts: itinerary.basisPartsGround(gwLike, final.distance_km),
    verify_url: itinerary.verifyLink(final.mode, dest, place),
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
  const { airport: dest, note: suspendedNote, skippedCloser } = geo.nearestServedOrNote(destLat, destLng, { preferHub: true });
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
  // Origin-side splits: the SAME call, roles swapped - mirrors server.py's plan(). A major-hub
  // origin naturally yields nothing here (discoverGateways gates on the FIRST arg's hub tier).
  const originGws = geo.discoverGateways(origin, dest, { maxGroundH });

  // Live-schedule injection (browser twin of server.py's Transitous enrichment): api.js runs
  // this plan once offline, fetches real timetables for the gateway legs it found, then runs
  // it again with the results - a real door-to-door time replaces the leg's formula duration
  // before ranking. Never set by the parity harness, so the offline contract is untouched.
  if (transitByIata) {
    for (const g of [...gws, ...originGws]) {
      const tr = transitByIata[g.iata];
      if (tr) {
        g.transit = tr;
        g.ground_hours = tr.duration_h;
      }
    }
  }

  // flight leg pairs: direct, each dest-side gateway (origin -> g), each origin-side gateway
  // (g -> dest) - the last group flies the OPPOSITE direction from the other two.
  const flightPairs = [
    [origin, dest], ...gws.map((g) => [origin, g]), ...originGws.map((g) => [g, dest]),
  ];
  const ctx = {};
  const priced = flightPairs.map(([frm, to]) => priceFlightEstimate(frm, to, date, ret, travelers, ctx));

  const options = [];
  const geoByName = {};
  const emissionsLegsByName = {};
  const legSpecsByName = {};
  // structured option-name contract (docs/api.md): name_key/name_params alongside the plain
  // English name trip.js already computes - mirrors server.py's name_meta_by_name.
  const nameMetaByName = {};

  function flightCost(f) {
    if (roundtrip && !f.rt) return f.price * 2;
    return f.price;
  }

  // The last-mile leg: geo.finalLeg() prices the honest last hop from the resolved destination
  // AIRPORT onward to the actual clicked/searched point. Computed once and appended to EVERY
  // option below (including the direct-flight baseline) - mirrors server.py's plan().
  const final = geo.finalLeg(dest, destLat, destLng);
  const finalPlace = {
    iata: "", name: `${destLat.toFixed(4)}, ${destLng.toFixed(4)}`, city: null,
    lat: destLat, lng: destLng, hub: 3,
  };
  let finalSuffix = "";
  let finalGeoEntry = null;
  let finalEmissionsEntry = null;
  let finalLegSpecRow = null;
  if (final && final.possible) {
    const finalCost = trip.scaleLegCost(final.mode, final.cost, travelers) * rtMult;
    // "final:" prefix marks this leg as the last-mile hop (trip.FINAL_LEG_PREFIX) so it never
    // counts toward is_split, even though it rides along on every option - mirrors server.py.
    finalSuffix = ` ; ${trip.FINAL_LEG_PREFIX}${final.mode} ${finalCost} ${final.hours}`;
    finalGeoEntry = { type: "ground", mode: final.mode, from: pt(dest), to: pt(finalPlace) };
    const finalDistKm = (final.ferry ? final.ferry.crossing_km : final.distance_km) * rtMult;
    finalEmissionsEntry = { mode: final.mode, road_km: finalDistKm };
    finalLegSpecRow = finalLegSpec(dest, finalPlace, final, finalCost);
  }
  function appendFinal(name) {
    if (finalGeoEntry) {
      geoByName[name].push(finalGeoEntry);
      emissionsLegsByName[name].push(finalEmissionsEntry);
      legSpecsByName[name].push(finalLegSpecRow);
    }
  }

  // direct
  const df = priced[0];
  const directName = `Fly direct to ${dest.iata}`;
  const directCost = flightCost(df);
  options.push(trip.parseOption(`${directName} | fly ${directCost} ${df.hours}${finalSuffix}`));
  nameMetaByName[directName] = { key: "option.flyDirect", params: { iata: dest.iata } };
  geoByName[directName] = [{ type: "flight", from: pt(origin), to: pt(dest) }];
  legSpecsByName[directName] = [flightLegSpec(origin, dest, df, directCost, date, ret)];
  const directKm = geo.haversineKm(origin.lat, origin.lng, dest.lat, dest.lng) * rtMult;
  emissionsLegsByName[directName] = [{ mode: "fly", distance_km: directKm }];
  appendFinal(directName);

  // dest-side splits (fly to a cheaper hub near the DESTINATION, then ground it)
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
      options.push(trip.parseOption(`${name} | ${g.ground_mode} ${groundCost} ${g.ground_hours}${finalSuffix}`));
      nameMetaByName[name] = { key: "option.groundOnlyFrom",
        params: { iata: origin.iata, mode: itinerary.modeI18nParam(g.ground_mode) } };
      geoByName[name] = [
        { type: "ground", mode: g.ground_mode, from: pt(origin), to: pt(dest) },
      ];
      emissionsLegsByName[name] = [{ mode: g.ground_mode, road_km: groundKm }];
      legSpecsByName[name] = [
        groundLegSpec(g, dest, groundCost, groundKm / Math.max(rtMult, 1)),
      ];
      appendFinal(name);
      return;
    }
    g.fly = gf;
    const flyCost = flightCost(gf);
    const name = `${g.iata} + ${g.ground_mode}`;
    options.push(trip.parseOption(
      `${name} | fly ${flyCost} ${gf.hours} ; ${g.ground_mode} ${groundCost} ${g.ground_hours}${finalSuffix}`,
    ));
    nameMetaByName[name] = { key: "option.gatewayGround",
      params: { iata: g.iata, mode: itinerary.modeI18nParam(g.ground_mode) } };
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
      flightLegSpec(origin, g, gf, flyCost, date, ret),
      groundLegSpec(g, dest, groundCost, groundKm / Math.max(rtMult, 1)),
    ];
    appendFinal(name);
  });

  // origin-side splits (ground it from ORIGIN to a better-connected hub near origin, then fly
  // from there to dest) - mirrors server.py's plan(). Single-sided only: never combined with a
  // dest-side split in the same option.
  originGws.forEach((g, i) => {
    const gf = priced[1 + gws.length + i];
    const groundCost = trip.scaleLegCost(g.ground_mode, g.ground_cost, travelers) * rtMult;
    const groundKm = g.ferry
      ? g.ferry.crossing_km * rtMult
      : geo.haversineKm(origin.lat, origin.lng, g.lat, g.lng) * geo.ROAD_WINDING * rtMult;
    // the symmetric self-gateway case the dest-side loop above already guards - mirrors
    // server.py. discoverGateways() already filters this at the source; this is defense in
    // depth so a data slip can never resurrect a phantom same-airport "flight" leg.
    if (g.iata === dest.iata) {
      const name = `${g.ground_mode.charAt(0).toUpperCase()}${g.ground_mode.slice(1)} only to ${dest.iata}`;
      options.push(trip.parseOption(`${name} | ${g.ground_mode} ${groundCost} ${g.ground_hours}${finalSuffix}`));
      nameMetaByName[name] = { key: "option.groundOnlyTo",
        params: { iata: dest.iata, mode: itinerary.modeI18nParam(g.ground_mode) } };
      geoByName[name] = [
        { type: "ground", mode: g.ground_mode, from: pt(origin), to: pt(dest) },
      ];
      emissionsLegsByName[name] = [{ mode: g.ground_mode, road_km: groundKm }];
      legSpecsByName[name] = [
        groundLegSpec(g, dest, groundCost, groundKm / Math.max(rtMult, 1), origin),
      ];
      appendFinal(name);
      return;
    }
    const flyCost = flightCost(gf);
    const name = `${g.ground_mode.charAt(0).toUpperCase()}${g.ground_mode.slice(1)} to ${g.iata} + fly`;
    options.push(trip.parseOption(
      `${name} | ${g.ground_mode} ${groundCost} ${g.ground_hours} ; fly ${flyCost} ${gf.hours}${finalSuffix}`,
    ));
    nameMetaByName[name] = { key: "option.groundToHubFly",
      params: { iata: g.iata, mode: itinerary.modeI18nParam(g.ground_mode) } };
    geoByName[name] = [
      { type: "ground", mode: g.ground_mode, from: pt(origin), to: pt(g) },
      { type: "flight", from: pt(g), to: pt(dest) },
    ];
    const flyKm = geo.haversineKm(g.lat, g.lng, dest.lat, dest.lng) * rtMult;
    emissionsLegsByName[name] = [
      { mode: g.ground_mode, road_km: groundKm },
      { mode: "fly", distance_km: flyKm },
    ];
    legSpecsByName[name] = [
      groundLegSpec(g, g, groundCost, groundKm / Math.max(rtMult, 1), origin),
      flightLegSpec(g, dest, gf, flyCost, date, ret),
    ];
    appendFinal(name);
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
    const meta = nameMetaByName[o.name] || { key: null, params: {} };
    o.name_key = meta.key;
    o.name_params = meta.params;
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
  if ([...gws, ...originGws].some((g) => g.ferry)) {
    notes.push(note("notes.ferryRealCorridor"));
  }
  if ([...gws, ...originGws].some((g) => g.transit)) {
    notes.push(note("notes.transitLiveSchedule"));
  }
  if (skippedCloser) {
    notes.push(note("notes.airportClosedNearby", {
      closed_iata: skippedCloser.iata, closed_name: skippedCloser.name, used_iata: dest.iata,
    }));
  }
  if (final && final.possible) {
    notes.push(note("notes.finalLeg", {
      mode: itinerary.modeI18nParam(final.mode), iata: dest.iata, km: pyRound(final.distance_km),
    }));
  } else if (final && final.reason === "restricted_crossing") {
    notes.push(note("notes.groundCrossingRestricted", { iata: dest.iata, country: final.country || "" }));
  } else if (final !== null) {
    // any other refusal (open sea, no corridor) at dist > FINAL_LEG_MIN_KM: always say so, not
    // just past the old 120km threshold - mirrors server.py.
    notes.push(note("notes.lastMileGap", { iata: dest.iata, km: Math.trunc(dest.dist_km || 0) }));
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
    origin_gateways: originGws.map(gw),
    direct: df,
    result: clean,
    weather: null, // no OpenWeather key on Pages - the UI already treats a null weather block as "no data"
    notes,
  };
}
