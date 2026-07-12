// itinerary.js - faithful JS port of hopandhaul/itinerary.py's ESTIMATE-only path.
//
// The Pages build never has a Duffel key (no server, no CORS-able secret), so it only ever
// exercises itinerary.py's synthetic-clock branch - never the live-segment one. This file
// still accepts a `segments`/`isLive` leg shape (mirroring build_timeline's full signature) so
// it stays a faithful port instead of a silently-narrower one, but plan.js below never sets
// those fields. See tests/web_parity/ for how this is checked against the Python original - 
// it rides along inside plan()'s own output, no separate case type needed.
//
// Same honesty rules as itinerary.py (see that file's module docstring for the full
// reasoning): clock times are an EXAMPLE schedule, never invented flight numbers, no
// longitude-based timezone guessing.
import { pyRound } from "./pyround.js";
import { pyG } from "./geo.js";

export const AIRPORT_ARRIVAL_BUFFER_H = 2.0;
export const DEFAULT_DEPART_LOCAL = "08:00";
export const FLIGHT_MODES = new Set(["fly", "flight", "plane", "air"]);

// --------------------------------------------------------------------------- clock math
function hhmmToMin(s) {
  const [hh, mm] = s.split(":").map(Number);
  return hh * 60 + mm;
}

/** minutes (may be negative or span multiple days) -> ["HH:MM", dayOffset] - mirrors
 * itinerary._min_to_hhmm(), including Python's floor-division mod semantics for negative
 * input (JS's % is remainder, not modulo, so this needs an explicit floor-div). */
function minToHhmm(totalMin) {
  const day = Math.floor(totalMin / 1440);
  const rem = totalMin - day * 1440;
  const hh = Math.floor(rem / 60);
  const mm = rem % 60;
  return [`${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`, day];
}

function dayLabel(dayOffset, date) {
  if (date) {
    const d = new Date(`${date}T00:00:00Z`);
    if (!Number.isNaN(d.getTime())) {
      d.setUTCDate(d.getUTCDate() + dayOffset);
      return d.toISOString().slice(0, 10);
    }
  }
  return `Day ${dayOffset + 1}`;
}

function airportLabel(a) {
  return { iata: a?.iata ?? null, name: a?.name ?? null, city: a?.city ?? null };
}

// --------------------------------------------------------------------------- verify links
/** Deep link to check a flight leg's price against reality.
 * Format: https://www.google.com/travel/flights?q=Flights+from+XXX+to+YYY+on+YYYY-MM-DD */
export function googleFlightsLink(originIata, destIata, date = null) {
  let q = `Flights from ${originIata} to ${destIata}`;
  if (date) q += ` on ${date}`;
  return "https://www.google.com/travel/flights?" + new URLSearchParams({ q }).toString();
}

// Python's urllib.parse.quote(text, safe="-") only ever leaves ASCII letters/digits and
// "_.-~" unescaped - everything else (including UTF-8 multi-byte characters, and punctuation
// like "'" or "(" that plenty of real city names carry, e.g. "St. John's") gets percent-
// escaped, uppercase hex. JS's built-in encodeURIComponent() has a DIFFERENT safe set (it also
// leaves "!~*'()" unescaped) - close enough to look right by eye, but a byte-for-byte mismatch
// on exactly the punctuation real airport city names contain. This reimplements Python's exact
// safe set instead, so rome2rio_link() output matches _slug() byte for byte (see
// tests/web_parity/ - this rides inside build_timeline()'s verify_url output).
const PY_ALWAYS_SAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~";

function pyQuote(text) {
  const bytes = new TextEncoder().encode(text);
  let out = "";
  for (const b of bytes) {
    const ch = String.fromCharCode(b);
    out += (b < 128 && PY_ALWAYS_SAFE.includes(ch)) ? ch : "%" + b.toString(16).toUpperCase().padStart(2, "0");
  }
  return out;
}

function slug(text) {
  return pyQuote(text.trim().replace(/ /g, "-"));
}

/** Deep link to check a ground leg's price/time against reality.
 * Format: https://www.rome2rio.com/map/{from}/{to} */
