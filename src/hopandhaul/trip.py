#!/usr/bin/env python3
"""
trip.py — deterministic cheapest-route reasoning engine for the travel-scout agent.

Core question it answers: given several ways to get from A to B, which is cheapest,
and — Cole's rule — is it worth flying into a cheaper nearby airport and taking a
train/bus/car the rest of the way instead of flying direct?

The rule: only recommend a multimodal "split" (fly-to-gateway + ground leg) over the
cheapest direct option if the split saves at least a threshold amount (default $200).
Below that, the extra transfer/hassle isn't worth it. Two honest exceptions the engine
also handles: a split that is *both* cheaper AND faster (dominant — take it regardless),
and a value-of-time overlay so you can compare cash against extra hours fairly.

Pure Python stdlib. It does no networking — it reasons over prices/durations you (or the
agent, via search/APIs) supply. Garbage prices in -> garbage answer out; gather real ones.

Option grammar (canonical):
    "NAME | mode cost hours ; mode cost hours ; ..."
  - "NAME |" is optional (auto-named from the legs if omitted).
  - legs separated by ";"  (a >=2-leg option is treated as a multimodal "split")
  - each leg: "mode cost hours"  e.g.  "fly 210 3.0"   ("hours" is decimal, door-to-door)
  cost may be written $1,240 / 1240 — symbols and commas are stripped.

Sugar:
    --direct "fly 620 5.5"                          (single leg to the final destination)
    --split  "DEN via Amtrak: fly 210 3.0 + train 75 4.0"   (legs joined by '+', name optional)

Examples:
    hopandhaul plan --to "Aspen CO" \
        --direct "fly 620 5.5" \
        --split  "DEN + Amtrak: fly 210 3.0 + train 75 4.0" \
        --split  "Eagle + rental: fly 390 4.5 + drive 60 2.0" \
        --vot 30
    python -m hopandhaul.trip --selftest
"""
from __future__ import annotations

import argparse
import json
import sys

DEFAULT_THRESHOLD = 200.0  # Cole's rule: a split must beat direct by >= this to be worth it.
GROUND_MODES = {"train", "bus", "coach", "drive", "car", "rental", "ferry", "rail", "ground", "shuttle"}
FLIGHT_MODES = {"fly", "flight", "plane", "air"}
KNOWN_MODES = GROUND_MODES | FLIGHT_MODES
# Modes priced per VEHICLE (one car carries the whole group); everything else is per person.
PER_VEHICLE_MODES = {"drive", "car", "rental", "taxi", "uber", "rideshare"}


# --------------------------------------------------------------------------- parsing
def num(tok: str) -> float:
    """Parse a cost/hours token: strips $ , and stray whitespace."""
    cleaned = tok.strip().lstrip("$").replace(",", "").replace("$", "")
    if cleaned.endswith("h"):
        cleaned = cleaned[:-1]
    return float(cleaned)


_num = num  # back-compat alias; duffel.py/providers.py called this before it went public


def parse_leg(text: str) -> dict:
    """'fly 210 3.0' -> {'mode','cost','hours'}. Tolerates a missing hours (defaults 0).

    Unknown modes are accepted (a typo shouldn't crash a CLI) but flagged with
    'mode_unknown' so callers can warn instead of silently mispricing (a typo'd
    "flght"/"walk" used to fall into the per-person cost branch with no signal).
    """
    parts = text.split()
    if len(parts) < 2:
        raise ValueError(f"leg needs at least 'mode cost': got {text!r}")
    mode = parts[0].lower()
    cost = num(parts[1])
    hours = num(parts[2]) if len(parts) >= 3 else 0.0
    if cost < 0 or hours < 0:
        raise ValueError(f"leg cost/hours must be >= 0: got {text!r}")
    return {"mode": mode, "cost": cost, "hours": hours, "mode_unknown": mode not in KNOWN_MODES}


