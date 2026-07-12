// trip.js - faithful JS port of hopandhaul/trip.py's option-parsing and evaluate() reasoning.
//
// Mirrors trip.py exactly (including comments explaining *why*, kept so this never drifts
// from the rationale of the Python original) for the subset plan.js's orchestrator actually
// needs: leg/option parsing, group-size scaling, and the evaluate() ranking engine. The CLI-only
// surface (sugar_direct/sugar_split, format_report, argparse, selftest) isn't ported - nothing
// in the browser calls it; plan.js builds canonical option strings directly, the same way
// server.py's plan() does.
//
// See tests/web_parity/ for the harness proving this stays in lockstep with trip.py.
import { pyRound } from "./pyround.js";

export const DEFAULT_THRESHOLD = 200.0;
const GROUND_MODES = new Set(["train", "bus", "coach", "drive", "car", "rental", "ferry", "rail", "ground", "shuttle"]);
const FLIGHT_MODES = new Set(["fly", "flight", "plane", "air"]);
const KNOWN_MODES = new Set([...GROUND_MODES, ...FLIGHT_MODES]);
// Modes priced per VEHICLE (one car carries the whole group); everything else is per person.
const PER_VEHICLE_MODES = new Set(["drive", "car", "rental", "taxi", "uber", "rideshare"]);

// --------------------------------------------------------------------------- parsing
/** Parse a cost/hours token: strips $ , and stray whitespace - mirrors trip.num(). */
export function num(tok) {
  let cleaned = tok.trim().replace(/^\$+/, "").replace(/,/g, "").replace(/\$/g, "");
  if (cleaned.endsWith("h")) cleaned = cleaned.slice(0, -1);
  const v = Number(cleaned);
  if (!Number.isFinite(v) || cleaned === "") {
    throw new Error(`could not convert string to float: ${JSON.stringify(tok)}`);
  }
  return v;
}

/** 'fly 210 3.0' -> {mode, cost, hours, mode_unknown} - mirrors trip.parse_leg(). */
export function parseLeg(text) {
  const parts = text.split(/\s+/).filter(Boolean);
  if (parts.length < 2) throw new Error(`leg needs at least 'mode cost': got ${JSON.stringify(text)}`);
  const mode = parts[0].toLowerCase();
  const cost = num(parts[1]);
  const hours = parts.length >= 3 ? num(parts[2]) : 0.0;
  if (cost < 0 || hours < 0) throw new Error(`leg cost/hours must be >= 0: got ${JSON.stringify(text)}`);
  return { mode, cost, hours, mode_unknown: !KNOWN_MODES.has(mode) };
}

/** 'NAME | fly 210 3.0 ; train 75 4.0' -> full option dict with totals - mirrors
 * trip.parse_option(). minLegs guards a malformed multi-leg split being silently demoted to a
 * 1-leg direct, same as the Python original. */
export function parseOption(text, minLegs = 1) {
  let name = null;
  let body = text;
  if (text.includes("|")) {
    const idx = text.indexOf("|");
    name = text.slice(0, idx).trim();
    body = text.slice(idx + 1);
  }
  const legStrs = body.split(";").map((s) => s.trim()).filter(Boolean);
  if (legStrs.length === 0) throw new Error(`option has no legs: ${JSON.stringify(text)}`);
  if (legStrs.length < minLegs) {
    throw new Error(`expected at least ${minLegs} legs, got ${legStrs.length}: ${JSON.stringify(text)}`);
  }
  const legs = legStrs.map((s) => parseLeg(s));
  const cost = legs.reduce((sum, leg) => sum + leg.cost, 0);
  const hours = legs.reduce((sum, leg) => sum + leg.hours, 0);
  if (!name) name = legs.map((leg) => leg.mode).join(" → ");
  return {
    name,
    legs,
    cost: pyRound(cost, 2),
    hours: pyRound(hours, 4),
    nlegs: legs.length,
    is_split: legs.length >= 2,
  };
}

/** Group math: per-person modes (fly/train/bus/ferry...) scale ×N; per-vehicle modes don't - 
 * mirrors trip.scale_leg_cost(). */
export function scaleLegCost(mode, cost, travelers) {
  if (travelers <= 1 || PER_VEHICLE_MODES.has(mode.toLowerCase())) return cost;
  return cost * travelers;
}

/** Re-price a parsed option for N travelers - mirrors trip.scale_option(). */
export function scaleOption(opt, travelers) {
  if (travelers <= 1) return opt;
  const legs = opt.legs.map((leg) => ({ ...leg, cost: pyRound(scaleLegCost(leg.mode, leg.cost, travelers), 2) }));
  const cost = pyRound(legs.reduce((sum, leg) => sum + leg.cost, 0), 2);
  return { ...opt, legs, cost };
}

// --------------------------------------------------------------------------- reasoning
/** a dominates b if a is no worse on both cost and time, and strictly better on one - mirrors
 * trip._dominates(). Compares hours_eff (buffered), falling back to raw hours if unbuffered. */
function dominates(a, b) {
  const aH = a.hours_eff !== undefined ? a.hours_eff : a.hours;
  const bH = b.hours_eff !== undefined ? b.hours_eff : b.hours;
  return a.cost <= b.cost && aH <= bH && (a.cost < b.cost || aH < bH);
}

