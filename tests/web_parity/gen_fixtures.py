#!/usr/bin/env python3
"""
gen_fixtures.py - runs the real Python engine (hopandhaul.server.plan / hopandhaul.trip.evaluate)
over every case in cases.json and writes each result to fixtures/<name>.json.

This is one half of the web-parity gate: check.mjs (Node) runs the SAME cases through the JS
port under src/hopandhaul/ui/engine/ and deep-equals the two. If they disagree, the JS is wrong
 - fix the JS to match this output, never the other way around.

fixtures/ is regenerated every run (gitignored, not committed) rather than frozen: a couple of
cases exercise geo.fare_date_multiplier's booking-lead-time curve, which reads the real
system date when no explicit `today` is given (matching what plan() actually does - it has no
`today` parameter to override). Regenerating fresh each run, immediately before check.mjs reads
it, keeps both sides looking at "today" from the same few seconds of wall-clock time instead of
whatever day the fixtures happened to be committed on.

Run:  python tests/web_parity/gen_fixtures.py
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CASES_PATH = os.path.join(HERE, "cases.json")
OUT_DIR = os.path.join(HERE, "fixtures")
# check.mjs reads THIS, not cases.json, so both engines see byte-identical dates even if the
# two processes straddle midnight. See resolve_relative_dates() for why the dates move at all.
RESOLVED_CASES_PATH = os.path.join(OUT_DIR, "_cases.resolved.json")

_REL_DATE = re.compile(r"^([+-])(\d+)d$")
_REL_EOM = re.compile(r"^eom\+(\d+)m$")


def _end_of_month(today: datetime.date, months_ahead: int) -> datetime.date:
    """The last day of the month `months_ahead` months after the one `today` is in."""
    m = today.month - 1 + months_ahead
    y, m = today.year + m // 12, m % 12 + 1
    nxt = datetime.date(y + 1, 1, 1) if m == 12 else datetime.date(y, m + 1, 1)
    return nxt - datetime.timedelta(days=1)


def resolve_relative_dates(cases: list, today: datetime.date) -> list:
    """Turn "+70d" / "-30d" / "eom+2m" date fields into real YYYY-MM-DD, relative to `today`.

    Hardcoded dates rot. Three of these cases were pinned to 2026-07-10 and 2026-08-15 to test
    the booking-lead-time curve; once those days passed, fare_date_multiplier() started
    returning a neutral 1.0 for all of them, so `date_far_out_aspen` and `date_close_in_aspen`
    were quietly testing the same undated code path as each other. They still PASSED, because
    both engines agreed about doing nothing. A parity gate that agrees on the wrong thing is
    worse than no gate, so date-sensitive cases now express an offset instead of a date.

    Offsets are chosen so the multiplier is never exactly 1.0 no matter what day CI runs on:
    +5d and +70d and +200d land in three different LEAD_CURVE buckets, verified against every
    possible "today" across a full year.

    "eom+2m" is the last day of the month two months out. A plain day offset can't pin a date
    sweep to a month boundary - which month a "+70d" anchor lands in moves with the calendar -
    and crossing one is the whole point of the dates_month_boundary case: it is where naive
    day arithmetic (and a local-time Date constructor) breaks on one side and not the other.
    """
    out = []
    for case in cases:
        case = json.loads(json.dumps(case))          # don't mutate the caller's parsed JSON
        params = case.get("params")
        if isinstance(params, dict):
            for field in ("date", "ret"):
                raw = str(params.get(field) or "")
                m = _REL_DATE.match(raw)
                if m:
                    sign = -1 if m.group(1) == "-" else 1
                    params[field] = (today + datetime.timedelta(days=sign * int(m.group(2)))).isoformat()
                    continue
                m = _REL_EOM.match(raw)
                if m:
                    params[field] = _end_of_month(today, int(m.group(1))).isoformat()
        out.append(case)
    return out

# Make sure "hopandhaul" imports even if the package isn't pip-installed in this environment.
sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))

from hopandhaul import dates, trip  # noqa: E402
from hopandhaul.server import (  # noqa: E402
    ValidationError,
    parse_plan_params,
    plan,
    sweep_dates,
)


def build_option_string(opt: dict) -> str:
    legs = " ; ".join(f"{leg['mode']} {leg['cost']} {leg['hours']}" for leg in opt["legs"])
    return f"{opt['name']} | {legs}"


def run_plan_case(case: dict) -> dict:
    p = case["params"]
    return plan(
        p["dest_lat"], p["dest_lng"],
        origin_iata=p.get("origin_iata", "JFK"),
        date=p.get("date"),
        vot=p.get("vot"),
        threshold=p.get("threshold", trip.DEFAULT_THRESHOLD),
        max_ground_h=p.get("max_ground_h", 6.0),
        roundtrip=p.get("roundtrip", False),
        fetch_weather=False,
        travelers=p.get("travelers", 1),
        ret=p.get("ret"),
        transfer_buffer=p.get("transfer_buffer", 1.0),
        allow_live=False, allow_transit=False,
    )


def run_evaluate_case(case: dict) -> dict:
    travelers = case.get("travelers", 1)
    # Every real call site (server.py's plan(), trip.py's own CLI _run()) scales each option's
    # leg costs by travelers BEFORE evaluate() ever sees them - evaluate()'s own `travelers`
    # arg is metadata only, it doesn't re-price anything. Match that here so a case that sets
    # "travelers" actually exercises scale_option's group math, not just the metadata field.
    options = [trip.scale_option(trip.parse_option(build_option_string(o)), travelers)
               for o in case["options"]]
    res = trip.evaluate(
        options,
        threshold=case.get("threshold", trip.DEFAULT_THRESHOLD),
        vot=case.get("vot"),
        transfer_buffer=case.get("transfer_buffer", 0.0),
        max_hours=case.get("max_hours"),
        travelers=travelers,
    )
    return {k: v for k, v in res.items() if not k.startswith("_")}


def run_dates_case(case: dict) -> dict:
    p = case["params"]
    # allow_live/allow_transit off for the same reason run_plan_case has them off: the browser
    # engine has neither, so those branches have nothing to be in parity with. What this case
    # type actually pins is the window (which dates are in it, in what order), the per-date
    # rows, the savings arithmetic and the tie-break.
    return sweep_dates(
        p["dest_lat"], p["dest_lng"],
        origin_iata=p.get("origin_iata", "JFK"),
        date=p["date"],
        window=p.get("window", 1),
        vot=p.get("vot"),
        threshold=p.get("threshold", trip.DEFAULT_THRESHOLD),
        max_ground_h=p.get("max_ground_h", 6.0),
        roundtrip=p.get("roundtrip", False),
        travelers=p.get("travelers", 1),
        ret=p.get("ret"),
        transfer_buffer=p.get("transfer_buffer", 1.0),
        allow_live=False, allow_transit=False, fetch_weather=False,
    )


def run_dates_helpers_case(case: dict) -> dict:
    """The two dates.py helpers a whole-sweep case can't reach, pinned directly.

    basis_of_legs' guard order is load-bearing and invisible to a sweep fixture: every option
    plan() builds has a flight leg, so the empty-flight-leg branch (all([]) is True in Python
    and [].every() is true in JS, so a ground-only option would label itself "live") never comes
    up there. Same for the window: a sweep reads the real clock, so it can only ever produce the
    window that today allows, not a leap day or a year rollover.

    These are the only cases carrying literal dates, and they can't rot the way the old pinned
    cases did: `today` is passed in rather than read off the clock, and nothing here touches
    fare_date_multiplier, so the answer is the same in 2030 as it is now.
    """
    p = case["params"]
    return {
        "dates": dates.candidate_dates(p["anchor"], p["window"],
                                       today=datetime.date.fromisoformat(p["today"])),
        "basis": dates.basis_of_legs(p["legs"]),
    }
def run_validate_case(case: dict) -> dict:
    """The trust boundary: raw pre-validation strings in, normalized params or an error out.

    server.parse_plan_params() and ui/engine/validate.js parsePlanParams() are hand-kept
    mirrors guarding untrusted input (a query string here, a hand-edited share URL in the
    browser). Comparing them here means a divergence like the Unicode-digit date one is a
    red CI job instead of two engines quietly disagreeing about what is valid.
    """
    q = {k: [v] for k, v in case["params"].items()}
    try:
        return {"ok": True, "params": parse_plan_params(q)}
    except ValidationError as e:
        return {"ok": False, "error": str(e)}


def run_case(case: dict) -> dict:
    if case["type"] == "plan":
        return run_plan_case(case)
    if case["type"] == "evaluate":
        return run_evaluate_case(case)
    if case["type"] == "dates":
        return run_dates_case(case)
    if case["type"] == "dates_helpers":
        return run_dates_helpers_case(case)
    if case["type"] == "validate":
        return run_validate_case(case)
    raise ValueError(f"unknown case type {case['type']!r} in {case.get('name')!r}")


def main() -> int:
    with open(CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)

    names = [c["name"] for c in cases]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        print(f"error: duplicate case names in cases.json: {sorted(dupes)}", file=sys.stderr)
        return 2

    os.makedirs(OUT_DIR, exist_ok=True)
    cases = resolve_relative_dates(cases, datetime.date.today())
    with open(RESOLVED_CASES_PATH, "w", encoding="utf-8") as f:
        json.dump(cases, f, indent=2)
        f.write("\n")

    for case in cases:
        try:
            out = run_case(case)
        except Exception as e:
            print(f"error generating fixture {case['name']!r}: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        with open(os.path.join(OUT_DIR, f"{case['name']}.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, sort_keys=True)
            f.write("\n")

    print(f"wrote {len(cases)} fixtures to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