def parse_option(text: str, min_legs: int = 1) -> dict:
    """'NAME | fly 210 3.0 ; train 75 4.0' -> full option dict with totals.

    min_legs guards callers that mean to build a multi-leg split (sugar_split) —
    a malformed "--split" with only one leg is a correctness bug in the core
    thesis (a split silently demoted to a direct), not something to swallow.
    """
    name = None
    body = text
    if "|" in text:
        name, body = text.split("|", 1)
        name = name.strip()
    leg_strs = [s for s in body.split(";") if s.strip()]
    if not leg_strs:
        raise ValueError(f"option has no legs: {text!r}")
    if len(leg_strs) < min_legs:
        raise ValueError(
            f"expected at least {min_legs} legs, got {len(leg_strs)}: {text!r}")
    legs = [parse_leg(s) for s in leg_strs]
    cost = sum(leg["cost"] for leg in legs)
    hours = sum(leg["hours"] for leg in legs)
    if not name:
        name = " → ".join(leg["mode"] for leg in legs)
    return {
        "name": name,
        "legs": legs,
        "cost": round(cost, 2),
        "hours": round(hours, 4),
        "nlegs": len(legs),
        "is_split": len(legs) >= 2,
    }


def sugar_direct(text: str) -> str:
    """--direct value -> canonical option string.

    Accepts an optional 'NAME:' or 'NAME |' label, then a single leg:
      'fly 620 5.5' | '620 5.5' | 'Fly direct to ASE: fly 620 5.5'
    """
    name = None
    body = text
    if "|" in text:
        name, body = text.split("|", 1)
    elif ":" in text:
        head, tail = text.split(":", 1)
        if len(tail.split()) >= 2:  # remainder looks like 'mode cost [hours]' -> head was a label
            name, body = head, tail
    name = name.strip() if name else None
    toks = body.split()
    # allow bare "cost hours" -> assume a flight
    if toks and not toks[0].replace(".", "").isalpha():
        body = "fly " + body
    label = name or "Direct"
    return f"{label} | {body.strip()}"


def scale_leg_cost(mode: str, cost: float, travelers: int) -> float:
    """Group math: per-person modes (fly/train/bus/ferry…) scale ×N; per-vehicle modes don't."""
    if travelers <= 1 or mode.lower() in PER_VEHICLE_MODES:
        return cost
    return cost * travelers


def scale_option(opt: dict, travelers: int) -> dict:
    """Re-price a parsed option (whose leg costs are per person / per vehicle) for N travelers."""
    if travelers <= 1:
        return opt
    legs = [{**leg, "cost": round(scale_leg_cost(leg["mode"], leg["cost"], travelers), 2)}
            for leg in opt["legs"]]
    return {**opt, "legs": legs, "cost": round(sum(leg["cost"] for leg in legs), 2)}


def sugar_split(text: str) -> str:
    """--split value -> canonical option string. Legs joined by '+', optional 'NAME:' prefix."""
    name = None
    body = text
    if ":" in text and "+" in text.split(":", 1)[1]:
        # "NAME: leg + leg"  — only treat as name if a '+' follows (avoids eating times)
        name, body = text.split(":", 1)
        name = name.strip()
    elif ":" in text and "+" not in text and text.count(":") == 1:
        name, body = text.split(":", 1)
        name = name.strip()
    legs = "; ".join(s.strip() for s in body.split("+") if s.strip())
    label = name or "Split"
    return f"{label} | {legs}"


# --------------------------------------------------------------------------- reasoning
def _dominates(a: dict, b: dict) -> bool:
    """a dominates b if a is no worse on both cost and time, and strictly better on one."""
    return a["cost"] <= b["cost"] and a["hours"] <= b["hours"] and (
        a["cost"] < b["cost"] or a["hours"] < b["hours"]
    )