export function rome2rioLink(fromPlace, toPlace) {
  return `https://www.rome2rio.com/map/${slug(fromPlace)}/${slug(toPlace)}`;
}

export function verifyLink(mode, origin, dest, date = null) {
  if (FLIGHT_MODES.has(mode)) return googleFlightsLink(origin.iata, dest.iata, date);
  const fromPlace = origin.city || origin.name || origin.iata;
  const toPlace = dest.city || dest.name || dest.iata;
  return rome2rioLink(fromPlace, toPlace);
}

// --------------------------------------------------------------------------- price provenance
/** 'where this number comes from' for an ESTIMATE flight leg - `detail` is geo.estimateFlight()'s
 * own return object. */
export function flightProvenanceEstimate(detail, date) {
  if (!detail) return "route-band estimate";
  const bits = [date ? `route-band estimate for ${date}` : "route-band estimate (no date given)"];
  if (detail.regions) bits.push(`${detail.regions} market ×${(detail.route_mult ?? 1.0).toFixed(2)}`);
  const an = detail.anchor;
  if (an) {
    // a REAL number rides along with the model: what this route's passengers actually paid
    const held = an.adjusted ? "estimate adjusted into that band" : "estimate already inside that band";
    bits.push(`real market check (BTS ${an.asof ?? ""}): avg paid $${pyG(an.fare_avg)}`
      + `, lowest-fare carrier $${pyG(an.fare_low)} — ${held}`);
  }
  if (detail.date_mult) bits.push(`date factor ×${detail.date_mult.toFixed(2)}`);
  if (detail.likely_connection) bits.push("fare priced assuming a connecting flight (small/remote airport)");
  return bits.join("; ");
}

export function flightProvenanceLive(live) {
  const carrier = live.carrier || "an airline";
  const bits = [`live fare — ${carrier}`];
  if (live.native_price != null && live.currency && live.currency !== "USD") {
    const conv = live.converted ? " (converted to USD, verify at booking)" : "";
    bits.push(`priced ${live.native_price} ${live.currency}${conv}`);
  }
  return bits.join("; ");
}

/** 'where this number comes from' for a REAL ferry-corridor leg - mirrors
 * itinerary.ferry_provenance: names the ports, operators, real fare band and frequency. */
export function ferryProvenance(ferry) {
  const ops = (ferry.operators || []).join(", ") || "operator n/a";
  const bits = [];
  const lo = ferry.price_usd_lo, hi = ferry.price_usd_hi;
  const asof = ferry.price_asof || "n/a";
  if (ferry.fare_is_real && lo != null) {
    const band = (hi != null && hi !== lo) ? `$${pyG(lo)}–$${pyG(hi)}` : `from $${pyG(lo)}`;
    bits.push(`real ferry fare ${band} (${ops}; as of ${asof})`);
  } else {
    bits.push(`ferry fare estimate (${ops})`);
  }
  bits.push(`${ferry.port_a} → ${ferry.port_b}, ~${pyG(ferry.duration_h)}h crossing`);
  const freq = ferry.frequency_per_day;
  if (freq) {
    bits.push(`~${pyG(freq)} sailings/day` + (ferry.seasonal ? ", seasonal service" : ""));
  } else if (ferry.seasonal) {
    bits.push("seasonal service");
  }
  if (ferry.access_cost != null) {
    bits.push(`+ ~$${pyG(ferry.access_cost)} airport–port transfer estimate`);
  }
  return bits.join("; ");
}

export function groundProvenance(gw, roadKm) {
  let base;
  if (gw.ferry) {
    base = ferryProvenance(gw.ferry);
  } else if (gw.source === "curated") {
    const note = gw.notes;
    base = `curated gateway estimate${note ? " — " + note : ""}`;
  } else {
    const km = roadKm != null ? `~${Math.trunc(roadKm)}km` : "distance-based";
    base = `ground estimate (${km} road/rail distance, regional rate table)`;
  }
  const tr = gw.transit;
  if (tr && tr.line) base = `${base}; ${tr.line}`;
  return base;
}

