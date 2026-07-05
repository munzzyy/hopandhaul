#!/usr/bin/env python3
"""
providers.py — live flight-price fetcher for travel-scout (Amadeus Self-Service, FREE tier).

The best *free* real-fare source. Fetches the cheapest flight for each candidate O→D→date via
the Amadeus Flight Offers Search API, attaches a ground leg (train/bus/drive) for each gateway,
then hands the whole thing to trip.py — which applies Cole's $200 fly-then-train rule.

Ground pricing: Rome2Rio (the one true multimodal API) is paid, so it is NOT used here. Ground
legs come from gateways.json estimates or values you pass in; the agent verifies them via free
web search. Flights are live + free; the ground number is the one to re-check.

Setup (one time, free):
  1. Sign up at https://developers.amadeus.com  → create an app → copy the API Key + Secret.
  2. Set env vars (PowerShell, persistent):
       setx AMADEUS_CLIENT_ID     "your_api_key"
       setx AMADEUS_CLIENT_SECRET "your_api_secret"
     (open a NEW shell after setx). Optional: AMADEUS_ENV=prod  (default: test).
  3. Test env is free but uses cached/limited data & routes. Production has a free monthly quota
     of live calls — use --prod (or AMADEUS_ENV=prod) once your app is moved to production.

No hardcoded secrets. Stdlib only (urllib) — no pip installs. If keys are absent it prints setup
help and exits cleanly; the agent then falls back to web-search pricing.

Examples:
  python -m hopandhaul.providers --from JFK --to ASE --date 2026-08-15 \
      --gateway "DEN train 75 6.0" --gateway "EGE drive 60 1.3" --vot 30
  # or pull gateway suggestions straight from gateways.json by destination airport:
  python -m hopandhaul.providers --from JFK --to ASE --date 2026-08-15 --auto-gateways
  python -m hopandhaul.providers --selftest   # offline checks (no network / no keys needed)
"""
from __future__ import annotations

import argparse
import importlib.resources
import json
import os
import re
import sys
import urllib.parse

from . import _secrets, trip  # reuse the deterministic engine + report
from .integrations import net

TEST_BASE = "https://test.api.amadeus.com"
PROD_BASE = "https://api.amadeus.com"

# Amadeus test-env data is already cached/limited server-side, but this keeps repeat gateway
# fan-out (same route re-queried across nearby hub candidates) from re-hitting the API at all.
_OFFER_CACHE = net.TTLCache(ttl_seconds=600, max_size=512)


# --------------------------------------------------------------------------- helpers
def have_keys() -> bool:
    return _secrets.has("AMADEUS_CLIENT_ID") and _secrets.has("AMADEUS_CLIENT_SECRET")


def iso8601_to_hours(s: str) -> float:
    """'PT5H30M' -> 5.5 ; 'PT45M' -> 0.75 ; 'P1DT2H' -> 26.0."""
    if not s:
        return 0.0
    m = re.match(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", s)
    if not m:
        return 0.0
    days, hours, mins = (int(g) if g else 0 for g in m.groups())
    return round(days * 24 + hours + mins / 60.0, 4)


def _http_json(url: str, data=None, headers=None, timeout=25):
    return net.fetch_json(url, data=data, headers=headers or {},
                          method="POST" if data else "GET", timeout=timeout)


# --------------------------------------------------------------------------- amadeus
def get_token(base: str) -> str:
    cid = _secrets.get("AMADEUS_CLIENT_ID")
    secret = _secrets.get("AMADEUS_CLIENT_SECRET")
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": secret,
    }).encode("utf-8")
    hdr = {"Content-Type": "application/x-www-form-urlencoded"}
    out = _http_json(f"{base}/v1/security/oauth2/token", data=body, headers=hdr)
    return out["access_token"]


