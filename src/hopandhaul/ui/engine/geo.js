// geo.js - faithful JS port of hopandhaul/geo.py's estimate path (no network).
//
// This mirrors geo.py function-for-function, constant-for-constant, including operation
// order, so it stays bit-for-bit identical to the Python engine after rounding - see
// tests/web_parity/. If you're changing behavior here, change geo.py first and port the
// change back; this file should never drift ahead of the Python original.
//
// Pure ESM, no dependencies beyond ./data.js (airport/gateway lookups) and ./pyround.js
// (Python round() semantics).
import { pyRound } from "./pyround.js";
import {
  airports, gatewaysDb, byIata, ferryCorridors, landgrid, fareAnchors, fareAnchorsAsof,
} from "./data.js";

export { byIata };

/** Python "%g" formatting for the numbers this engine narrates (fares, hours, sailing
 * frequencies): 6 significant digits, trailing zeros stripped - matches f"{x:g}". */
export function pyG(x) {
  return String(Number(Number(x).toPrecision(6)));
}

// ---- fare model (ESTIMATE) -------------------------------------------------------------
export const FLIGHT_CURVE = [30.0, 1.8, 0.012]; // (base, per_sqrt_km, per_km)
export const FLIGHT_FLOOR = 45.0;
export const NA_SHORT_FLOOR = 65.0;
export const SMALL_AIRPORT_PREMIUM = { 1: 0.0, 2: 0.18, 3: 0.75 };
export const HUB_COMPETITION_DISCOUNT = 0.92;
export const FLIGHT_FIXED_H = 1.1;
export const CONNECTION_H = 1.6;

// Route-market multiplier by (region, region) pair, pre-sorted alphabetically - mirrors
// geo.py's ROUTE_MULT dict exactly (including its "keys must be pre-sorted" invariant, which
// this module also enforces below at load time, matching geo.py's module-level assert).
const ROUTE_MULT_ENTRIES = [
  [["EU", "EU"], 0.55],
  [["SEA", "SEA"], 0.55],
  [["IN", "IN"], 0.55],
  [["KR", "KR"], 0.60], [["CN", "CN"], 0.75], [["JP", "JP"], 0.85],
  [["NA", "NA"], 1.00], [["LATAM", "LATAM"], 0.95], [["AU", "AU"], 0.90],
  [["ME", "ME"], 0.85], [["ZA", "ZA"], 0.80], [["AF", "AF"], 1.60],
  [["RU", "RU"], 1.20],
  [["EU", "NA"], 0.85],
  [["EU", "ME"], 0.80], [["EU", "IN"], 0.90], [["CN", "EU"], 0.90],
  [["EU", "JP"], 0.90], [["EU", "SEA"], 0.90], [["EU", "LATAM"], 0.95],
  [["AF", "EU"], 0.90],
  [["AF", "NA"], 1.15], [["AF", "ME"], 1.00], [["AF", "ZA"], 1.15],
  [["JP", "KR"], 0.80], [["KR", "SEA"], 0.85], [["JP", "SEA"], 0.80],
  [["CN", "SEA"], 0.85], [["CN", "JP"], 0.90], [["CN", "KR"], 0.90],
  [["IN", "SEA"], 0.80], [["IN", "ME"], 0.85], [["ME", "SEA"], 0.90],
  [["JP", "NA"], 1.25], [["CN", "NA"], 1.30], [["AU", "NA"], 1.05],
  [["LATAM", "NA"], 0.95], [["AU", "SEA"], 0.85],
  [["EU", "RU"], 0.95], [["CN", "RU"], 0.95], [["RU", "SEA"], 1.05],
];
const ROUTE_MULT = new Map();
for (const [pair, mult] of ROUTE_MULT_ENTRIES) {
  const sorted = [...pair].sort();
  if (sorted[0] !== pair[0] || sorted[1] !== pair[1]) {
    throw new Error(`ROUTE_MULT key not pre-sorted: ${pair.join(",")}`); // regression guard, mirrors geo.py's assert
  }
  ROUTE_MULT.set(sorted.join(","), mult);
}

export function _routeMult(regionA, regionB) {
  const key = [regionA, regionB].sort().join(",");
  if (ROUTE_MULT.has(key)) return ROUTE_MULT.get(key);
  if (regionA === "AF" || regionB === "AF") return 1.15;
  return 1.0;
}

// lead-time booking curve: [max_days_out, fare multiplier]
const LEAD_CURVE = [
  [3, 1.45], [6, 1.30], [13, 1.18], [20, 1.08], [45, 1.00],
  [90, 0.96], [180, 1.00], [99999, 1.05],
];
const MONTH_MULT = {
  1: 0.92, 2: 0.92, 3: 1.03, 4: 1.00, 5: 1.00, 6: 1.10,
  7: 1.12, 8: 1.08, 9: 0.93, 10: 0.93, 11: 0.97, 12: 1.15,
};
const DOW_MULT = { 0: 1.00, 1: 0.96, 2: 0.96, 3: 1.00, 4: 1.06, 5: 1.00, 6: 1.06 }; // Mon..Sun
const DATE_MULT_CLAMP = [0.75, 1.75];