/** Tuple comparator: compares two same-length arrays element by element, matching how Python
 * compares the tuples passed as `key=` to min()/sorted(). */
function cmpTuple(a, b) {
  for (let i = 0; i < a.length; i++) {
    if (a[i] < b[i]) return -1;
    if (a[i] > b[i]) return 1;
  }
  return 0;
}

/** min(arr, key=keyFn) - keeps the FIRST minimal element on a tie, exactly like Python's
 * min(), which JS's Math.min has no equivalent for. */
function minByTuple(arr, keyFn) {
  let best = null;
  let bestKey = null;
  for (const item of arr) {
    const k = keyFn(item);
    if (best === null || cmpTuple(k, bestKey) < 0) {
      best = item;
      bestKey = k;
    }
  }
  return best;
}

/** Rank options and apply Cole's split-vs-direct rule - mirrors trip.evaluate() exactly,
 * including its stable sort/tie-break semantics (see minByTuple/cmpTuple above). Returns the
 * same shape as the Python version, MINUS the "_"-prefixed private rows (baseline/recommended
 * are exposed as `recommended`/`baseline` string names, same as the public trip.py contract - 
 * plan.js reads the private rows internally before stripping, same as server.py does). */
export function evaluate(options, {
  threshold = DEFAULT_THRESHOLD, vot = null, transferBuffer = 0.0,
  maxHours = null, travelers = 1,
} = {}) {
  if (!options.length) throw new Error("no options to evaluate");

  const opts = options.map((o) => ({ ...o }));

  for (const o of opts) {
    const buf = transferBuffer * Math.max(0, o.nlegs - 1);
    o.buffer_h = pyRound(buf, 4);
    o.hours_eff = pyRound(o.hours + buf, 4);
  }

  const directs = opts.filter((o) => !o.is_split);
  let baseline;
  let baselineKind;
  if (directs.length) {
    baseline = minByTuple(directs, (o) => [o.cost, o.hours_eff]);
    baselineKind = "cheapest direct";
  } else {
    baseline = minByTuple(opts, (o) => [o.cost, o.hours_eff]);
    baselineKind = "cheapest available (no direct option given)";
  }

  const adj = (o) => o.cost + (vot ? vot * o.hours_eff : 0.0);

  const rows = [];
  for (const o of opts) {
    const savings = pyRound(baseline.cost - o.cost, 2);
    const extraH = pyRound(o.hours_eff - baseline.hours_eff, 4);
    const isBaseline = o === baseline;
    const dominant = !isBaseline && dominates(o, baseline);
    const qualifies = savings >= threshold;
    let status;
    if (isBaseline) status = "baseline";
    else if (dominant) status = "dominant";
    else if (o.is_split && qualifies) status = "split_qualifies";
    else if (qualifies) status = "alt_qualifies";
    else if (savings > 0) status = "cheaper_below_threshold";
    else if (extraH < 0) status = "pricier_faster";
    else status = "worse";

    let breakevenVot = null;
    if (extraH > 0 && savings > 0) {
      breakevenVot = pyRound(savings / extraH, 2);
    } else if (extraH < 0 && savings < 0) {
      breakevenVot = pyRound((-savings) / (-extraH), 2);
    }
    const overBudget = maxHours !== null && o.hours_eff > maxHours;
    rows.push({
      ...o,
      savings_vs_baseline: savings,
      extra_hours_vs_baseline: extraH,
      is_baseline: isBaseline,
      dominant,
      qualifies,
      status,
      over_time_budget: overBudget,
      breakeven_vot: breakevenVot,
      adjusted_cost: pyRound(adj(o), 2),
    });
  }

  let eligible = rows.filter((r) => r.is_baseline || r.dominant || r.qualifies);
  let timeBudgetBinding = false;
  if (maxHours !== null) {
    const fits = eligible.filter((r) => !r.over_time_budget);
    if (fits.length) {
      timeBudgetBinding = fits.length < eligible.length;
      eligible = fits;
    }
  }
  const recommended = vot
    ? minByTuple(eligible, (r) => [r.adjusted_cost, r.hours_eff])
    : minByTuple(eligible, (r) => [r.cost, r.hours_eff]);

  const cheapestCash = minByTuple(rows, (r) => [r.cost, r.hours_eff]);
  const fastest = minByTuple(rows, (r) => [r.hours_eff, r.cost]);

  const ranked = rows.slice().sort((r1, r2) => cmpTuple(
    [r1 !== recommended ? 1 : 0, r1.cost, r1.hours_eff],
    [r2 !== recommended ? 1 : 0, r2.cost, r2.hours_eff],
  ));

  return {
    threshold,
    vot,
    transfer_buffer: transferBuffer,
    max_hours: maxHours,
    time_budget_binding: timeBudgetBinding,
    travelers,
    baseline: baseline.name,
    baseline_kind: baselineKind,
    recommended: recommended.name,
    cheapest_cash: cheapestCash.name,
    fastest: fastest.name,
    options: ranked,
    _recommended_row: recommended,
    _baseline_row: rows.find((r) => r.is_baseline),
  };
}
