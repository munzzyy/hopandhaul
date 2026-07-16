#!/usr/bin/env python3
"""
dates.py - "which date is actually cheapest" sweep for travel-scout.

Every other hopandhaul command takes one --date and prices that single day. This sweeps
a bounded window of candidate dates around it and calls duffel.build_and_evaluate() - the
SAME per-date pricing/evaluation primitive `hopandhaul duffel` and the map UI already
trust - once per date, then reports which one actually comes out cheapest. No fare logic
lives here; this only decides which date to ask about and picks the winner.

Live Duffel pricing is used for every date when a key is configured; build_and_evaluate
already falls back leg-by-leg to geo.py's calibrated estimate whenever Duffel has no offer
for a route/date (see duffel.py's _price_flight_cli), and with no key at all every date is
priced by the estimator alone, same as `hopandhaul duffel`/the map UI with no key. Each
date's result says which basis actually won: "live" (every flight leg on the recommended
option came from a real Duffel query), "estimate" (none did), or "mixed" (some did, some
fell back per-leg - a thin gateway route Duffel doesn't carry that day, say).

The window is centered on --date and bounded (default 3 days each way, capped at
MAX_WINDOW) so a naive invocation can't fire off dozens of live Duffel calls. Dates before
today are skipped, not priced - there's no fare to check for a day that's already gone.
Repeated sweeps (or a --date already covered by an earlier `hopandhaul duffel` call) ride
duffel.py's own per-date offer cache, so overlapping windows cost nothing extra.

Examples:
  hopandhaul dates --from JFK --to ASE --date 2026-08-15 --window 3 --auto-gateways
  python -m hopandhaul.dates --from JFK --to ASE --date 2026-08-15 \
      --return-date 2026-08-22 --window 2
  python -m hopandhaul.dates --selftest                              # offline, no network
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date as _date, timedelta

from . import duffel, trip
from .integrations import net

DEFAULT_WINDOW = 3
MAX_WINDOW = 7


# --------------------------------------------------------------------------- window/basis
def _candidate_dates(anchor: str, window: int) -> list[str]:
    """anchor +/- window days, in order, YYYY-MM-DD strings - dates before today are
    dropped (no fare to check for a day that's already gone). Always includes the anchor
    itself when it isn't in the past (window=0 -> just [anchor], or [] if anchor is past)."""
    base = _date.fromisoformat(anchor)
    today = _date.today()
    out = []
    for delta in range(-window, window + 1):
        d = base + timedelta(days=delta)
        if d >= today:
            out.append(d.isoformat())
    return out


def _basis(rec_row: dict) -> str:
    """'live' / 'estimate' / 'mixed' for a recommended option's FLIGHT leg(s). Ground legs
    out of build_and_evaluate are always distance estimates (see duffel.py's
    _ground_leg_spec_cli), so only the fly legs say anything about whether a real Duffel
    query - not the calibrated model - actually set the price for this date."""
    flight_live = [leg["is_live"] for leg in rec_row["itinerary"]["legs"] if leg["mode"] == "fly"]
    if not flight_live or not any(flight_live):
        return "estimate"
    if all(flight_live):
        return "live"
    return "mixed"


# --------------------------------------------------------------------------- the sweep
def sweep(origin: str, dest: str, date: str, gateways: list[dict], adults: int = 1,
         cabin: str = "economy", nonstop: bool = False, vot: float | None = None,
         transfer_buffer: float = 1.0, threshold: float = trip.DEFAULT_THRESHOLD,
         return_date: str | None = None, window: int = DEFAULT_WINDOW) -> dict:
    """Price `date` +/- window days with duffel.build_and_evaluate(), one call per
    candidate date, and report which one is actually cheapest.

    return_date (if given) shifts by the same number of days as its paired departure date,
    so a round trip's LENGTH stays fixed while its placement in the window moves - this
    answers "is next week cheaper for the same week-long trip?", not "what if I only
    stayed one night?"

    A candidate date whose live lookup fails (a transient Duffel/network error) is recorded
    with an 'error' field and excluded from the winner search rather than aborting the whole
    sweep - one bad call shouldn't hide a good answer from every other date.
    """
    if not (0 <= window <= MAX_WINDOW):
        raise ValueError(f"--window must be between 0 and {MAX_WINDOW}")
    base = _date.fromisoformat(date)
    trip_len = None
    if return_date:
        trip_len = (_date.fromisoformat(return_date) - base).days
        if trip_len < 0:
            raise ValueError("--return-date must be on or after --date")

    rows = []
    for cand_s in _candidate_dates(date, window):
        cand = _date.fromisoformat(cand_s)
        cand_ret = (cand + timedelta(days=trip_len)).isoformat() if trip_len is not None else None
        try:
            res, warnings = duffel.build_and_evaluate(
                origin, dest, cand_s, gateways, adults, cabin, nonstop,
                vot, transfer_buffer, threshold, return_date=cand_ret)
        except net.FetchError as e:
            rows.append({"date": cand_s, "return_date": cand_ret, "error": str(e)})
            continue
        rec = res["_recommended_row"]
        rows.append({
            "date": cand_s, "return_date": cand_ret, "recommended": res["recommended"],
            "cost": rec["cost"], "hours": rec["hours_eff"], "basis": _basis(rec),
            "warnings": warnings, "result": res,
        })

    if not rows:
        raise ValueError("no candidate dates left in range - every date in the window is "
                         "already in the past")
    priced = [r for r in rows if "error" not in r]
    if not priced:
        raise ValueError("every candidate date failed to price - see the per-date errors")
    best = min(priced, key=lambda r: (r["cost"], r["hours"]))
    return {"origin": origin.upper(), "dest": dest.upper(), "anchor_date": date,
           "window": window, "dates": rows, "best": best}


# --------------------------------------------------------------------------- reporting
def _fmt_money(x: float) -> str:
    """Mirrors trip.py's private _fmt_money - duplicated here rather than reached into
    across modules, same one-function-worth-of-footprint every other module keeps local."""
    return f"${x:,.0f}" if abs(x - round(x)) < 0.005 else f"${x:,.2f}"


def _fmt_hours(h: float) -> str:
    total_min = round(h * 60)
    hh, mm = divmod(total_min, 60)
    return f"{hh}h{mm:02d}" if mm else f"{hh}h"


def format_sweep(out: dict, origin: str, dest: str) -> str:
    L = [f"CHEAPEST DATE SWEEP: {origin.upper()} -> {dest.upper()}",
        f"anchor {out['anchor_date']} +/- {out['window']} day(s) "
        f"({len(out['dates'])} date(s) checked; dates already past are skipped)", ""]
    for r in out["dates"]:
        if "error" in r:
            L.append(f"   {r['date']}   -- lookup failed: {r['error']}")
            continue
        mark = "→" if r is out["best"] else " "
        rt = f"  (ret {r['return_date']})" if r["return_date"] else ""
        L.append(f" {mark} {r['date']}{rt}   {_fmt_money(r['cost']).rjust(8)}   "
                 f"{_fmt_hours(r['hours']).rjust(6)}   {r['basis']:<8}   {r['recommended']}")
    L.append("")
    b = out["best"]
    L.append(f"CHEAPEST: {b['date']} - {_fmt_money(b['cost'])} via {b['recommended']} "
             f"({b['basis']}-priced)")
    if any(r.get("basis") != "live" for r in out["dates"] if "error" not in r):
        L.append("  some dates above used the calibrated ESTIMATE, not a confirmed live "
                 "fare - re-verify before booking (see the basis column).")
    L.append("")
    L.append("Full report for the cheapest date:")
    L.append("")
    L.append(trip.format_report(b["result"], origin, dest))
    itin = duffel.format_itineraries(b["result"])
    if itin:
        L.append("")
        L.append(itin)
    return "\n".join(L)


def _json_summary(out: dict) -> dict:
    """The sweep's JSON shape drops each date's full 'result' (nested itinerary objects
    carry raw datetimes, which aren't JSON-serializable) - it's a summary command, not a
    dump of every itinerary. Re-run `hopandhaul duffel --date <that date>` for the full
    leg-by-leg breakdown of a specific date."""
    def trim(r):
        return {k: v for k, v in r.items() if k != "result"}
    return {"origin": out["origin"], "dest": out["dest"], "anchor_date": out["anchor_date"],
           "window": out["window"], "dates": [trim(r) for r in out["dates"]],
           "best": trim(out["best"])}


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    trip._force_utf8()
    p = argparse.ArgumentParser(
        description="Sweep dates around --date and report the actual cheapest one to fly "
                    "- same per-date pricing `hopandhaul duffel` uses.")
    p.add_argument("--from", dest="origin", help="origin airport IATA (e.g. JFK)")
    p.add_argument("--to", dest="dest", help="final destination airport IATA (e.g. ASE)")
    p.add_argument("--date", help="anchor departure date YYYY-MM-DD - the window centers here")
    p.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                   help=f"days to check each side of --date (default {DEFAULT_WINDOW}, "
                        f"max {MAX_WINDOW}); {2 * DEFAULT_WINDOW + 1} dates checked by default")
    p.add_argument("--return-date", dest="return_date", default=None,
                   help="return date YYYY-MM-DD for the ANCHOR date - shifts by the same "
                        "amount as each candidate departure, so trip length stays fixed")
    p.add_argument("--gateway", action="append", default=[],
                   help="'HUB_IATA mode ground_cost ground_hours' (repeatable)")
    p.add_argument("--auto-gateways", action="store_true",
                   help="pull gateway suggestions from gateways.json for --to")
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--cabin", default="economy",
                   choices=["economy", "premium_economy", "business", "first"])
    p.add_argument("--nonstop", action="store_true", help="prefer nonstop offers")
    p.add_argument("--vot", type=float, default=None, help="value of time $/hr")
    p.add_argument("--transfer-buffer", type=float, default=1.0,
                   help="hours added per connection (default 1.0; splits are separate tickets)")
    p.add_argument("--threshold", type=float, default=trip.DEFAULT_THRESHOLD)
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()

    if not (args.origin and args.dest and args.date):
        p.error("--from, --to and --date are required")

    if not duffel.have_keys():
        print("(No DUFFEL_API_KEY configured — every date below is priced with distance "
              "ESTIMATES, same as `hopandhaul duffel` with no key. See README.md.)\n")

    gateways = [duffel.parse_gateway_arg(g) for g in args.gateway]
    if args.auto_gateways:
        gateways += duffel.auto_gateways(args.dest)

    try:
        out = sweep(args.origin, args.dest, args.date, gateways, args.adults, args.cabin,
                   args.nonstop, args.vot, args.transfer_buffer, args.threshold,
                   return_date=args.return_date, window=args.window)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except net.FetchError as e:
        print(f"Duffel request failed: {e}")
        return 3

    if args.json:
        print(json.dumps(_json_summary(out), indent=2))
        return 0

    print(format_sweep(out, args.origin, args.dest))
    return 0