function _flightSpeedKmh(d) {
  return d < 1500 ? 700.0 : (d < 6000 ? 800.0 : 850.0);
}

// ---- ground model (ESTIMATE) -------------------------------------------------------------
export const ROAD_WINDING = 1.2;
const GROUND = { // mode -> [speed_kmh, base_$, per_km_$]
  drive: [85, 15, 0.20], car: [85, 15, 0.20], rental: [85, 20, 0.20],
  bus: [70, 4, 0.08], coach: [70, 4, 0.08], shuttle: [65, 6, 0.15],
  train: [110, 6, 0.12], rail: [110, 6, 0.12], ferry: [40, 10, 0.25],
  ground: [80, 8, 0.15],
};
const REGION_GROUND = {
  EU: { train: [150, 8, 0.15], rail: [150, 8, 0.15], bus: [75, 5, 0.06],
    drive: [90, 20, 0.26], car: [90, 20, 0.26], rental: [90, 25, 0.26] },
  NA: { train: [95, 8, 0.15], rail: [95, 8, 0.15], bus: [80, 5, 0.09],
    drive: [90, 15, 0.20], car: [90, 15, 0.20], rental: [90, 22, 0.20] },
  JP: { train: [190, 10, 0.22], rail: [190, 10, 0.22], bus: [70, 8, 0.10] },
  KR: { train: [150, 5, 0.09], rail: [150, 5, 0.09], bus: [80, 4, 0.05] },
  CN: { train: [200, 5, 0.075], rail: [200, 5, 0.075], bus: [65, 4, 0.05] },
  IN: { train: [55, 3, 0.03], rail: [55, 3, 0.03], bus: [55, 3, 0.04] },
  SEA: { train: [60, 3, 0.04], rail: [60, 3, 0.04], bus: [65, 3, 0.045] },
  LATAM: { bus: [75, 3, 0.05], train: [70, 5, 0.08], rail: [70, 5, 0.08] },
  AU: { train: [100, 8, 0.13], rail: [100, 8, 0.13], drive: [90, 18, 0.22] },
  ME: { bus: [80, 4, 0.05], drive: [100, 15, 0.15], car: [100, 15, 0.15] },
  ZA: { bus: [85, 4, 0.06], drive: [95, 15, 0.18], car: [95, 15, 0.18] },
  AF: { bus: [60, 3, 0.05], train: [50, 4, 0.05], rail: [50, 4, 0.05],
    drive: [70, 15, 0.22], car: [70, 15, 0.22] },
  RU: { train: [70, 6, 0.06], rail: [70, 6, 0.06], bus: [60, 5, 0.06],
    drive: [75, 15, 0.18], car: [75, 15, 0.18] },
};
const GROUND_ACCESS_H = 0.3;

// Distance breakpoints for the default drive -> train -> bus progression, by region group.
// An array of {regions, breaks} in the same order geo.py's dict literal is written, since
// _mode_breaks() does a first-match scan (dict iteration order in Python 3.7+ == insertion
// order == literal order).
const MODE_BREAKS = [
  { regions: ["EU", "JP", "CN", "KR"], breaks: [[80, "drive"], [650, "train"], [null, "bus"]] },
  { regions: ["NA"], breaks: [[250, "drive"], [null, "bus"]] },
  { regions: ["LATAM", "ME", "ZA", "AF"], breaks: [[120, "drive"], [null, "bus"]] },
  { regions: ["OTHER"], breaks: [[120, "drive"], [450, "train"], [null, "bus"]] },
];
const MODE_TIE_BAND_KM = 25;
const SHADOW_VOT_USD_PER_HOUR = 15.0;

function _modeBreaks(region) {
  for (const entry of MODE_BREAKS) {
    if (entry.regions.includes(region)) return entry.breaks;
  }
  return MODE_BREAKS[MODE_BREAKS.length - 1].breaks; // ("OTHER",) fallback
}

// --------------------------------------------------------------------------- geometry
const DEG2RAD = Math.PI / 180;

export function haversineKm(lat1, lng1, lat2, lng2) {
  const r = 6371.0;
  const p1 = lat1 * DEG2RAD, p2 = lat2 * DEG2RAD;
  const dphi = (lat2 - lat1) * DEG2RAD;
  const dlam = (lng2 - lng1) * DEG2RAD;
  const a = Math.sin(dphi / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dlam / 2) ** 2;
  return pyRound(2 * r * Math.asin(Math.min(1.0, Math.sqrt(a))), 1);
}

export const NEAREST_SOFT_KM = 120;
export const NEAREST_WARN_KM = 400;
export const NEAREST_HARD_KM = 700;

/** Closest airport to a point, capped at maxKm - mirrors geo.nearest_airport exactly,
 * including its "first minimal wins" tie-break (relies on airports() iteration order matching
 * the Python side's, which it does: both read the same airports.json in file order). */
