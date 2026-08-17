// dates.js - the browser-native cheapest-date sweep: price the anchor date +/- window days and
// say which day actually comes out cheapest. This is a faithful port of server.py's
// sweep_dates(), the same way plan.js ports plan() - see tests/web_parity/ for the gate that
// holds the two in exact numeric agreement.
//
// Two things the Python twin does that this file deliberately does not:
//
// 1. Live fares. sweep_dates() opens with a rate-limiter probe, gives the live pass a wall-clock
//    budget, and throws the whole pass away and re-prices offline if the window comes back on
//    more than one basis (a "cheapest day" computed across mixed live/estimate rows is not a
//    comparison at all). None of that has a counterpart here: plan.js has no live path, the
//    Pages CSP would not allow one, and there is no Duffel key in a browser. So every row is an
//    estimate, `comparable` is always true, and `live_cut_off` is always false - which is
//    exactly what sweep_dates(..., allow_live=False) produces, and what the fixtures generate.
// 2. Transit and weather. Off on both sides for the same reason: ground schedules do not change
//    which DATE is cheapest, fares do, and the date the user picks gets a full plan of its own
//    the moment its chip is clicked.
//
// The window, the live/estimate/mixed labeling and the tie-break all come from dates.py's three
// stdlib helpers on the Python side; candidateDates/basisOfLegs/pickBest below are their ports,
// and they are exported so a caller can reuse them without running a whole sweep.
import { plan } from "./plan.js";
import { pyRound } from "./pyround.js";
import { DEFAULT_THRESHOLD, minByTuple } from "./trip.js";
import { parseIsoDate, localTodayYMD, ymdUtcMs, cmpYMD } from "./geo.js";

export const DEFAULT_WINDOW = 3;   // 3 days each way -> 7 dates priced
export const MAX_WINDOW = 7;       // 7 days each way -> 15 dates, the hard cap

const DAY_MS = 86400000;

function err(code, message) {
  return { ok: false, error: message, code };
}

function pad(n, width) {
  return String(n).padStart(width, "0");
}

/** Whole-day arithmetic on a pure calendar date, done in UTC milliseconds.
 *
 * Python shifts a date with `base + timedelta(days=n)`, which has no timezone and no DST to get
 * wrong. `new Date(y, m - 1, d + n)` is LOCAL time and slides by an hour across a DST boundary,
 * which in a zone whose transition lands near midnight lands on the previous calendar day. That
 * is not a rounding wobble: it changes which dates are in the window, and the parity gate fails
 * on the array length. 86400000 * n is exact in a double for every n this app can see. */
function shiftYMD(ymd, days) {
  const d = new Date(ymdUtcMs(ymd) + days * DAY_MS);
  return { y: d.getUTCFullYear(), m: d.getUTCMonth() + 1, d: d.getUTCDate() };
}

function isoOf({ y, m, d }) {
  return `${pad(y, 4)}-${pad(m, 2)}-${pad(d, 2)}`;
}

/** parseIsoDate() that throws instead of returning null, so a bad date string fails the same
 * place Python's date.fromisoformat() does. */
function mustParse(dateStr) {
  const d = parseIsoDate(dateStr);
  if (!d) throw new Error(`invalid date ${JSON.stringify(dateStr)}`);
  return d;
}

/**
 * anchor +/- window days, in order, as YYYY-MM-DD strings, with dates before today dropped -
 * there is no fare to check for a day that has already gone. Always includes the anchor itself
 * when it is not in the past (window=0 -> just [anchor], or [] if the anchor is past).
 *
 * `today` is injectable ({y,m,d}) for the same reason dates.py's candidate_dates() takes one: a
 * caller that needs a deterministic window can pin the filter instead of racing the wall clock.
 * Ports dates.candidate_dates().
 */
export function candidateDates(anchor, window, today = null) {
  const base = mustParse(anchor);
  const t = today || localTodayYMD();
  const out = [];
  for (let delta = -window; delta <= window; delta++) {
    const d = shiftYMD(base, delta);
    if (cmpYMD(d, t) >= 0) out.push(isoOf(d));
  }
  return out;
}