def search_cheapest(base: str, token: str, origin: str, dest: str, date: str,
                    adults: int = 1, currency: str = "USD",
                    nonstop: bool = False, max_results: int = 8) -> dict | None:
    """Return {'price','hours','stops','carrier'} for the cheapest offer, or None if no offers."""
    cache_key = (base, origin.upper(), dest.upper(), date, adults, currency, nonstop)
    cached = _OFFER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    params = {
        "originLocationCode": origin.upper(),
        "destinationLocationCode": dest.upper(),
        "departureDate": date,
        "adults": str(adults),
        "currencyCode": currency,
        "max": str(max_results),
    }
    if nonstop:
        params["nonStop"] = "true"
    url = f"{base}/v2/shopping/flight-offers?" + urllib.parse.urlencode(params)
    out = _http_json(url, headers={"Authorization": f"Bearer {token}"})
    offers = out.get("data") or []
    if not offers:
        return None
    best = min(offers, key=lambda o: float(o["price"]["grandTotal"]))
    itin = best["itineraries"][0]
    segs = itin.get("segments", [])
    result = {
        "price": round(float(best["price"]["grandTotal"]), 2),
        "hours": iso8601_to_hours(itin.get("duration", "")),
        "stops": max(0, len(segs) - 1),
        "carrier": segs[0]["carrierCode"] if segs else "?",
    }
    _OFFER_CACHE.set(cache_key, result)
    return result


# --------------------------------------------------------------------------- gateway lookup
def parse_gateway_arg(s: str) -> dict:
    """'DEN train 75 6.0' -> {'hub_airport','ground_mode','ground_cost','ground_hours'}."""
    parts = s.split()
    if len(parts) < 4:
        raise ValueError(f"--gateway needs 'HUB mode cost hours': got {s!r}")
    return {
        "hub_airport": parts[0].upper(),
        "ground_mode": parts[1].lower(),
        "ground_cost": trip.num(parts[2]),
        "ground_hours": trip.num(parts[3]),
    }


def auto_gateways(dest_airport: str) -> list[dict]:
    """Pull gateway suggestions from gateways.json for a destination airport code."""
    with importlib.resources.files("hopandhaul.data").joinpath("gateways.json").open(
            "r", encoding="utf-8") as f:
        data = json.load(f)
    found = []
    for region, entries in data.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if str(e.get("dest_airport", "")).upper() == dest_airport.upper():
                for g in e["gateways"]:
                    found.append({
                        "hub_airport": g["hub_airport"],
                        "ground_mode": g["ground_mode"],
                        "ground_cost": float(g["ground_cost_usd"]),
                        "ground_hours": float(g["ground_time_h"]),
                        "notes": g.get("notes", ""),
                    })
    return found


# --------------------------------------------------------------------------- orchestration
def build_and_evaluate(origin, dest, date, gateways, adults, currency, nonstop,
                       prod, vot, transfer_buffer, threshold):
    base = PROD_BASE if prod or os.environ.get("AMADEUS_ENV", "").lower() == "prod" else TEST_BASE
    token = get_token(base)

    options = []
    warnings = []

    direct = search_cheapest(base, token, origin, dest, date, adults, currency, nonstop)
    if direct:
        options.append(trip.parse_option(
            f"Fly direct to {dest.upper()} | fly {direct['price']} {direct['hours']}"))
    else:
        warnings.append(f"No direct flight offers found {origin}->{dest} on {date}.")

    for g in gateways:
        fly = search_cheapest(base, token, origin, g["hub_airport"], date, adults, currency, nonstop)
        if not fly:
            warnings.append(f"No flight offers {origin}->{g['hub_airport']}; skipped that gateway.")
            continue
        name = f"{g['hub_airport']} + {g['ground_mode']}"
        options.append(trip.parse_option(
            f"{name} | fly {fly['price']} {fly['hours']} ; "
            f"{g['ground_mode']} {g['ground_cost']} {g['ground_hours']}"))

    if not options:
        return None, warnings, base
    res = trip.evaluate(options, threshold=threshold, vot=vot, transfer_buffer=transfer_buffer)
    return res, warnings, base