export function nearestAirport(lat, lng, { preferHub = false, maxKm = NEAREST_HARD_KM } = {}) {
  let best = null;
  let bestScore = null;
  for (const a of airports()) {
    const d = haversineKm(lat, lng, a.lat, a.lng);
    if (maxKm !== null && d > maxKm) continue;
    const score = preferHub ? d + (a.hub - 1) * 20 : d;
    if (bestScore === null || score < bestScore) {
      best = a;
      bestScore = score;
    }
  }
  if (best === null) return null;
  const distKm = haversineKm(lat, lng, best.lat, best.lng);
  const warnTier = distKm >= NEAREST_WARN_KM ? "hard" : (distKm >= NEAREST_SOFT_KM ? "soft" : null);
  return { ...best, dist_km: distKm, warn_tier: warnTier };
}

// --------------------------------------------------------------------------- regions
/** Coarse region for ground-transport quality/cost and route-market pricing - mirrors
 * geo.region_of's if/elif chain exactly, including its comments on why order matters. */
export function regionOf(lat, lng) {
  if (lat >= 41 && lat <= 82 && lng >= 41 && lng <= 180) return "RU";
  if (lat >= 41 && lat <= 82 && lng >= -180 && lng <= -169) return "RU";
  if (lat >= 55 && lat <= 82 && lng >= 30 && lng < 41) return "RU";
  if (lat >= 33 && lat <= 39.5 && lng >= 124.5 && lng < 129.4) return "KR";
  if (lat >= 30 && lat <= 46 && lng >= 129.4 && lng <= 146) return "JP";
  if (lat >= 18 && lat <= 54 && lng >= 97 && lng <= 127) return "CN";
  if (lat >= 5 && lat <= 33 && lng >= 60 && lng <= 93) return "IN";
  if (lat >= -11 && lat <= 25 && lng >= 93 && lng <= 142) return "SEA";
  if (lat >= 34 && lat < 36 && lng >= 12 && lng <= 36) return "EU";
  if (lat >= 12 && lat <= 42 && lng >= 33 && lng <= 63) return "ME";
  if (lat >= 27 && lat <= 35.95 && lng >= -13 && lng <= -1.5) return "AF";
  if (lat >= 21 && lat <= 31.8 && lng >= 24 && lng < 33) return "AF";
  if ((lat >= 36 && lat <= 72 && lng >= -11 && lng <= 32) || (lat >= 27 && lat <= 40 && lng >= -32 && lng < -13)) return "EU";
  if (lat >= 62 && lat <= 67.5 && lng >= -25 && lng <= -13) return "EU";
  if (lat >= -59 && lat <= -44 && lng >= -73 && lng <= -56) return "LATAM";
  if (lat >= 58 && lat <= 84 && lng >= -75 && lng <= -10) return "EU";
  if (lat >= 18 && lat <= 72 && lng >= -170 && lng <= -52) return "NA";
  if (lat >= -56 && lat < 24 && lng >= -120 && lng <= -30) return "LATAM";
  if (lat >= -48 && lat <= -9 && lng >= 112 && lng <= 180) return "AU";
  if (lat >= -35 && lat <= -22 && lng >= 16 && lng <= 33) return "ZA";
  if (lat >= -35 && lat <= 37 && lng >= -18 && lng <= 52) return "AF";
  if (lat >= -30 && lat <= 24 && ((lng >= -180 && lng <= -134) || (lng >= 130 && lng <= 180))) return "AU";
  return "OTHER";
}

// --------------------------------------------------------------------------- estimates
function pad2(n) {
  return String(n).padStart(2, "0");
}

/** Strict YYYY-MM-DD parse + real-calendar-date validation. Returns {y,m,d} or null - 
 * mirrors what datetime.date.fromisoformat()/date() raise ValueError on. */