def evaluate(options: list[dict], threshold: float = DEFAULT_THRESHOLD,
             vot: float | None = None, transfer_buffer: float = 0.0,
             max_hours: float | None = None, travelers: int = 1) -> dict:
    """Rank options and apply Cole's split-vs-direct rule. Returns a structured result.

    max_hours: options whose effective time exceeds this are excluded from the
    recommendation (still shown, tagged over_time_budget) — unless nothing fits.
    travelers: metadata only (costs must already be group totals); shown in the report.
    """
    if not options:
        raise ValueError("no options to evaluate")

    opts = [dict(o) for o in options]

    # add a per-transfer connection buffer to multi-leg options' time (missed-connection realism)
    for o in opts:
        buf = transfer_buffer * max(0, o["nlegs"] - 1)
        o["buffer_h"] = round(buf, 4)
        o["hours_eff"] = round(o["hours"] + buf, 4)

    # baseline = cheapest DIRECT (single-leg) option; fall back to cheapest overall.
    directs = [o for o in opts if not o["is_split"]]
    if directs:
        baseline = min(directs, key=lambda o: (o["cost"], o["hours_eff"]))
        baseline_kind = "cheapest direct"
    else:
        baseline = min(opts, key=lambda o: (o["cost"], o["hours_eff"]))
        baseline_kind = "cheapest available (no direct option given)"

    def adj(o: dict) -> float:
        return o["cost"] + (vot * o["hours_eff"] if vot else 0.0)

    # annotate each option relative to baseline
    rows = []
    for o in opts:
        savings = round(baseline["cost"] - o["cost"], 2)         # + means cheaper than baseline
        extra_h = round(o["hours_eff"] - baseline["hours_eff"], 4)  # + means slower than baseline
        is_baseline = o is baseline
        dominant = (not is_baseline) and _dominates(o, baseline)
        qualifies = savings >= threshold                         # beats baseline by >= rule
        if is_baseline:
            status = "baseline"
        elif dominant:
            status = "dominant"          # cheaper AND faster (or equal) — rule is moot, take it
        elif o["is_split"] and qualifies:
            status = "split_qualifies"   # the headline case: fly cheaper + ground, saves >= threshold
        elif qualifies:
            status = "alt_qualifies"     # a different direct that clears the threshold
        elif savings > 0:
            status = "cheaper_below_threshold"
        elif extra_h < 0:
            status = "pricier_faster"
        else:
            status = "worse"
        # break-even value-of-time vs baseline (only meaningful when there's a cost/time trade)
        breakeven_vot = None
        if extra_h > 0 and savings > 0:
            breakeven_vot = round(savings / extra_h, 2)      # value time BELOW this -> this option wins
        elif extra_h < 0 and savings < 0:
            breakeven_vot = round((-savings) / (-extra_h), 2)  # value time ABOVE this -> this option wins
        over_budget = max_hours is not None and o["hours_eff"] > max_hours
        rows.append({
            **o,
            "savings_vs_baseline": savings,
            "extra_hours_vs_baseline": extra_h,
            "is_baseline": is_baseline,
            "dominant": dominant,
            "qualifies": qualifies,
            "status": status,
            "over_time_budget": over_budget,
            "breakeven_vot": breakeven_vot,
            "adjusted_cost": round(adj(o), 2),
        })

    # eligible recommendation set: the baseline, plus anything dominant or clearing the threshold.
    # A time budget (max_hours) knocks options out of contention — unless nothing at all fits.
    eligible = [r for r in rows if r["is_baseline"] or r["dominant"] or r["qualifies"]]
    time_budget_binding = False
    if max_hours is not None:
        fits = [r for r in eligible if not r["over_time_budget"]]
        if fits:
            time_budget_binding = len(fits) < len(eligible)
            eligible = fits
    if vot:
        recommended = min(eligible, key=lambda r: (r["adjusted_cost"], r["hours_eff"]))
    else:
        recommended = min(eligible, key=lambda r: (r["cost"], r["hours_eff"]))

    cheapest_cash = min(rows, key=lambda r: (r["cost"], r["hours_eff"]))
    fastest = min(rows, key=lambda r: (r["hours_eff"], r["cost"]))

    # order the display table: recommended first, then by cash cost.
    ranked = sorted(rows, key=lambda r: (r is not recommended, r["cost"], r["hours_eff"]))

    return {
        "threshold": threshold,
        "vot": vot,
        "transfer_buffer": transfer_buffer,
        "max_hours": max_hours,
        "time_budget_binding": time_budget_binding,
        "travelers": travelers,
        "baseline": baseline["name"],
        "baseline_kind": baseline_kind,
        "recommended": recommended["name"],
        "cheapest_cash": cheapest_cash["name"],
        "fastest": fastest["name"],
        "options": ranked,
        "_recommended_row": recommended,
        "_baseline_row": next(r for r in rows if r["is_baseline"]),
    }


# --------------------------------------------------------------------------- reporting
_STATUS_TAG = {
    "baseline": "baseline (fly direct)",
    "dominant": "✅ cheaper AND faster",
    "split_qualifies": "✅ split saves ≥ threshold",
    "alt_qualifies": "✅ saves ≥ threshold",
    "cheaper_below_threshold": "⚠️ cheaper but < threshold",
    "pricier_faster": "faster but pricier",
    "worse": "✗ worse",
}