def print_setup_help():
    print("Amadeus keys not set — live flight pricing is unavailable.\n")
    print("Get them free:")
    print("  1) https://developers.amadeus.com  -> create an app -> copy API Key + Secret")
    print("  2) setx AMADEUS_CLIENT_ID     \"your_api_key\"")
    print("     setx AMADEUS_CLIENT_SECRET \"your_api_secret\"   (then open a NEW shell)")
    print("  3) optional: AMADEUS_ENV=prod for live production quota (default = free test env)\n")
    print("Meanwhile the agent prices flights via free web search and still runs trip.py.")


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    trip._force_utf8()
    p = argparse.ArgumentParser(description="Live Amadeus flight fetch -> trip.py ($200 rule).")
    p.add_argument("--from", dest="origin", help="origin airport IATA (e.g. JFK)")
    p.add_argument("--to", dest="dest", help="final destination airport IATA (e.g. ASE)")
    p.add_argument("--date", help="departure date YYYY-MM-DD")
    p.add_argument("--gateway", action="append", default=[],
                   help="'HUB_IATA mode ground_cost ground_hours' (repeatable)")
    p.add_argument("--auto-gateways", action="store_true",
                   help="pull gateway suggestions from gateways.json for --to")
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--currency", default="USD")
    p.add_argument("--nonstop", action="store_true", help="direct flights only")
    p.add_argument("--prod", action="store_true", help="use Amadeus production endpoint")
    p.add_argument("--vot", type=float, default=None, help="value of time $/hr")
    p.add_argument("--transfer-buffer", type=float, default=1.0,
                   help="hours added per connection (default 1.0; splits are separate tickets)")
    p.add_argument("--threshold", type=float, default=trip.DEFAULT_THRESHOLD)
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()

    if not have_keys():
        print_setup_help()
        return 2

    if not (args.origin and args.dest and args.date):
        p.error("--from, --to and --date are required for a live search")

    gateways = [parse_gateway_arg(g) for g in args.gateway]
    if args.auto_gateways:
        gateways += auto_gateways(args.dest)
    if not gateways:
        print("No gateways given. Add --gateway '...' or --auto-gateways to test the split logic.")

    try:
        res, warnings, base = build_and_evaluate(
            args.origin, args.dest, args.date, gateways, args.adults, args.currency,
            args.nonstop, args.prod, args.vot, args.transfer_buffer, args.threshold)
    except net.FetchError as e:
        print(f"Amadeus request failed: {e}")
        return 3

    for w in warnings:
        print(f"⚠️  {w}")
    if res is None:
        print("No priceable options — nothing to recommend.")
        return 1
    print(f"(source: Amadeus {'PROD' if base == PROD_BASE else 'TEST'} · flights live · ground = estimate, verify)\n")
    if args.json:
        print(json.dumps({k: v for k, v in res.items() if not k.startswith("_")}, indent=2))
    else:
        print(trip.format_report(res, args.origin, args.dest))
    return 0


# --------------------------------------------------------------------------- self-test (offline)
def selftest():
    trip._force_utf8()
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    check("ISO8601 PT5H30M -> 5.5", iso8601_to_hours("PT5H30M") == 5.5)
    check("ISO8601 PT45M -> 0.75", iso8601_to_hours("PT45M") == 0.75)
    check("ISO8601 PT2H -> 2.0", iso8601_to_hours("PT2H") == 2.0)
    check("ISO8601 P1DT2H -> 26.0", iso8601_to_hours("P1DT2H") == 26.0)

    g = parse_gateway_arg("DEN train 75 6.0")
    check("gateway arg parses", g["hub_airport"] == "DEN" and g["ground_cost"] == 75.0
          and g["ground_hours"] == 6.0 and g["ground_mode"] == "train")

    ag = auto_gateways("ASE")
    check("auto-gateways finds Aspen (ASE) hubs incl. DEN",
          any(x["hub_airport"] == "DEN" for x in ag) and len(ag) >= 2)

    # offline end-to-end: mimic what live fares would produce, prove the engine wiring works.
    opts = [
        trip.parse_option("Fly direct to ASE | fly 620 5.5"),
        trip.parse_option("DEN + train | fly 210 3.0 ; train 75 6.0"),
    ]
    r = trip.evaluate(opts, threshold=200, transfer_buffer=1.0)
    check("wired engine recommends the qualifying split", r["recommended"] == "DEN + train")

    check("have_keys() is a bool", isinstance(have_keys(), bool))

    key_a = (TEST_BASE, "JFK", "ASE", "2026-08-15", 1, "USD", False)
    key_b = (PROD_BASE, "JFK", "ASE", "2026-08-15", 1, "USD", False)
    _OFFER_CACHE.set(key_a, {"price": 200})
    _OFFER_CACHE.set(key_b, {"price": 250})
    check("offer cache keeps test/prod base results separate",
          _OFFER_CACHE.get(key_a)["price"] == 200 and _OFFER_CACHE.get(key_b)["price"] == 250)
    check("net.TTLCache is the backing cache type", isinstance(_OFFER_CACHE, net.TTLCache))

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (offline checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