/**
 * "live" / "estimate" / "mixed" for a set of itinerary legs, read off the FLIGHT legs only.
 * Ground legs are always distance estimates on both engines, so they say nothing about whether a
 * real fare query set this price.
 *
 * The empty/none-live guard has to come first: [].every() is true in JS just as all([]) is True
 * in Python, so an option with no flight leg at all would otherwise label itself "live".
 * Ports dates.basis_of_legs().
 */
export function basisOfLegs(legs) {
  const flightLive = legs.filter((l) => l.mode === "fly").map((l) => l.is_live);
  if (!flightLive.length || !flightLive.some(Boolean)) return "estimate";
  if (flightLive.every(Boolean)) return "live";
  return "mixed";
}

/**
 * The cheapest row that actually priced, ties broken on effective hours and then on window
 * order. minByTuple keeps the FIRST minimum, like Python's min(), so a genuine tie goes to the
 * EARLIEST date in the window - the one a traveler would rather be told about. Two same-weekday
 * days in one month carry the same fare multiplier, so those ties are routine, not theoretical.
 * Ports dates.pick_best().
 */
export function pickBest(rows) {
  if (!rows.length) {
    throw new Error("no candidate dates left in range - every date in the window is "
      + "already in the past");
  }
  // Same test as Python's `"error" not in r`: a priced row simply has no "error" key.
  const priced = rows.filter((r) => !("error" in r));
  if (!priced.length) throw new Error("every candidate date failed to price - see the per-date errors");
  return minByTuple(priced, (r) => [r.cost, r.hours]);
}

/** One plan() result reduced to a sweep row: the recommended option's cost, effective hours and
 * pricing basis, and nothing else. Ports server.py's _sweep_row(), including its reason for
 * dropping the full result - the UI re-fetches the chosen date through plan() the moment its
 * chip is clicked, so carrying 15 itineraries here would multiply the payload for data nobody
 * reads. */
function sweepRow(res, cand, candRet) {
  if (!res.ok) {
    return {
      date: cand, return_date: candRet, ok: false,
      error: res.error ?? "could not price that date",
      code: res.code ?? "date_lookup_failed",
    };
  }
  const result = res.result;
  // plan() strips "_"-prefixed keys, so the recommended option has to be found by name.
  const rec = result.options.find((o) => o.name === result.recommended) ?? null;
  if (rec === null) {
    return {
      date: cand, return_date: candRet, ok: false,
      error: "could not price that date", code: "date_lookup_failed",
    };
  }
  return {
    date: cand, return_date: candRet, ok: true,
    recommended: rec.name, cost: rec.cost, hours: rec.hours_eff,
    basis: basisOfLegs(rec.itinerary.legs),
    pricing_source: res.pricing_source, savings_vs_anchor: null,
  };
}

/**
 * @param {object} params
 * @param {number} params.destLat
 * @param {number} params.destLng
 * @param {string} [params.originIata]
 * @param {string|null} [params.date] YYYY-MM-DD, required - a sweep with no anchor is meaningless
 * @param {number} [params.window] days each way, 0..MAX_WINDOW
 * @param {number|null} [params.vot]
 * @param {number} [params.threshold]
 * @param {number} [params.maxGroundH]
 * @param {boolean} [params.roundtrip]
 * @param {number} [params.travelers]
 * @param {string|null} [params.ret] YYYY-MM-DD return date
 * @param {number} [params.transferBuffer]
 * @returns {object} same shape as server.py's sweep_dates(..., allow_live=False,
 *   allow_transit=False, fetch_weather=False)
 */