function parseIsoDate(dateStr) {
  const s = String(dateStr);
  if (s.length !== 10 || s[4] !== "-" || s[7] !== "-") return null;
  const y = Number(s.slice(0, 4)), m = Number(s.slice(5, 7)), d = Number(s.slice(8, 10));
  if (!Number.isInteger(y) || !Number.isInteger(m) || !Number.isInteger(d)) return null;
  const dt = new Date(Date.UTC(y, m - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== m - 1 || dt.getUTCDate() !== d) return null;
  return { y, m, d };
}

/** Today as a pure {y,m,d} calendar date in the LOCAL timezone - matches Python's
 * datetime.date.today(), which is also local-timezone. */
function localTodayYMD() {
  const now = new Date();
  return { y: now.getFullYear(), m: now.getMonth() + 1, d: now.getDate() };
}

function ymdUtcMs({ y, m, d }) {
  return Date.UTC(y, m - 1, d);
}

function cmpYMD(a, b) {
  return ymdUtcMs(a) - ymdUtcMs(b);
}

export function isPastDate(dateStr, today = null) {
  if (!dateStr) return false;
  const d = parseIsoDate(dateStr);
  if (!d) return false;
  return cmpYMD(d, today || localTodayYMD()) < 0;
}

export function fareDateMultiplier(dateStr, today = null) {
  if (!dateStr || isPastDate(dateStr, today)) return 1.0;
  const d = parseIsoDate(dateStr);
  if (!d) return 1.0;
  const t = today || localTodayYMD();
  const daysOut = Math.round((ymdUtcMs(d) - ymdUtcMs(t)) / 86400000);
  let lead = 1.0;
  for (const [maxDays, mult] of LEAD_CURVE) {
    if (daysOut <= maxDays) { lead = mult; break; }
  }
  const pyWeekday = (new Date(ymdUtcMs(d)).getUTCDay() + 6) % 7; // Mon=0..Sun=6, matches date.weekday()
  const m = lead * (MONTH_MULT[d.m] ?? 1.0) * (DOW_MULT[pyWeekday] ?? 1.0);
  const [lo, hi] = DATE_MULT_CLAMP;
  return pyRound(Math.max(lo, Math.min(hi, m)), 3);
}
void pad2; // reserved for future date formatting; keeps lint quiet if unused in a given build

// US fare anchoring - mirrors geo.py: the anchor BOUNDS the curve using the real BTS band
// and rides along in the output for provenance.
export const ANCHOR_MATCH_KM = 60.0;
export const ANCHOR_LO_FRAC = 0.45;
export const ANCHOR_HI_FRAC = 1.00;

/** The busiest real BTS city-pair market covering these two airports, or null - 
 * mirrors geo.fare_anchor_for, including its "first orientation match, then break" scan
 * and its strictly-greater pax_day tie-break (ties keep the earlier anchor). */
export function fareAnchorFor(orig, dest) {
  let best = null;
  for (const an of fareAnchors()) {
    for (const [p, q] of [[an.a, an.b], [an.b, an.a]]) {
      if (haversineKm(orig.lat, orig.lng, p[0], p[1]) <= ANCHOR_MATCH_KM
          && haversineKm(dest.lat, dest.lng, q[0], q[1]) <= ANCHOR_MATCH_KM) {
        if (best === null || an.pax_day > best.pax_day) best = an;
        break;
      }
    }
  }
  return best;
}

export function estimateFlight(orig, dest, date = null, today = null) {
  const d = haversineKm(orig.lat, orig.lng, dest.lat, dest.lng);
  const [base, perSqrt, perKm] = FLIGHT_CURVE;
  let fare = base + perSqrt * Math.sqrt(d) + perKm * d;
  fare *= 1 + (SMALL_AIRPORT_PREMIUM[dest.hub] ?? 0.0);
  fare *= 1 + 0.5 * (SMALL_AIRPORT_PREMIUM[orig.hub] ?? 0.0);
  if (orig.hub === 1 && dest.hub === 1) fare *= HUB_COMPETITION_DISCOUNT;
  const rO = regionOf(orig.lat, orig.lng);
  const rD = regionOf(dest.lat, dest.lng);
  const rm = _routeMult(rO, rD);
  fare *= rm;

  let anchor = null;
  let anchorAdjusted = false;
  if (rO === "NA" && rD === "NA") {
    anchor = fareAnchorFor(orig, dest);
    if (anchor) {
      const lo = ANCHOR_LO_FRAC * anchor.fare_low;
      const hi = ANCHOR_HI_FRAC * anchor.fare_low;
      const clamped = Math.max(lo, Math.min(hi, fare));
      anchorAdjusted = Math.abs(clamped - fare) >= 0.5;
      fare = clamped;
    }
  }

  const dm = fareDateMultiplier(date, today);
  fare *= dm;
  const floor = (rO === rD && rO === "NA" && d < 400) ? NA_SHORT_FLOOR : FLIGHT_FLOOR;
  fare = Math.max(floor, fare);
  let hours = FLIGHT_FIXED_H + d / _flightSpeedKmh(d);
  const connects = d > 2000 && (orig.hub === 3 || dest.hub === 3);
  if (connects) hours += CONNECTION_H;
  const out = {
    price: pyRound(fare / 5) * 5,
    hours: pyRound(hours, 1),
    distance_km: d,
    source: "estimate",
    route_mult: rm,
    regions: `${rO}-${rD}`,
    likely_connection: connects,
  };
  if (anchor) {
    out.anchor = {
      fare_avg: anchor.fare_avg, fare_low: anchor.fare_low,
      pax_day: anchor.pax_day, asof: fareAnchorsAsof(), adjusted: anchorAdjusted,
    };
  }
  if (dm !== 1.0) out.date_mult = dm;
  if (isPastDate(date, today)) out.past_date = true;
  return out;
}

function _groundLeg(distKm, mode, region) {
  const table = REGION_GROUND[region] || {};
  const tuple = table[mode] || GROUND[mode] || GROUND.ground;
  const [speed, base, perKm] = tuple;
  const roadKm = distKm * ROAD_WINDING;
  const hours = roadKm / speed + GROUND_ACCESS_H;
  const cost = base + perKm * roadKm;
  return { cost, hours, road_km: roadKm };
}

function _score(distKm, mode, region) {
  const leg = _groundLeg(distKm, mode, region);
  return leg.cost + leg.hours * SHADOW_VOT_USD_PER_HOUR;
}

export function pickGroundMode(distKm, region = "OTHER") {
  const breaks = _modeBreaks(region);
  const limits = breaks.filter((b) => b[0] !== null).map((b) => b[0]);
  const modesInOrder = breaks.map((b) => b[1]);
  for (let i = 0; i < limits.length; i++) {
    const limit = limits[i];
    if (distKm <= limit + MODE_TIE_BAND_KM) {
      if (distKm > limit - MODE_TIE_BAND_KM) {
        const a = modesInOrder[i], b = modesInOrder[i + 1];
        return _score(distKm, a, region) <= _score(distKm, b, region) ? a : b;
      }
      return modesInOrder[i];
    }
  }
  return modesInOrder[modesInOrder.length - 1];
}

export function estimateGround(distKm, mode, region = "OTHER") {
  mode = mode.toLowerCase();
  const leg = _groundLeg(distKm, mode, region);
  const cost = pyRound(leg.cost / 5) * 5;
  const hours = pyRound(leg.hours, 1);
  return { cost: Math.max(5, cost), hours, mode, road_km: pyRound(leg.road_km, 1) };
}

// --------------------------------------------------------------------------- landmasses
const ISLAND_LANDMASS = {
  // Mediterranean
  JTR: "santorini", JMK: "mykonos", PAS: "paros", CFU: "corfu", ZTH: "zakynthos",
  EFL: "kefalonia", JSI: "skiathos", HER: "crete", CHQ: "crete", JSH: "crete",
  RHO: "rhodes", KGS: "kos", MJT: "lesbos", SMI: "samos", JNX: "naxos",
  MLA: "malta", LCA: "cyprus", PFO: "cyprus",
  PMO: "sicily", CTA: "sicily", TPS: "sicily",
  CAG: "sardinia", OLB: "sardinia", AHO: "sardinia", AJA: "corsica", BIA: "corsica",
  PMI: "mallorca", IBZ: "ibiza", MAH: "menorca",
  // Atlantic
  TFS: "tenerife", TFN: "tenerife", LPA: "grancanaria", ACE: "lanzarote",
  FUE: "fuerteventura", SPC: "lapalma", FNC: "madeira", PDL: "azores",
  KEF: "iceland", FAE: "faroe", SID: "capeverde",
  DUB: "ireland", ORK: "ireland", SNN: "ireland", NOC: "ireland",
  BFS: "ireland", BHD: "ireland",
  JER: "jersey", GCI: "guernsey", IOM: "isleofman",
  LSI: "shetland", KOI: "orkney",
  MHQ: "aland", VBY: "gotland", RNN: "bornholm",
  YYT: "newfoundland",
  // Caribbean + nearby
  HAV: "cuba", VRA: "cuba", PUJ: "hispaniola", SDQ: "hispaniola",
  POP: "hispaniola", STI: "hispaniola", PAP: "hispaniola",
  SJU: "puertorico", BQN: "puertorico", PSE: "puertorico",
  STT: "stthomas", STX: "stcroix", SXM: "stmartin", SBH: "stbarts",
  AUA: "aruba", CUR: "curacao", BON: "bonaire", MBJ: "jamaica", KIN: "jamaica",
  GCM: "cayman", BGI: "barbados", ANU: "antigua", SKB: "stkitts", EIS: "tortola",
  UVF: "stlucia", GND: "grenada", POS: "trinidad", FDF: "martinique",
  PTP: "guadeloupe", NAS: "newprovidence", FPO: "grandbahama", GGT: "exuma",
  PLS: "provo", CZM: "cozumel", RTB: "roatan", ADZ: "sanandres", BZE: "mainland",
  // Indian Ocean / Africa
  ZNZ: "zanzibar", MRU: "mauritius", RUN: "reunion", SEZ: "seychelles",
  TNR: "madagascar", CMB: "srilanka", MLE: "maldives",
  // Asia-Pacific
  CJU: "jeju", OKA: "okinawa", TPE: "taiwan", KHH: "taiwan", SYX: "hainan",
  HKG: "mainland", MFM: "mainland", SIN: "mainland",
  PEN: "mainland", HKT: "mainland",
  LGK: "langkawi", USM: "samui", PQC: "phuquoc",
  BKI: "borneo", BWN: "borneo",
  CGK: "java", YIA: "java", SUB: "java", KNO: "sumatra", DPS: "bali",
  LOP: "lombok", LBJ: "flores",
  MNL: "luzon", CRK: "luzon", CEB: "cebu", KLO: "panay", MPH: "panay",
  TAG: "bohol", PPS: "palawan",
  HNL: "oahu", OGG: "maui", KOA: "hawaiibig", ITO: "hawaiibig", LIH: "kauai",
  AKL: "nznorth", WLG: "nznorth", CHC: "nzsouth", ZQN: "nzsouth",
  HBA: "tasmania", HTI: "whitsundays",
  YYJ: "vancouverisland", YWH: "vancouverisland", YCD: "vancouverisland",
  YQQ: "vancouverisland", FRD: "sanjuan", ESD: "sanjuan",
  NAN: "fiji", PPT: "tahiti", BOB: "borabora", RAR: "rarotonga", GUM: "guam",
  YGR: "magdalen",
  IPC: "easterisland", ACK: "nantucket", MVY: "marthasvineyard",
  JNU: "juneau", KTN: "ketchikan",
};
const _CONTINENT = {
  NA: "americas", LATAM: "americas", EU: "eurasia", ME: "eurasia",
  CN: "eurasia", IN: "eurasia", SEA: "eurasia", KR: "korea",
  JP: "japan", AU: "australia", AF: "africa", ZA: "africa",
};

export function landmassOf(a) {
  const lm = ISLAND_LANDMASS[a.iata];
  if (lm && lm !== "mainland") return lm;
  return _CONTINENT[regionOf(a.lat, a.lng)] || "other";
}

// --------------------------------------------------------------------------- open water
// Faithful port of geo.py's sea-gap layer: the packed land grid, great-circle sampling,
// offset-path rescue, and the fixed-links table. Every sampled coordinate is rounded to 4
// decimals BEFORE the grid lookup on both sides, so trig ULP differences can't flip a cell.
export const WATER_RUN_MIN_KM = 22.0;
export const WATER_RESCUE_RUN_KM = 11.0;
export const WATER_OFFSET_KM = 40.0;
export const FIXED_LINK_NEAR_KM = 120.0;

export const FIXED_LINKS = [
  ["Channel Tunnel", 51.01, 1.50],
  ["Oresund Bridge", 55.57, 12.85],
  ["Great Belt Bridge", 55.34, 11.03],
  ["Seikan Tunnel", 41.30, 140.30],
  ["Osman Gazi Bridge", 40.75, 29.51],
  ["Canakkale 1915 Bridge", 40.31, 26.45],
  ["HK-Zhuhai-Macau Bridge", 22.30, 113.75],
  ["Overseas Highway (Florida Keys)", 24.75, -81.02],
  ["Chesapeake Bay Bridge-Tunnel", 37.03, -76.08],
  ["Confederation Bridge (PEI)", 46.22, -63.75],
  ["King Fahd Causeway", 26.18, 50.26],
  ["Penang Bridge", 5.35, 100.35],
  ["Seto-Ohashi Bridge", 34.40, 133.81],
  ["Akashi Kaikyo Bridge", 34.62, 135.02],
  ["Rio-Niteroi Bridge", -22.87, -43.16],
];

export function isLand(lat, lng) {
  const g = landgrid();
  const row = Math.min(g.h - 1, Math.max(0, Math.floor((90.0 - lat) / g.res)));
  const col = Math.min(g.w - 1, Math.max(0, Math.floor((lng + 180.0) / g.res)));
  const i = row * g.w + col;
  return (g.bits[i >> 3] & (0x80 >> (i & 7))) !== 0;
}

function _pathPoints(lat1, lng1, lat2, lng2, n) {
  const phi1 = lat1 * DEG2RAD, lam1 = lng1 * DEG2RAD;
  const phi2 = lat2 * DEG2RAD, lam2 = lng2 * DEG2RAD;
  const v1 = [Math.cos(phi1) * Math.cos(lam1), Math.cos(phi1) * Math.sin(lam1), Math.sin(phi1)];
  const v2 = [Math.cos(phi2) * Math.cos(lam2), Math.cos(phi2) * Math.sin(lam2), Math.sin(phi2)];
  const dot = Math.max(-1.0, Math.min(1.0, v1[0] * v2[0] + v1[1] * v2[1] + v1[2] * v2[2]));
  const om = Math.acos(dot);
  const pts = [];
  for (let k = 0; k <= n; k++) {
    const t = k / n;
    let a, b;
    if (om < 1e-9) { a = 1 - t; b = t; } else {
      a = Math.sin((1 - t) * om) / Math.sin(om);
      b = Math.sin(t * om) / Math.sin(om);
    }
    const x = a * v1[0] + b * v2[0], y = a * v1[1] + b * v2[1], z = a * v1[2] + b * v2[2];
    pts.push([
      pyRound(Math.atan2(z, Math.sqrt(x * x + y * y)) / DEG2RAD, 4),
      pyRound(Math.atan2(y, x) / DEG2RAD, 4),
    ]);
  }
  return pts;
}

export function waterPathStats(lat1, lng1, lat2, lng2) {
  const d = haversineKm(lat1, lng1, lat2, lng2);
  const n = Math.max(8, Math.min(96, Math.floor(d / 12) + 1));
  const pts = _pathPoints(lat1, lng1, lat2, lng2, n);
  const step = d / n;
  let fracN = 0;
  let run = 0.0, best = 0.0;
  let runStart = null;
  let mid = null;
  for (let i = 0; i < pts.length; i++) {
    const [la, ln] = pts[i];
    if (isLand(la, ln)) { run = 0.0; continue; }
    fracN += 1;
    if (run === 0.0) runStart = i;
    run += step;
    if (run > best) {
      best = run;
      mid = pts[Math.floor((runStart + i) / 2)];
    }
  }
  return {
    water_frac: pyRound(fracN / pts.length, 4),
    max_run_km: pyRound(best, 1),
    run_mid: mid,
    dist_km: d,
  };
}

function _bearingRad(lat1, lng1, lat2, lng2) {
  const p1 = lat1 * DEG2RAD, p2 = lat2 * DEG2RAD;
  const dl = (lng2 - lng1) * DEG2RAD;
  return Math.atan2(Math.sin(dl) * Math.cos(p2),
    Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl));
}

