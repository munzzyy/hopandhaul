#!/usr/bin/env python3
"""
transit.py — REAL ground-transport schedules via Transitous (api.transitous.org), the
community-run MOTIS journey planner over worldwide GTFS. Keyless, free for open-source /
non-commercial use (hopandhaul qualifies), CORS-open (the browser build calls it too).

What it changes: ground legs used to be pure distance formulas. When Transitous knows the
route, a leg now carries the real thing — actual operators (DB Fernverkehr, Viking Line,
Washington State Ferries), a real departure time, and a real door-to-door duration. FARES
stay estimates (GTFS almost never ships them); the per-leg provenance says exactly which
half is real.

Policy compliance (https://transitous.org/api/): identifying User-Agent, cached, low volume
(a handful of lookups per map click, TTL-cached), attribution link in the README/UI. A
circuit breaker backs off entirely after repeated failures so an offline machine doesn't
hang every click on a dead socket.

Run:  python -m hopandhaul.transit 60.17 24.94 59.44 24.75 --date 2026-07-16
      python -m hopandhaul.transit --selftest     (offline, no network)
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import threading
import time
import urllib.parse

from . import __version__
from .integrations import net

BASE = "https://api.transitous.org/api/v2/plan"
UA = f"hopandhaul/{__version__} (https://github.com/munzzyy/hopandhaul)"
ATTRIBUTION_URL = "https://transitous.org/sources/"

_CACHE = net.TTLCache(ttl_seconds=600, max_size=256)

# circuit breaker: after this many consecutive network failures, stop trying for a while —
# an offline laptop must not pay a connect-timeout on every map click.
_BREAKER_THRESHOLD = 2
_BREAKER_COOLDOWN_S = 600
_breaker = {"failures": 0, "open_until": 0.0}
_breaker_lock = threading.Lock()

# MOTIS mode vocabulary -> the engine's leg modes. Local access modes (tram/subway/walk)
# are kept in the leg list but never chosen as the "main" mode of an intercity leg.
_MODE_MAP = {
    "HIGHSPEED_RAIL": "train", "LONG_DISTANCE": "train", "NIGHT_RAIL": "train",
    "REGIONAL_RAIL": "train", "REGIONAL_FAST_RAIL": "train", "RAIL": "train",
    "METRO": "transit", "SUBWAY": "transit", "TRAM": "transit",
    "BUS": "bus", "COACH": "bus",
    "FERRY": "ferry",
    "WALK": "walk", "BIKE": "walk", "CAR": "drive", "ODM": "bus",
    "AIRPLANE": "fly",
}


def _breaker_open() -> bool:
    with _breaker_lock:
        return time.time() < _breaker["open_until"]


def _breaker_record(ok: bool):
    with _breaker_lock:
        if ok:
            _breaker["failures"] = 0
        else:
            _breaker["failures"] += 1
            if _breaker["failures"] >= _BREAKER_THRESHOLD:
                _breaker["open_until"] = time.time() + _BREAKER_COOLDOWN_S
                _breaker["failures"] = 0


def _leg_summary(leg: dict) -> dict:
    mode = _MODE_MAP.get(str(leg.get("mode", "")).upper(), "transit")
    return {
        "mode": mode,
        "agency": leg.get("agencyName") or None,
        "route": leg.get("routeShortName") or None,
        "duration_h": round((leg.get("duration") or 0) / 3600.0, 2),
        "depart": (leg.get("startTime") or "")[11:16] or None,
    }


def _summarize(itin: dict) -> dict:
    legs = [_leg_summary(x) for x in itin.get("legs", [])]
    riding = [x for x in legs if x["mode"] not in ("walk",)]
    main = max(riding, key=lambda x: x["duration_h"]) if riding else None
    first_ride = next((x for x in riding), None)
    return {
        "duration_h": round((itin.get("duration") or 0) / 3600.0, 2),
        "transfers": itin.get("transfers", max(0, len(riding) - 1)),
        "legs": legs,
        "main_mode": main["mode"] if main else None,
        "main_agency": main["agency"] if main else None,
        "main_route": main["route"] if main else None,
        "depart": first_ride["depart"] if first_ride else None,
    }


def ground_options(from_lat: float, from_lng: float, to_lat: float, to_lng: float,
                   date: str | None = None, prefer_mode: str | None = None,
                   timeout: int = 8) -> dict | None:
    """Real scheduled journeys between two points. Returns the best itinerary summary plus
    how many the planner offered, or None (no coverage / network down / breaker open).

    prefer_mode: when the engine already chose a leg mode ("ferry", "train", "bus"), pick the
    fastest itinerary WHOSE MAIN MODE MATCHES it if one exists — the point is to put real
    times on the leg we're already showing, not to silently swap it for a different mode."""
    if _breaker_open():
        return None
    if not date:
        # schedules need a concrete day; a week out is a sane, mostly-cache-friendly default
        date = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
    key = (round(from_lat, 3), round(from_lng, 3), round(to_lat, 3), round(to_lng, 3),
           date, prefer_mode)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached or None
    params = {
        "fromPlace": f"{from_lat},{from_lng}",
        "toPlace": f"{to_lat},{to_lng}",
        "time": f"{date}T06:00:00Z",
    }
    url = BASE + "?" + urllib.parse.urlencode(params)
    try:
        out = net.fetch_json(url, headers={"User-Agent": UA, "Accept": "application/json"},
                             timeout=timeout, max_retries=0)
        _breaker_record(True)
    except (net.FetchError, OSError, ValueError):
        _breaker_record(False)
        _CACHE.set(key, {})           # negative-cache this exact lookup for the TTL
        return None
    itins = [_summarize(i) for i in out.get("itineraries", [])]
    itins = [i for i in itins if i["duration_h"] > 0 and i["main_mode"]]
    if not itins:
        _CACHE.set(key, {})
        return None
    pool = itins
    if prefer_mode:
        matching = [i for i in itins if i["main_mode"] == prefer_mode]
        if matching:
            pool = matching
    best = min(pool, key=lambda i: i["duration_h"])
    result = {**best, "n_options": len(itins), "date": date, "source": "transitous"}
    _CACHE.set(key, result)
    return result