def _fmt_money(x: float) -> str:
    return f"${x:,.0f}" if abs(x - round(x)) < 0.005 else f"${x:,.2f}"


def _fmt_hours(h: float) -> str:
    total_min = round(h * 60)
    hh, mm = divmod(total_min, 60)
    return f"{hh}h{mm:02d}" if mm else f"{hh}h"


def format_report(res: dict, origin: str | None, dest: str | None) -> str:
    L = []
    where = f"{origin} → {dest}" if origin and dest else (dest or origin or "trip")
    L.append(f"TRIP: {where}")
    baseline_phrase = ("vs the cheapest direct" if res.get("baseline_kind") == "cheapest direct"
                       else "vs the cheapest option given (no direct flight was supplied)")
    rule = (f"Rule: recommend a fly-cheaper-then-ground split only if it saves "
            f"≥ {_fmt_money(res['threshold'])} {baseline_phrase}.")
    L.append(rule)
    if res["vot"]:
        L.append(f"Value of time: {_fmt_money(res['vot'])}/hr (used to rank cash vs hours).")
    if res["transfer_buffer"]:
        L.append(f"Transfer buffer: +{_fmt_hours(res['transfer_buffer'])} added per connection.")
    if res.get("travelers", 1) > 1:
        L.append(f"Travelers: {res['travelers']} — costs are GROUP TOTALS "
                 f"(per-person fares ×{res['travelers']}; drive/rental legs are per vehicle).")
    if res.get("max_hours") is not None:
        L.append(f"Time budget: {_fmt_hours(res['max_hours'])} door-to-door — slower options are "
                 f"excluded from the recommendation.")
    L.append("")

    # table
    L.append("OPTIONS (recommended first, then by cost):")
    name_w = max(12, min(38, max(len(o["name"]) for o in res["options"])))
    for o in res["options"]:
        mark = "→" if o["name"] == res["recommended"] else " "
        tag = _STATUS_TAG.get(o["status"], o["status"])
        if o.get("over_time_budget"):
            tag += "  ⏱ over time budget"
        legs = " + ".join(f"{leg['mode']} {_fmt_money(leg['cost'])}" for leg in o["legs"])
        line = (f" {mark} {o['name'][:name_w].ljust(name_w)}  "
                f"{_fmt_money(o['cost']).rjust(7)}  {_fmt_hours(o['hours_eff']).rjust(6)}  "
                f"{('multimodal' if o['is_split'] else 'direct').ljust(10)}  {tag}")
        L.append(line)
        sub = f"       ({legs})"
        if not o["is_baseline"]:
            if o["savings_vs_baseline"] > 0:
                sub += f"  — saves {_fmt_money(o['savings_vs_baseline'])}"
            elif o["savings_vs_baseline"] < 0:
                sub += f"  — costs {_fmt_money(-o['savings_vs_baseline'])} more"
            if o["extra_hours_vs_baseline"] > 0:
                sub += f", +{_fmt_hours(o['extra_hours_vs_baseline'])}"
            elif o["extra_hours_vs_baseline"] < 0:
                sub += f", {_fmt_hours(-o['extra_hours_vs_baseline'])} faster"
        L.append(sub)
    L.append("")

    # decision
    rec = res["_recommended_row"]
    L.append("DECISION:")
    L.append(f"  Cheapest cash:  {res['cheapest_cash']}")
    L.append(f"  Fastest:        {res['fastest']}")
    if res.get("time_budget_binding"):
        L.append(f"  (the {_fmt_hours(res['max_hours'])} time budget excluded at least one "
                 f"otherwise-qualifying option)")
    if rec["is_baseline"]:
        why_base = (f"no alternative clears the {_fmt_money(res['threshold'])} rule "
                    f"(or beats it on time)")
        if res.get("time_budget_binding"):
            why_base += f" within the {_fmt_hours(res['max_hours'])} time budget"
        L.append(f"  → RECOMMENDED: {rec['name']} — {why_base}, so fly direct.")
    else:
        why = []
        if rec["dominant"]:
            why.append("it is both cheaper and faster than flying direct")
        elif rec["status"] in ("split_qualifies", "alt_qualifies"):
            why.append(f"it saves {_fmt_money(rec['savings_vs_baseline'])} "
                       f"(≥ {_fmt_money(res['threshold'])} rule)")
        L.append(f"  → RECOMMENDED: {rec['name']} — {', '.join(why)}.")
        # trade-off / break-even reasoning
        if rec["extra_hours_vs_baseline"] > 0 and rec["breakeven_vot"] is not None:
            L.append(f"    It adds {_fmt_hours(rec['extra_hours_vs_baseline'])} vs direct for "
                     f"{_fmt_money(rec['savings_vs_baseline'])} saved.")
            L.append(f"    Break-even: prefer the direct flight only if your time is worth more "
                     f"than {_fmt_money(rec['breakeven_vot'])}/hr.")
            if res["vot"]:
                delta = round(rec["savings_vs_baseline"] - res["vot"] * rec["extra_hours_vs_baseline"], 2)
                verdict = "still ahead" if delta >= 0 else "behind"
                L.append(f"    At your {_fmt_money(res['vot'])}/hr, the split is {verdict} by "
                         f"{_fmt_money(abs(delta))} after valuing the extra time.")
        elif rec["extra_hours_vs_baseline"] <= 0:
            L.append("    …and it is no slower than flying direct — a clean win.")

    L.append("")
    L.append("CAUTIONS:")
    if any(o["is_split"] and not o["is_baseline"] for o in res["options"]):
        L.append("  • Split legs booked separately are NOT protected — a delayed flight can forfeit a")
        L.append("    non-refundable train/bus. Leave a real buffer, or book a flexible ground fare.")
    L.append("  • Prices/times are the inputs supplied; re-verify live before booking (fares move fast).")
    L.append("  • This is one direction — run the return separately; round-trip fares can flip the math.")
    return "\n".join(L)


