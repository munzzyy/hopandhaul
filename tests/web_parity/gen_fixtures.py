#!/usr/bin/env python3
"""
gen_fixtures.py — runs the real Python engine (hopandhaul.server.plan / hopandhaul.trip.evaluate)
over every case in cases.json and writes each result to fixtures/<name>.json.

This is one half of the web-parity gate: check.mjs (Node) runs the SAME cases through the JS
port under src/hopandhaul/ui/engine/ and deep-equals the two. If they disagree, the JS is wrong
— fix the JS to match this output, never the other way around.

fixtures/ is regenerated every run (gitignored, not committed) rather than frozen: a couple of
cases exercise geo.fare_date_multiplier's booking-lead-time curve, which reads the real
system date when no explicit `today` is given (matching what plan() actually does — it has no
`today` parameter to override). Regenerating fresh each run, immediately before check.mjs reads
it, keeps both sides looking at "today" from the same few seconds of wall-clock time instead of
whatever day the fixtures happened to be committed on.

Run:  python tests/web_parity/gen_fixtures.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CASES_PATH = os.path.join(HERE, "cases.json")
OUT_DIR = os.path.join(HERE, "fixtures")

# Make sure "hopandhaul" imports even if the package isn't pip-installed in this environment.
sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))

from hopandhaul import trip  # noqa: E402
from hopandhaul.server import plan  # noqa: E402


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
    # leg costs by travelers BEFORE evaluate() ever sees them — evaluate()'s own `travelers`
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


def run_case(case: dict) -> dict:
    if case["type"] == "plan":
        return run_plan_case(case)
    if case["type"] == "evaluate":
        return run_evaluate_case(case)
    raise ValueError(f"unknown case type {case['type']!r} in {case.get('name')!r}")


def main() -> int:
    with open(CASES_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)

    names = [c["name"] for c in cases]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        print(f"error: duplicate case names in cases.json: {sorted(dupes)}", file=sys.stderr)
        return 2

    os.makedirs(OUT_DIR, exist_ok=True)
    for case in cases:
        try:
            out = run_case(case)
        except Exception as e:  # noqa: BLE001 — a fixture that can't generate is a hard failure
            print(f"error generating fixture {case['name']!r}: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        with open(os.path.join(OUT_DIR, f"{case['name']}.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, sort_keys=True)
            f.write("\n")

    print(f"wrote {len(cases)} fixtures to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