def describe(t: dict) -> str:
    """One honest line for provenance: what's real (schedule) and what isn't (the fare)."""
    riding = [x for x in t["legs"] if x["mode"] not in ("walk",)]
    hops = " + ".join(
        (f"{x['agency']} {x['route']}".strip() if x["agency"] or x["route"] else x["mode"])
        for x in riding) or t.get("main_mode") or "transit"
    dep = f" departing {t['depart']}" if t.get("depart") else ""
    return (f"live schedule (Transitous, {t['date']}): {hops}, "
            f"{t['duration_h']:g}h door-to-door{dep}; "
            f"{t['n_options']} scheduled option(s) found")


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    p = argparse.ArgumentParser(description="Real ground schedules via Transitous (keyless).")
    p.add_argument("coords", nargs="*", help="FROM_LAT FROM_LNG TO_LAT TO_LNG")
    p.add_argument("--date", default=None, help="YYYY-MM-DD")
    p.add_argument("--mode", default=None, help="prefer itineraries whose main mode matches")
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
    if len(args.coords) != 4:
        p.error("give FROM_LAT FROM_LNG TO_LAT TO_LNG")
    lat1, lng1, lat2, lng2 = (float(x) for x in args.coords)
    t = ground_options(lat1, lng1, lat2, lng2, date=args.date, prefer_mode=args.mode)
    if not t:
        print("no scheduled journey found (or Transitous unreachable)")
        return 1
    if args.json:
        print(json.dumps(t, indent=2, ensure_ascii=False))
    else:
        print(describe(t))
    return 0


# --------------------------------------------------------------------------- self-test (offline)
def selftest() -> int:
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    itin = {"duration": 11940, "transfers": 0, "legs": [
        {"mode": "WALK", "duration": 60, "startTime": "2026-07-16T07:16"},
        {"mode": "TRAM", "agencyName": "HSL", "routeShortName": "9",
         "duration": 600, "startTime": "2026-07-16T07:17"},
        {"mode": "FERRY", "agencyName": "Viking Line", "duration": 9900,
         "startTime": "2026-07-16T07:30"},
        {"mode": "BUS", "agencyName": "Tallinna Linnatransport", "routeShortName": "2",
         "duration": 240, "startTime": "2026-07-16T10:23"},
    ]}
    s = _summarize(itin)
    check("summary totals hours from seconds", abs(s["duration_h"] - 3.32) < 0.02)
    check("main leg is the longest riding leg (the ferry), not the tram",
          s["main_mode"] == "ferry" and s["main_agency"] == "Viking Line")
    check("departure clock comes from the first riding leg", s["depart"] == "07:17")
    check("walk legs survive in the leg list but never become main",
          any(x["mode"] == "walk" for x in s["legs"]) and s["main_mode"] != "walk")

    d = describe({**s, "n_options": 5, "date": "2026-07-16", "source": "transitous"})
    check("describe() names the real operators and calls the schedule live",
          "Viking Line" in d and "live schedule" in d and "5 scheduled option" in d)

    check("mode map covers the rail family as 'train'",
          all(_MODE_MAP[m] == "train" for m in
              ("HIGHSPEED_RAIL", "REGIONAL_RAIL", "RAIL", "NIGHT_RAIL")))
    check("UA identifies the project per the Transitous policy",
          "hopandhaul" in UA and "github.com" in UA)

    # circuit breaker: two failures open it; success resets it
    _breaker["failures"] = 0
    _breaker["open_until"] = 0.0
    _breaker_record(False)
    check("one failure doesn't open the breaker", not _breaker_open())
    _breaker_record(False)
    check("two consecutive failures open the breaker", _breaker_open())
    _breaker["open_until"] = 0.0
    _breaker_record(True)
    _breaker_record(False)
    check("a success in between resets the count", not _breaker_open())
    _breaker["failures"] = 0
    _breaker["open_until"] = 0.0

    check("breaker-open short-circuits without a network call",
          (_breaker.update(open_until=time.time() + 60) or
           ground_options(0, 0, 1, 1) is None))
    _breaker["open_until"] = 0.0

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (offline checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