# --------------------------------------------------------------------------- CLI
def build_options(args) -> list[dict]:
    # (raw_string, min_legs) — a --split that parses to 1 leg is a bug, not a silent direct.
    raw = []
    for d in args.direct or []:
        raw.append((sugar_direct(d), 1))
    for s in args.split or []:
        raw.append((sugar_split(s), 2))
    for o in args.option or []:
        raw.append((o, 1))
    if args.json_in:
        with open(args.json_in, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data:
            if isinstance(item, str):
                raw.append((item, 1))
            else:  # {"name":..,"legs":[{"mode","cost","hours"}]}
                legs = "; ".join(f"{leg['mode']} {leg['cost']} {leg.get('hours', 0)}"
                                for leg in item["legs"])
                raw.append((f"{item.get('name', '')} | {legs}", 1))
    return [parse_option(r, min_legs=n) for r, n in raw]


def _force_utf8():
    """Windows consoles default to cp1252 and choke on → ✅ ≈; make stdout/stderr UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # py3.7+
        except (AttributeError, ValueError):
            pass


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Cheapest-route engine with the $200 fly-then-train rule.")
    p.add_argument("--to", dest="dest", help="final destination (label only)")
    p.add_argument("--from", dest="origin", help="origin (label only)")
    p.add_argument("-o", "--option", action="append", help="canonical option: 'NAME | mode cost hours ; ...'")
    p.add_argument("--direct", action="append", help="sugar: single-leg direct, e.g. 'fly 620 5.5'")
    p.add_argument("--split", action="append", help="sugar: 'NAME: fly 210 3.0 + train 75 4.0'")
    p.add_argument("--json-in", help="read options from a JSON file (list of strings or {name,legs})")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                   help=f"min $ a split must save vs direct to be recommended (default {DEFAULT_THRESHOLD:g})")
    p.add_argument("--vot", type=float, default=None, help="value of time in $/hr (ranks cash vs hours)")
    p.add_argument("--transfer-buffer", type=float, default=0.0,
                   help="hours added per connection to model missed-connection risk")
    p.add_argument("--travelers", type=int, default=1,
                   help="group size: per-person legs (fly/train/bus/ferry) scale ×N; "
                        "drive/car/rental legs are per vehicle (give per-person/per-vehicle prices)")
    p.add_argument("--max-hours", type=float, default=None,
                   help="door-to-door time budget; slower options are excluded from the recommendation")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    p.add_argument("--selftest", action="store_true", help="run built-in checks and exit")
    return p


def _run(args) -> int:
    if not (args.option or args.direct or args.split or args.json_in):
        raise ValueError("give at least one option via --direct / --split / --option / --json-in")
    if args.threshold < 0:
        raise ValueError("--threshold must be >= 0")
    if args.vot is not None and args.vot < 0:
        raise ValueError("--vot must be >= 0")
    if args.travelers < 1:
        raise ValueError("--travelers must be >= 1")
    if args.max_hours is not None and args.max_hours <= 0:
        raise ValueError("--max-hours must be > 0")

    options = [scale_option(o, args.travelers) for o in build_options(args)]
    unknown_modes = sorted({leg["mode"] for o in options for leg in o["legs"] if leg["mode_unknown"]})
    res = evaluate(options, threshold=args.threshold, vot=args.vot,
                   transfer_buffer=args.transfer_buffer,
                   max_hours=args.max_hours, travelers=args.travelers)
    if args.json:
        out = {k: v for k, v in res.items() if not k.startswith("_")}
        out["unknown_modes"] = unknown_modes
        print(json.dumps(out, indent=2))
    else:
        for m in unknown_modes:
            print(f"WARN  unrecognized leg mode '{m}' — priced anyway, check for a typo", file=sys.stderr)
        print(format_report(res, args.origin, args.dest))
    return 0


def main(argv=None):
    _force_utf8()
    p = _build_parser()
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()

    try:
        return _run(args)
    except (ValueError, FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


# --------------------------------------------------------------------------- self-test
def _approx(a, b, tol=0.01):
    return abs(a - b) <= tol


def selftest():
    failures = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    # Case 1: the headline case. Direct $620/5.5h; split $285/9h saves $335 (>=200) -> recommend split.
    opts = [
        parse_option("Fly direct | fly 620 5.5"),
        parse_option("DEN + Amtrak | fly 210 3.0 ; train 75 6.0"),
    ]
    r = evaluate(opts, threshold=200)
    check("split saving $335 is recommended over direct", r["recommended"] == "DEN + Amtrak")
    split_row = next(o for o in r["options"] if o["name"] == "DEN + Amtrak")
    check("savings computed as $335", _approx(split_row["savings_vs_baseline"], 335))
    check("break-even VOT = 335 / 3.5h ≈ $95.71/hr", _approx(split_row["breakeven_vot"], 95.71, 0.05))

    # Case 2: split only saves $150 (< $200) and is slower -> keep the direct flight.
    opts2 = [
        parse_option("Fly direct | fly 620 5.5"),
        parse_option("Split | fly 400 4.0 ; train 70 4.0"),
    ]
    r2 = evaluate(opts2, threshold=200)
    check("split saving only $150 is NOT recommended (< $200 rule)", r2["recommended"] == "Fly direct")
    sr2 = next(o for o in r2["options"] if o["name"] == "Split")
    check("that split is tagged cheaper_below_threshold", sr2["status"] == "cheaper_below_threshold")

    # Case 3: dominance — split cheaper $500 AND faster 4h beats direct $620/5.5h regardless of threshold.
    opts3 = [
        parse_option("Fly direct | fly 620 5.5"),
        parse_option("Secondary + train | fly 430 2.5 ; train 70 1.5"),
    ]
    r3 = evaluate(opts3, threshold=200)
    dom = next(o for o in r3["options"] if o["name"] == "Secondary + train")
    check("cheaper+faster split is dominant", dom["dominant"] and r3["recommended"] == "Secondary + train")

    # Case 4: value-of-time flips a marginal call. Split saves $250 but +5h; at $60/hr direct wins.
    opts4 = [
        parse_option("Fly direct | fly 600 4.0"),
        parse_option("Cheap + bus | fly 280 5.0 ; bus 70 4.0"),
    ]
    r4hi = evaluate(opts4, threshold=200, vot=60)   # time expensive -> direct
    r4lo = evaluate(opts4, threshold=200, vot=10)   # time cheap -> split
    check("high VOT ($60/hr) prefers the direct flight", r4hi["recommended"] == "Fly direct")
    check("low VOT ($10/hr) prefers the cheaper split", r4lo["recommended"] == "Cheap + bus")

    # Case 5: parsing robustness — $ and commas, bare 'cost hours' direct sugar.
    o5 = parse_option("X | fly $1,240 6")
    check("parses $1,240 -> 1240.0", _approx(o5["cost"], 1240.0))
    s5 = parse_option(sugar_direct("620 5.5"))
    check("bare 'cost hours' sugars to a fly leg", s5["legs"][0]["mode"] == "fly" and _approx(s5["cost"], 620))
    sp5 = parse_option(sugar_split("DEN via rail: fly 210 3 + train 75 4"))
    check("split sugar names + splits legs", sp5["name"] == "DEN via rail" and sp5["nlegs"] == 2)

    # Case 6: transfer buffer lengthens multi-leg time only.
    r6 = evaluate([parse_option("Fly direct | fly 620 5.5"),
                   parse_option("Split | fly 300 3 ; train 60 3")], threshold=200, transfer_buffer=1.0)
    sr6 = next(o for o in r6["options"] if o["name"] == "Split")
    check("transfer buffer adds 1h to the 1-transfer split", _approx(sr6["hours_eff"], 7.0))

    # Case 7: group math — trains scale per person, a rental doesn't; best split flips at n=4.
    check("scale_leg_cost: train ×4", _approx(scale_leg_cost("train", 75, 4), 300))
    check("scale_leg_cost: rental stays per-vehicle", _approx(scale_leg_cost("rental", 80, 4), 80))
    base7 = [parse_option("Fly direct | fly 620 5.5"),
             parse_option("DEN + train | fly 210 3.0 ; train 75 6.0"),
             parse_option("EGE + rental | fly 240 4.0 ; rental 80 4.5")]
    r7a = evaluate([scale_option(o, 1) for o in base7], threshold=200, travelers=1)
    r7b = evaluate([scale_option(o, 4) for o in base7], threshold=200, travelers=4)
    check("solo: train split wins (cheapest)", r7a["recommended"] == "DEN + train")
    check("group of 4: rental split overtakes the train split",
          r7b["recommended"] == "EGE + rental")
    rb = next(o for o in r7b["options"] if o["name"] == "EGE + rental")
    check("group rental split total = 4×240 + 80 = $1,040", _approx(rb["cost"], 1040))

    # Case 8: time budget — a qualifying but slow split is excluded under --max-hours.
    opts8 = [parse_option("Fly direct | fly 620 5.5"),
             parse_option("Slow split | fly 210 3.0 ; train 75 6.0")]
    r8 = evaluate(opts8, threshold=200, max_hours=8.0)
    sr8 = next(o for o in r8["options"] if o["name"] == "Slow split")
    check("9h split flagged over an 8h time budget", sr8["over_time_budget"] is True)
    check("time budget forces the direct recommendation", r8["recommended"] == "Fly direct")
    check("time_budget_binding reported", r8["time_budget_binding"] is True)
    r8b = evaluate(opts8, threshold=200, max_hours=2.0)   # nothing fits -> budget ignored
    check("impossible budget falls back to normal ranking", r8b["recommended"] == "Slow split")

    # Case 9: a malformed 1-leg "split" must error, not silently downgrade to a direct.
    try:
        parse_option(sugar_split("fly 300 4"), min_legs=2)
        check("1-leg sugar_split raises instead of silently downgrading", False)
    except ValueError:
        check("1-leg sugar_split raises instead of silently downgrading", True)
    ok_split = parse_option(sugar_split("fly 210 3 + train 75 4"), min_legs=2)
    check("a real 2-leg split still parses fine under min_legs=2", ok_split["nlegs"] == 2)

    # Case 10: --threshold >= 0 is enforced by the CLI, not just documented.
    rc_neg = main(["--direct", "fly 620 5.5", "--split", "DEN: fly 210 3 + train 75 4",
                   "--threshold", "-100"])
    check("negative --threshold is rejected with a clean exit code (no traceback)", rc_neg == 2)

    # Case 11: unknown leg modes are flagged, not silently priced as if nothing were wrong.
    o11 = parse_option("Weird | flght 200 3.0")
    check("typo'd mode 'flght' is flagged mode_unknown", o11["legs"][0]["mode_unknown"] is True)
    o11b = parse_option("Fine | fly 200 3.0 ; train 40 2.0")
    check("known modes are not flagged", not any(leg["mode_unknown"] for leg in o11b["legs"]))

    n_cases = 11
    print(f"\n{'ALL PASS' if not failures else str(len(failures)) + ' FAILED'} "
          f"({n_cases} cases)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