function _offsetPoint(lat, lng, thetaRad, distKm) {
  const d = distKm / 6371.0;
  const p1 = lat * DEG2RAD, l1 = lng * DEG2RAD;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(thetaRad));
  const l2 = l1 + Math.atan2(Math.sin(thetaRad) * Math.sin(d) * Math.cos(p1),
    Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  // Python's % is a true modulo (result sign follows the divisor); JS's is a remainder.
  // (x % 360 + 360) % 360 reproduces Python's behavior for the wrap below.
  const lng2 = ((((l2 / DEG2RAD) + 540.0) % 360.0 + 360.0) % 360.0) - 180.0;
  return [pyRound(p2 / DEG2RAD, 4), pyRound(lng2, 4)];
}

/** True when open sea genuinely separates two points and no land detour plausibly exists - 
 * mirrors geo.sea_gap: direct-run trigger, tight offset-path rescue, fixed-link rescue. */
export function seaGap(a, b) {
  const stats = waterPathStats(a.lat, a.lng, b.lat, b.lng);
  if (stats.max_run_km < WATER_RUN_MIN_KM) return false;
  const theta = _bearingRad(a.lat, a.lng, b.lat, b.lng);
  for (const side of [1.0, -1.0]) {
    const o1 = _offsetPoint(a.lat, a.lng, theta + side * Math.PI / 2, WATER_OFFSET_KM);
    const o2 = _offsetPoint(b.lat, b.lng, theta + side * Math.PI / 2, WATER_OFFSET_KM);
    if (waterPathStats(o1[0], o1[1], o2[0], o2[1]).max_run_km < WATER_RESCUE_RUN_KM) return false;
  }
  const mid = stats.run_mid;
  if (mid) {
    for (const [, llat, llng] of FIXED_LINKS) {
      if (haversineKm(mid[0], mid[1], llat, llng) <= FIXED_LINK_NEAR_KM) return false;
    }
  }
  return true;
}

