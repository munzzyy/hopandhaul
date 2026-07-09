// geo.js — faithful JS port of hopandhaul/geo.py's estimate path (no network).
//
// This mirrors geo.py function-for-function, constant-for-constant, including operation
// order, so it stays bit-for-bit identical to the Python engine after rounding — see
// tests/web_parity/. If you're changing behavior here, change geo.py first and port the
// change back; this file should never drift ahead of the Python original.
//
// Pure ESM, no dependencies beyond ./data.js (airport/gateway lookups) and ./pyround.js
// (Python round() semantics).
import { pyRound } from "./pyround.js";
import { airports, gatewaysDb, byIata } from "./data.js";

export { byIata };

// ---- fare model (ESTIMATE) -------------------------------------------------------------
export const FLIGHT_CURVE = [30.0, 1.8, 0.012]; // (base, per_sqrt_km, per_km)
export const FLIGHT_FLOOR = 45.0;
export const NA_SHORT_FLOOR = 65.0;
export const SMALL_AIRPORT_PREMIUM = { 1: 0.0, 2: 0.18, 3: 0.75 };
export const HUB_COMPETITION_DISCOUNT = 0.92;
export const FLIGHT_FIXED_H = 1.1;
export const CONNECTION_H = 1.6;

// Route-market multiplier by (region, region) pair, pre-sorted alphabetically — mirrors
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

/** Closest airport to a point, capped at maxKm — mirrors geo.nearest_airport exactly,
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
/** Coarse region for ground-transport quality/cost and route-market pricing — mirrors
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

/** Strict YYYY-MM-DD parse + real-calendar-date validation. Returns {y,m,d} or null —
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

/** Today as a pure {y,m,d} calendar date in the LOCAL timezone — matches Python's
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
  EFL: "kefalonia", JSI: "skiathos", HER: "crete", CHQ: "crete", RHO: "rhodes",
  KGS: "kos", MLA: "malta", LCA: "cyprus", PFO: "cyprus",
  PMO: "sicily", CTA: "sicily", TPS: "sicily",
  CAG: "sardinia", OLB: "sardinia", AHO: "sardinia", AJA: "corsica", BIA: "corsica",
  PMI: "mallorca", IBZ: "ibiza", MAH: "menorca",
  // Atlantic
  TFS: "tenerife", TFN: "tenerife", LPA: "grancanaria", ACE: "lanzarote",
  FUE: "fuerteventura", SPC: "lapalma", FNC: "madeira", PDL: "azores",
  KEF: "iceland", FAE: "faroe", SID: "capeverde",
  DUB: "ireland", ORK: "ireland", SNN: "ireland", NOC: "ireland",
  BFS: "ireland", BHD: "ireland",
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
  HBA: "tasmania", HTI: "whitsundays", YYJ: "vancouverisland",
  NAN: "fiji", PPT: "tahiti", BOB: "borabora", RAR: "rarotonga", GUM: "guam",
  IPC: "easterisland", ACK: "nantucket", MVY: "marthasvineyard",
  JNU: "juneau", KTN: "ketchikan",
};
const _CONTINENT = {
  NA: "americas", LATAM: "americas", EU: "eurasia", ME: "eurasia",
  CN: "eurasia", IN: "eurasia", SEA: "eurasia", KR: "korea",
  JP: "japan", AU: "australia", AF: "africa", ZA: "africa",
};
export const FERRY_MAX_KM = 250;

export function landmassOf(a) {
  const lm = ISLAND_LANDMASS[a.iata];
  if (lm && lm !== "mainland") return lm;
  return _CONTINENT[regionOf(a.lat, a.lng)] || "other";
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
    let mode;
    if (landmassOf(a) !== lmDest) {
      if (d > FERRY_MAX_KM) continue;
      mode = "ferry";
    } else {
      mode = pickGroundMode(d, region);
    }
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