export function sweepDates({
  destLat, destLng, originIata = "JFK", date = null, window = DEFAULT_WINDOW, vot = null,
  threshold = DEFAULT_THRESHOLD, maxGroundH = 6.0, roundtrip = false, travelers = 1,
  ret = null, transferBuffer = 1.0,
}) {
  if (!date) return err("invalid_param", "date is required");

  // `window` is the sweep's half-width in days. The name shadows the browser global inside this
  // function, which is deliberate: it is what the Python parameter is called and nothing in here
  // touches the DOM. Python's int() truncates a float toward zero and raises on everything else,
  // where Number() would read null or "" as 0 and quietly turn a missing window into a one-date
  // sweep, so those take the same reject path Python's TypeError does.
  // Python's int() truncates a float toward zero but RAISES on a string that is not a whole
  // number: int(3.7) is 3, int("3.7") is a ValueError. Number("3.7") is 3.7 either way, so a
  // string has to be checked for a fractional part before truncating or the two engines take
  // different branches on the same input. Number() also reads null and "" and booleans as 0,
  // which would quietly turn a missing window into a one-date sweep, so those reject too.
  const raw = typeof window === "string" ? window.trim() : window;
  const num = (raw === "" || raw === null || raw === undefined || typeof raw === "boolean")
    ? NaN : Number(raw);
  if (!Number.isFinite(num)) return err("invalid_param", `window must be between 0 and ${MAX_WINDOW}`);
  if (typeof raw === "string" && !Number.isInteger(num)) {
    return err("invalid_param", `window must be between 0 and ${MAX_WINDOW}`);
  }
  window = Math.trunc(num);
  if (!(window >= 0 && window <= MAX_WINDOW)) {
    return err("invalid_param", `window must be between 0 and ${MAX_WINDOW}`);
  }

  let candidates;
  let tripLen = null;
  try {
    candidates = candidateDates(date, window);
    if (ret) tripLen = Math.round((ymdUtcMs(mustParse(ret)) - ymdUtcMs(mustParse(date))) / DAY_MS);
  } catch {
    return err("invalid_param", "date must be YYYY-MM-DD");
  }
  if (tripLen !== null && tripLen < 0) {
    return err("invalid_param", "return date must be on or after the depart date");
  }
  if (!candidates.length) {
    return err("dates_all_past", "every date in that window is already in the past");
  }

  // plan()'s three structural refusals - unknown origin, no airport near the click, clicking your
  // own origin - depend on the origin and the clicked point and never on the date, so they would
  // come back identical for every candidate. Probe once and fail the whole sweep with the real
  // reason instead of emitting N byte-identical error rows and then reporting that no date could
  // be priced.
  const probe = plan({ destLat, destLng, originIata, date: candidates[0], maxGroundH });
  if (!probe.ok) {
    return err(probe.code ?? "plan_failed", probe.error ?? "could not plan that route");
  }

  // A return date shifts by the same number of days as its paired departure, so a round trip's
  // LENGTH stays fixed while its placement in the window moves.
  const retFor = (cand) => (tripLen === null ? null : isoOf(shiftYMD(mustParse(cand), tripLen)));

  const rows = [];
  for (const cand of candidates) {
    const candRet = retFor(cand);
    let res;
    try {
      res = plan({
        destLat, destLng, originIata, date: cand, vot, threshold, maxGroundH, roundtrip,
        travelers, ret: candRet, transferBuffer,
      });
    } catch {
      // One bad date degrades that date, never the whole sweep. Nothing is logged here for the
      // same reason no other engine module logs: the row itself carries the failure, and the UI
      // is the only layer that decides how loud it should be.
      rows.push({
        date: cand, return_date: candRet, ok: false,
        error: "could not price that date", code: "date_lookup_failed",
      });
      continue;
    }
    rows.push(sweepRow(res, cand, candRet));
  }

  const priced = rows.filter((r) => r.ok);
  if (!priced.length) {
    return err("dates_all_failed", "none of the dates in that window could be priced");
  }

  const anchorRow = priced.find((r) => r.date === date) ?? null;
  const anchorCost = anchorRow ? anchorRow.cost : null;
  for (const r of priced) {
    if (r === anchorRow) {
      // A literal zero, not anchorCost - anchorCost: a computed zero can land on -0, which
      // JSON.stringify writes as 0 and Python writes as -0.0, and the parity gate is not where
      // anyone wants to meet that. The `|| 0.0` below is the same guard for the rounded
      // differences, and matches Python's `round(...) or 0.0`.
      r.savings_vs_anchor = 0.0;
    } else if (anchorCost === null) {
      r.savings_vs_anchor = null;      // the anchor itself never priced (a past anchor, usually)
    } else {
      r.savings_vs_anchor = pyRound(anchorCost - r.cost, 2) || 0.0;
    }
  }

  const best = pickBest(rows);
  return {
    ok: true,
    origin_iata: probe.origin.iata,
    dest_iata: probe.dest.iata,
    anchor_date: date,
    window,
    comparable: new Set(priced.map((r) => r.basis)).size === 1,
    live_cut_off: false,
    dates: rows,
    best: {
      date: best.date, cost: best.cost, hours: best.hours,
      basis: best.basis, recommended: best.recommended,
    },
  };
}