// --------------------------------------------------------------------------- ferry corridors
export const PORT_MATCH_KM = 60.0;
export const CROSSING_DOMINANT = 0.5;
export const MIN_FERRY_FREQ_PER_DAY = 0.65;
export const FERRY_BOARDING_H = 0.5;
export const ACCESS_WATER_RUN_KM = 15.0;

const _portLandmassCache = new Map();

function _portLandmass(port) {
  const key = `${port.lat},${port.lng}`;
  let lm = _portLandmassCache.get(key);
  if (lm === undefined) {
    const near = nearestAirport(port.lat, port.lng, { maxKm: null });
    lm = near ? landmassOf(near) : "other";
    _portLandmassCache.set(key, lm);
  }
  return lm;
}

function _accessOk(airport, port) {
  if (haversineKm(airport.lat, airport.lng, port.lat, port.lng) < 8) return true;
  if (_portLandmass(port) !== landmassOf(airport)) return false;
  const stats = waterPathStats(airport.lat, airport.lng, port.lat, port.lng);
  return stats.max_run_km < ACCESS_WATER_RUN_KM;
}

/** Best real ferry corridor connecting the areas around two airports - mirrors
 * geo.ferry_corridor_for, including its strictly-lower access-score tie-break. */
export function ferryCorridorFor(a, b, maxPortKm = PORT_MATCH_KM) {
  let best = null;
  let bestScore = null;
  for (const c of ferryCorridors()) {
    const pa = c.port_a, pb = c.port_b;
    for (const [aPort, bPort] of [[pa, pb], [pb, pa]]) {
      const aKm = haversineKm(a.lat, a.lng, aPort.lat, aPort.lng);
      if (aKm > maxPortKm) continue;
      const bKm = haversineKm(b.lat, b.lng, bPort.lat, bPort.lng);
      if (bKm > maxPortKm) continue;
      if (!(_accessOk(a, aPort) && _accessOk(b, bPort))) continue;
      const score = aKm + bKm;
      if (bestScore === null || score < bestScore) {
        bestScore = score;
        best = {
          ...c, a_port: aPort, b_port: bPort,
          a_access_km: aKm, b_access_km: bKm,
          crossing_km: haversineKm(aPort.lat, aPort.lng, bPort.lat, bPort.lng),
        };
      }
    }
  }
  return best;
}