# --------------------------------------------------------------------------- self-test (offline)
def selftest():
    trip._force_utf8()
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    # window bounds: an anchor far in the future so nothing gets today-filtered.
    cds = _candidate_dates("2030-06-15", 2)
    check("window=2 checks 5 dates (anchor +/- 2 days)", cds == [
        "2030-06-13", "2030-06-14", "2030-06-15", "2030-06-16", "2030-06-17"])
    check("window=0 checks only the anchor itself", _candidate_dates("2030-06-15", 0) == ["2030-06-15"])
    check("a fully-past anchor+window drops every candidate",
          _candidate_dates("2000-01-01", 3) == [])

    try:
        sweep("JFK", "ASE", "2030-06-15", [], window=MAX_WINDOW + 1)
        check(f"--window above {MAX_WINDOW} is rejected", False)
    except ValueError:
        check(f"--window above {MAX_WINDOW} is rejected", True)
    try:
        sweep("JFK", "ASE", "2030-06-15", [], window=-1)
        check("a negative --window is rejected", False)
    except ValueError:
        check("a negative --window is rejected", True)

    # basis labeling: live / estimate / mixed, from a hand-built recommended row.
    def row(flags):
        return {"itinerary": {"legs": [{"mode": "fly", "is_live": f} for f in flags]
                              + [{"mode": "bus", "is_live": False}]}}
    check("all fly legs live -> 'live'", _basis(row([True])) == "live")
    check("no fly legs live -> 'estimate'", _basis(row([False])) == "estimate")
    check("some fly legs live, some not -> 'mixed'", _basis(row([True, False])) == "mixed")
    check("a recommendation with no flight leg at all still resolves ('estimate', not a crash)",
          _basis({"itinerary": {"legs": [{"mode": "bus", "is_live": False}]}}) == "estimate")

    # end-to-end sweep, offline (mocked build_and_evaluate): must call the real primitive
    # once per candidate date and pick the actual minimum, not just the first or the anchor.
    import unittest.mock as _mock

    # date -> (price, is_live) for the direct option this fake returns.
    PRICES = {"2030-06-13": (610.0, True), "2030-06-14": (590.0, True),
             "2030-06-15": (450.0, True), "2030-06-16": (700.0, False),
             "2030-06-17": (520.0, True)}
    calls = []

    def _fake_leg(mode, cost, hours, is_live, iata_from, iata_to):
        # same row shape itinerary.build_timeline() actually returns (see its docstring) -
        # format_sweep() renders this for real, so the fake needs every key that reaches,
        # not just the ones _basis() reads.
        return {"mode": mode, "from": {"iata": iata_from, "name": iata_from},
               "to": {"iata": iata_to, "name": iata_to}, "depart_clock": "09:00",
               "depart_day": "Day 1", "arrive_clock": "14:00", "arrive_day": "Day 1",
               "duration_h": hours, "checkin_by": None, "cost": cost,
               "price_basis": "live Duffel fare" if is_live else "distance ESTIMATE",
               "verify_url": "https://example.invalid/verify", "is_live": is_live,
               "carrier": "Fake Air" if is_live else None,
               "flight_number": "FA1" if is_live else None}

    def _fake_build_and_evaluate(origin, dest, date, gateways, adults, cabin, nonstop,
                                 vot, transfer_buffer, threshold, return_date=None):
        calls.append(date)
        price, live = PRICES[date]
        opts = [trip.parse_option(f"Fly direct to {dest} | fly {price} 5.0")]
        res = trip.evaluate(opts, threshold=threshold, vot=vot,
                            transfer_buffer=transfer_buffer, travelers=adults)
        for o in res["options"]:
            leg = _fake_leg("fly", price, 5.0, live, origin, dest)
            o["itinerary"] = {"legs": [leg], "any_live": live, "example_day": not live,
                              "depart_local": "09:00"}
        return res, ([] if live else [f"No live Duffel offer {origin}->{dest}; "
                                      "priced with a distance ESTIMATE."])

    with _mock.patch.object(duffel, "build_and_evaluate", side_effect=_fake_build_and_evaluate):
        out = sweep("JFK", "ASE", "2030-06-15", [], window=2)
    check("sweep calls build_and_evaluate exactly once per candidate date",
          sorted(calls) == sorted(PRICES.keys()))
    check("sweep finds the actual minimum, not the anchor or the first date checked",
          out["best"]["date"] == "2030-06-15" and out["best"]["cost"] == 450.0)
    check("the winning date is labeled by its real basis (live)", out["best"]["basis"] == "live")
    off_day = next(r for r in out["dates"] if r["date"] == "2030-06-16")
    check("a date whose flight fell back to an estimate is labeled 'estimate', not silently 'live'",
          off_day["basis"] == "estimate")
    check("format_sweep renders every checked date plus a cheapest-date summary line",
          format_sweep(out, "JFK", "ASE").count("2030-06-1") >= 6)   # 5 rows + the CHEAPEST line

    # return-date shifting: the trip LENGTH (7 nights) must stay fixed while the departure
    # date moves across the window, not the absolute return date.
    with _mock.patch.object(duffel, "build_and_evaluate", side_effect=_fake_build_and_evaluate):
        out_rt = sweep("JFK", "ASE", "2030-06-15", [], window=1, return_date="2030-06-22")
    rows_by_date = {r["date"]: r for r in out_rt["dates"]}
    check("return-date shifts with its departure date, preserving a 7-night trip",
          rows_by_date["2030-06-14"]["return_date"] == "2030-06-21"
          and rows_by_date["2030-06-16"]["return_date"] == "2030-06-23")
    try:
        sweep("JFK", "ASE", "2030-06-15", [], return_date="2030-06-01")
        check("--return-date before --date is rejected", False)
    except ValueError:
        check("--return-date before --date is rejected", True)

    # a per-date lookup failure degrades that ONE date, not the whole sweep.
    def _flaky(origin, dest, date, gateways, adults, cabin, nonstop, vot, transfer_buffer,
              threshold, return_date=None):
        if date == "2030-06-16":
            raise net.FetchError("simulated timeout")
        return _fake_build_and_evaluate(origin, dest, date, gateways, adults, cabin, nonstop,
                                        vot, transfer_buffer, threshold, return_date)

    with _mock.patch.object(duffel, "build_and_evaluate", side_effect=_flaky):
        out_flaky = sweep("JFK", "ASE", "2030-06-15", [], window=2)
    errored = next(r for r in out_flaky["dates"] if r["date"] == "2030-06-16")
    check("a date whose lookup fails is recorded with an error, not a crash",
          "error" in errored and "timeout" in errored["error"])
    check("the sweep still finds the real minimum among the dates that DID price",
          out_flaky["best"]["date"] == "2030-06-15" and out_flaky["best"]["cost"] == 450.0)

    # cache reuse: two overlapping sweeps must not re-fetch a date they share. Patched below
    # search_cheapest's OWN cache check (in _OFFER_CACHE), not search_cheapest itself, so the
    # test proves the real cache - not just that a mock wasn't called twice by coincidence.
    net_calls = {"n": 0}

    def _counting_create_offer_request(origin, dest, date, adults=1, cabin="economy",
                                       timeout=30, return_date=None):
        net_calls["n"] += 1
        return "req_fake"

    def _fake_get_offers(rid, limit=10, timeout=30):
        return [{"total_amount": "300.00", "total_currency": "USD", "owner": {"iata_code": "AA"},
                 "slices": [{"duration": "PT5H", "segments": []}]}]

    with _mock.patch.object(duffel, "have_keys", return_value=True), \
         _mock.patch.object(duffel, "create_offer_request",
                            side_effect=_counting_create_offer_request), \
         _mock.patch.object(duffel, "get_offers", side_effect=_fake_get_offers):
        sweep("JFK", "ASE", "2030-07-01", [], window=1)          # prices 06-30, 07-01, 07-02
        after_first = net_calls["n"]
        sweep("JFK", "ASE", "2030-07-02", [], window=1)          # 07-01, 07-02 already cached
    check("a second, overlapping sweep only prices the ONE genuinely new date",
          net_calls["n"] - after_first == 1)

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (offline checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