// --------------------------------------------------------------------------- timeline builder
/**
 * legs: ordered leg specs, one per trip.js leg, each:
 *   {mode, cost, hours, from, to, price_basis, verify_url, is_live, segments}
 * Returns {legs, any_live, example_day, depart_local} - mirrors itinerary.build_timeline()
 * exactly, including its "clock math reconciles with hours_eff" invariant (see the Python
 * docstring for why the transfer buffer, not the airport-arrival buffer, drives the gap
 * between legs).
 */
export function buildTimeline(legs, {
  date = null, departLocal = DEFAULT_DEPART_LOCAL,
  transferBufferH = 1.0, airportBufferH = AIRPORT_ARRIVAL_BUFFER_H,
} = {}) {
  if (!legs.length) return { legs: [], any_live: false, example_day: true, depart_local: departLocal };

  const rows = [];
  let clockMin = hhmmToMin(departLocal);
  let anyLive = false;

  legs.forEach((leg, i) => {
    if (i > 0) clockMin += Math.round(transferBufferH * 60);

    const segments = leg.is_live ? leg.segments : null;
    if (segments && segments.length) {
      anyLive = true;
      const [legRows, resync] = liveSegmentsToRows(leg, segments, date, i === 0, airportBufferH);
      rows.push(...legRows);
      clockMin = resync;
      return;
    }

    const isFlight = FLIGHT_MODES.has(leg.mode);
    const [departClock, depDay] = minToHhmm(clockMin);
    let checkinBy = null;
    if (isFlight) {
      const [checkinClock, checkinDay] = minToHhmm(clockMin - Math.round(airportBufferH * 60));
      checkinBy = { clock: checkinClock, day: dayLabel(checkinDay, date) };
    }
    const arriveMin = clockMin + Math.round(leg.hours * 60);
    const [arriveClock, arrDay] = minToHhmm(arriveMin);
    rows.push({
      mode: leg.mode,
      from: airportLabel(leg.from),
      to: airportLabel(leg.to),
      depart_clock: departClock, depart_day: dayLabel(depDay, date),
      arrive_clock: arriveClock, arrive_day: dayLabel(arrDay, date),
      duration_h: pyRound(leg.hours, 2),
      checkin_by: checkinBy,
      cost: leg.cost,
      price_basis: leg.price_basis,
      verify_url: leg.verify_url,
      is_live: false,
      carrier: null,
      flight_number: null,
    });
    clockMin = arriveMin;
  });

  return { legs: rows, any_live: anyLive, example_day: rows.some((r) => !r.is_live), depart_local: departLocal };
}

function liveSegmentsToRows(leg, segments, date, addCheckin, airportBufferH) {
  const rows = [];
  let lastArriveMin = null;
  segments.forEach((seg, idx) => {
    const depDt = seg.depart_at;
    const arrDt = seg.arrive_at;
    const depDay = date ? daysBetween(date, depDt) : 0;
    const arrDay = date ? daysBetween(date, arrDt) : 0;
    let checkinBy = null;
    if (addCheckin && idx === 0) {
      const checkinDt = new Date(depDt.getTime() - airportBufferH * 3600 * 1000);
      const checkinDay = date ? daysBetween(date, checkinDt) : 0;
      checkinBy = { clock: hhmm(checkinDt), day: dayLabel(checkinDay, date) };
    }
    rows.push({
      mode: "fly",
      from: airportLabel(seg.from),
      to: airportLabel(seg.to),
      depart_clock: hhmm(depDt), depart_day: dayLabel(depDay, date),
      arrive_clock: hhmm(arrDt), arrive_day: dayLabel(arrDay, date),
      duration_h: pyRound((arrDt.getTime() - depDt.getTime()) / 3600000, 2),
      checkin_by: checkinBy,
      cost: idx === 0 ? leg.cost : 0.0,
      price_basis: leg.price_basis,
      verify_url: leg.verify_url,
      is_live: true,
      carrier: seg.carrier ?? null,
      flight_number: seg.flight_number ?? null,
    });
    lastArriveMin = arrDay * 1440 + hhmmToMin(hhmm(arrDt));
  });
  return [rows, lastArriveMin];
}

function hhmm(d) {
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function daysBetween(dateStr, d) {
  const anchor = new Date(`${dateStr}T00:00:00`);
  const target = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  return Math.round((target - anchor) / 86400000);
}