export function ferryLegFromCorridor(match, region) {
  const access = estimateGround(match.a_access_km, "bus", region);
  const fareLo = match.price_usd_lo;
  const fare = fareLo != null
    ? Number(fareLo)
    : estimateGround(match.crossing_km, "ferry", region).cost;
  const hours = access.hours + FERRY_BOARDING_H + Number(match.duration_h);
  return {
    cost: pyRound(access.cost + fare), hours: pyRound(hours, 1),
    access_cost: access.cost, access_hours: access.hours,
    fare_usd: pyRound(fare), fare_is_real: fareLo != null,
    crossing_km: pyRound(match.crossing_km, 1),
  };
}

function _ferryNote(match) {
  const ops = (match.operators || []).join(", ") || "operator n/a";
  const freq = match.frequency_per_day;
  const freqS = freq ? `~${pyG(freq)}/day` : "frequency n/a";
  const seas = match.seasonal ? ", seasonal" : "";
  return `real ferry: ${match.a_port.name} → ${match.b_port.name} `
    + `(${ops}), ~${pyG(match.duration_h)}h crossing, ${freqS}${seas}`;
}

// --------------------------------------------------------------------------- gateway discovery
export function curatedGateways(destIata) {
  const out = [];
  const db = gatewaysDb();
  for (const entries of Object.values(db)) {
    if (!Array.isArray(entries)) continue;
    for (const e of entries) {
      if (String(e.dest_airport || "").toUpperCase() !== String(destIata || "").toUpperCase()) continue;
      for (const g of e.gateways) {
        const a = byIata(g.hub_airport);
        if (!a) continue;
        out.push({
          iata: a.iata, name: a.name, city: a.city ?? null, lat: a.lat, lng: a.lng,
          hub: a.hub, ground_mode: g.ground_mode,
          ground_hours: Number(g.ground_time_h),
          ground_cost: Number(g.ground_cost_usd),
          notes: g.notes || "", source: "curated",
        });
      }
    }
  }
  return out;
}

export function discoverGateways(dest, origin = null, { maxGroundH = 6.0, maxGateways = 4 } = {}) {
  const result = curatedGateways(dest.iata);
  const seen = new Set(result.map((g) => g.iata));

  const maxGwHub = { 1: 0, 2: 1, 3: 2 }[dest.hub] ?? 0;

  const region = regionOf(dest.lat, dest.lng);
  const maxKm = ["EU", "JP", "CN", "KR"].includes(region) ? 700 : 500;
  const lmDest = landmassOf(dest);

  const cands = [];
  for (const a of airports()) {
    if (a.iata === dest.iata || seen.has(a.iata)) continue;
    if (origin && a.iata === origin.iata) continue;
    if (a.hub > maxGwHub) continue;
    const d = haversineKm(dest.lat, dest.lng, a.lat, a.lng);
    if (d < 25 || d > maxKm) continue;

    // Water honesty, three rules - mirrors geo.py's discover_gateways exactly:
    //   1. a real corridor spanning most of the leg IS the connection;
    //   2. different landmasses with no corridor -> no leg at all;
    //   3. same landmass but a sea gap with no land detour -> no leg.
    let ferry = null;
    const corridor = ferryCorridorFor(a, dest);
    const usable = corridor !== null
      && (corridor.frequency_per_day || 0) >= MIN_FERRY_FREQ_PER_DAY;
    if (usable && corridor.crossing_km >= CROSSING_DOMINANT * d) {
      ferry = corridor;
    } else if (landmassOf(a) !== lmDest) {
      if (!usable) continue;
      ferry = corridor;
    } else if (seaGap(a, dest)) {
      continue;
    }

    if (ferry) {
      const leg = ferryLegFromCorridor(ferry, region);
      if (leg.hours > maxGroundH) continue;
      cands.push({
        iata: a.iata, name: a.name, city: a.city ?? null, lat: a.lat, lng: a.lng,
        hub: a.hub, ground_mode: "ferry", ground_hours: leg.hours,
        ground_cost: leg.cost, notes: _ferryNote(ferry), source: "auto",
        ferry: {
          id: ferry.id, name: ferry.name,
          operators: ferry.operators || [],
          duration_h: ferry.duration_h,
          frequency_per_day: ferry.frequency_per_day ?? null,
          seasonal: Boolean(ferry.seasonal),
          price_usd_lo: ferry.price_usd_lo ?? null,
          price_usd_hi: ferry.price_usd_hi ?? null,
          price_asof: ferry.price_asof ?? null,
          port_a: ferry.a_port.name, port_b: ferry.b_port.name,
          crossing_km: leg.crossing_km, fare_usd: leg.fare_usd,
          fare_is_real: leg.fare_is_real,
          access_cost: leg.access_cost, access_hours: leg.access_hours,
        },
        _dist: d,
      });
      continue;
    }

    const mode = pickGroundMode(d, region);
    const g = estimateGround(d, mode, region);
    if (g.hours > maxGroundH) continue;
    cands.push({
      iata: a.iata, name: a.name, city: a.city ?? null, lat: a.lat, lng: a.lng,
      hub: a.hub, ground_mode: mode, ground_hours: g.hours,
      ground_cost: g.cost, notes: `auto: ~${Math.trunc(d)}km ${mode}`, source: "auto",
      _dist: d,
    });
  }
  cands.sort((a, b) => (a.hub - b.hub) || (a._dist - b._dist)); // stable: best-connected & closest first
  for (const c of cands) {
    if (result.length >= maxGateways) break;
    delete c._dist;
    result.push(c);
  }
  return result.slice(0, maxGateways);
}
