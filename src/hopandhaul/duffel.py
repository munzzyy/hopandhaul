#!/usr/bin/env python3
"""
duffel.py — live flight-price fetcher for travel-scout via the Duffel Flights API.

Cole's flight data source. Creates an offer request for each candidate O->D->date, reads the
cheapest offer, normalizes it to USD, and (in CLI mode) attaches a ground leg per gateway and
hands the whole thing to trip.py — which applies Cole's $200 fly-then-train rule.

Auth is a single bearer key (no OAuth token dance): DUFFEL_API_KEY (a `duffel_test_…` or
`duffel_live_…` token). Read from env or secrets.local.json via _secrets. Stdlib urllib only.

Currency: Duffel returns each offer in the airline's filing currency. To keep the money math
honest (flight + ground summed in one currency) we convert to USD with a labelled approximate
FX table; the native amount + currency are preserved and any conversion is flagged.

Examples:
  hopandhaul-duffel --from JFK --to ASE --date 2026-08-15 --auto-gateways --vot 30
  python -m hopandhaul.duffel --from JFK --to ASE --date 2026-08-15 --gateway "DEN train 75 6.0"
  python -m hopandhaul.duffel --probe --from JFK --to LAX --date 2026-08-15   # one live call
  python -m hopandhaul.duffel --selftest                                     # offline, no network
"""
from __future__ import annotations

import argparse
import importlib.resources
import json
import os
import re
import sys
import urllib.parse

from . import _secrets, trip  # deterministic engine + report
from .integrations import net

BASE = os.environ.get("DUFFEL_BASE", "https://api.duffel.com")
DUFFEL_VERSION = os.environ.get("DUFFEL_VERSION", "v2")

# Duffel's documented free-tier limit is 120 req/60s. A client-side token bucket keeps a burst
# of concurrent gateway lookups (server.py fans out up to 6 threads per map click) from tripping
# that limit itself, on top of net.py's retry/backoff for whatever 429s still get through.
_RATE_LIMIT = net.TokenBucket(rate=120 / 60, capacity=20)

# 10-minute TTL, keyed on everything that changes the fare (widened from the old cache's
# origin/dest/date-only key, which silently served a cached economy fare to a business-class
# request once cabin became a UI toggle).
_OFFER_CACHE = net.TTLCache(ttl_seconds=600, max_size=512)

# Approximate USD value of 1 unit of each currency. Labelled ESTIMATE — the tool tells Cole to
# re-verify fares at booking anyway; this only keeps mixed-currency comparisons sane. Unknown
# currencies pass through native + flagged upstream.
FX_AS_OF = "2026-07-04"  # bump this date whenever the table below is refreshed
FX_USD = {
    "USD": 1.0, "GBP": 1.27, "EUR": 1.08, "CAD": 0.73, "AUD": 0.66, "NZD": 0.61,
    "CHF": 1.12, "SEK": 0.094, "NOK": 0.092, "DKK": 0.145, "PLN": 0.25, "CZK": 0.043,
    "HUF": 0.0027, "RON": 0.22, "MXN": 0.055, "BRL": 0.18, "JPY": 0.0064, "CNY": 0.138,
    "HKD": 0.128, "SGD": 0.74, "INR": 0.012, "ZAR": 0.054, "AED": 0.272, "SAR": 0.267,
    "TRY": 0.031, "THB": 0.028, "MYR": 0.21, "IDR": 0.000062, "PHP": 0.017, "KRW": 0.00073,
    "ILS": 0.27, "CLP": 0.0011, "COP": 0.00025, "ARS": 0.0011,
    # Europe (non-euro) + nearby
    "ISK": 0.0072, "BGN": 0.55, "RSD": 0.0092, "MKD": 0.0175, "BAM": 0.55, "ALL": 0.0107,
    "UAH": 0.024, "GEL": 0.37, "AMD": 0.0025, "AZN": 0.59, "GIP": 1.27,
    # Middle East
    "QAR": 0.275, "OMR": 2.60, "JOD": 1.41, "KWD": 3.25, "BHD": 2.65, "IQD": 0.00076,
    # Americas
    "PEN": 0.27, "UYU": 0.024, "PYG": 0.00013, "BOB": 0.145, "GTQ": 0.13, "CRC": 0.0019,
    "NIO": 0.027, "HNL": 0.040, "DOP": 0.0165, "JMD": 0.0064, "TTD": 0.148, "BBD": 0.50,
    "BSD": 1.0, "XCD": 0.37, "AWG": 0.56, "ANG": 0.56, "PAB": 1.0, "KYD": 1.20,
    # Africa
    "EGP": 0.020, "MAD": 0.10, "TND": 0.32, "DZD": 0.0075, "KES": 0.0077, "NGN": 0.00065,
    "GHS": 0.065, "TZS": 0.00037, "UGX": 0.00027, "XOF": 0.0018, "XAF": 0.0018,
    "RWF": 0.00072, "ETB": 0.008, "MUR": 0.022, "SCR": 0.068, "NAD": 0.054, "BWP": 0.073,
    "ZMW": 0.039, "MGA": 0.00021,
    # Asia-Pacific
    "TWD": 0.031, "VND": 0.000039, "LAK": 0.000046, "KHR": 0.00025, "MOP": 0.124,
    "BND": 0.74, "LKR": 0.0033, "NPR": 0.0074, "BDT": 0.0085, "PKR": 0.0036,
    "MVR": 0.065, "BTN": 0.012, "KZT": 0.0019, "UZS": 0.000078, "KGS": 0.0115,
    "MNT": 0.00029, "FJD": 0.44, "XPF": 0.0090,
}


# --------------------------------------------------------------------------- helpers
def have_keys() -> bool:
    return _secrets.has("DUFFEL_API_KEY")


def is_live_key() -> bool:
    return (_secrets.get("DUFFEL_API_KEY") or "").startswith("duffel_live")


def to_usd(amount: float, currency: str) -> tuple[float, bool]:
    """(usd_amount, converted?). converted is False when currency is USD or unknown."""
    cur = (currency or "USD").upper()
    if cur == "USD":
        return round(amount, 2), False
    rate = FX_USD.get(cur)
    if not rate:
        return round(amount, 2), False  # unknown currency: pass through native, flag upstream
    return round(amount * rate, 2), True


def iso8601_to_hours(s: str) -> float:
    """'PT5H30M' -> 5.5 ; 'PT45M' -> 0.75 ; 'P1DT2H' -> 26.0."""
    if not s:
        return 0.0
    m = re.match(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", s)
    if not m:
        return 0.0
    days, hours, mins = (int(g) if g else 0 for g in m.groups())
    return round(days * 24 + hours + mins / 60.0, 4)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_secrets.get('DUFFEL_API_KEY')}",
        "Duffel-Version": DUFFEL_VERSION,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _http_json(url: str, data=None, method="GET", timeout=30):
    _RATE_LIMIT.acquire()
    return net.fetch_json(url, data=data, headers=_headers(), method=method, timeout=timeout)


# --------------------------------------------------------------------------- duffel API
def _offer_request_body(origin: str, dest: str, date: str, return_date: str | None = None,
                        adults: int = 1, cabin: str = "economy") -> dict:
    """Pure request-body builder (unit-testable). A return_date adds the second slice —
    real round-trip fares, which are often much better than 2× one-way."""
    slices = [{"origin": origin.upper(), "destination": dest.upper(), "departure_date": date}]
    if return_date:
        slices.append({"origin": dest.upper(), "destination": origin.upper(),
                       "departure_date": return_date})
    return {"data": {
        "slices": slices,
        "passengers": [{"type": "adult"} for _ in range(max(1, adults))],
        "cabin_class": cabin,
    }}


def create_offer_request(origin: str, dest: str, date: str, adults: int = 1,
                         cabin: str = "economy", timeout: int = 30,
                         return_date: str | None = None) -> str:
    body = _offer_request_body(origin, dest, date, return_date, adults, cabin)
    data = json.dumps(body).encode("utf-8")
    out = _http_json(f"{BASE}/air/offer_requests?return_offers=false",
                     data=data, method="POST", timeout=timeout)
    return out["data"]["id"]


def get_offers(offer_request_id: str, limit: int = 10, timeout: int = 30) -> list[dict]:
    params = {"offer_request_id": offer_request_id, "sort": "total_amount", "limit": str(limit)}
    url = f"{BASE}/air/offers?" + urllib.parse.urlencode(params)
    out = _http_json(url, method="GET", timeout=timeout)
    return out.get("data") or []


def _checked_bags_included(first_slice: dict) -> int | None:
    """Fewest checked-bag allowance across passengers/segments in the outbound slice, or None
    if Duffel didn't return baggage data for this offer. A 'cheapest' fare that needs a paid
    bag add-on isn't actually cheapest — this is what lets the UI say so."""
    counts = []
    for seg in first_slice.get("segments") or []:
        for pax in seg.get("passengers") or []:
            for bag in pax.get("baggages") or []:
                if bag.get("type") == "checked":
                    counts.append(int(bag.get("quantity", 0) or 0))
    return min(counts) if counts else None


def _fare_conditions(o: dict) -> dict:
    """Refund/change eligibility Duffel already returns and the old parser discarded."""
    cond = o.get("conditions") or {}
    refund = cond.get("refund_before_departure") or {}
    change = cond.get("change_before_departure") or {}
    return {
        "refundable": bool(refund.get("allowed")),
        "changeable": bool(change.get("allowed")),
    }


def _parse_offer(o: dict) -> dict:
    slices = o.get("slices") or []
    first = slices[0] if slices else {}
    segs = first.get("segments") or []
    native = float(o["total_amount"])
    cur = (o.get("total_currency") or "USD").upper()
    usd, converted = to_usd(native, cur)
    owner = (o.get("owner") or {}).get("iata_code", "?")
    out = {
        "price": usd,                    # USD (converted if needed) — what the engine sums
        "native_price": round(native, 2),
        "currency": cur,
        "converted": converted,
        "fx_ok": cur == "USD" or cur in FX_USD,
        "hours": iso8601_to_hours(first.get("duration", "")),  # OUTBOUND journey time
        "stops": max(0, len(segs) - 1),
        "carrier": owner,
        "source": "duffel",
        "rt": len(slices) >= 2,          # price covers the return slice too
        "checked_bags_included": _checked_bags_included(first),
    }
    out.update(_fare_conditions(o))
    if out["rt"]:
        out["hours_return"] = iso8601_to_hours(slices[1].get("duration", ""))
    return out


def search_cheapest(origin: str, dest: str, date: str, adults: int = 1,
                    cabin: str = "economy", nonstop: bool = False,
                    max_offers: int = 10, timeout: int = 30,
                    return_date: str | None = None) -> dict | None:
    """Cheapest offer for O->D on date (round-trip if return_date), normalized to USD.
    RT price is the whole itinerary for ALL passengers; hours is the outbound journey.
    None if no offers / no key."""
    if not have_keys():
        return None
    # Cache key includes every param that changes the fare returned — cabin/nonstop/adults/
    # return_date — not just origin/dest/date. The old cache keyed on the latter only, so a
    # cached economy result would get silently served to a business-class request.
    cache_key = (origin.upper(), dest.upper(), date, adults, cabin, nonstop, return_date)
    cached = _OFFER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    rid = create_offer_request(origin, dest, date, adults, cabin, timeout, return_date)
    offers = get_offers(rid, max_offers, timeout)
    if not offers:
        return None
    parsed = [_parse_offer(o) for o in offers]
    if nonstop:
        ns = [p for p in parsed if p["stops"] == 0]
        if ns:
            parsed = ns
    parsed.sort(key=lambda p: p["price"])   # re-sort in USD (native sort can differ post-FX)
    best = parsed[0]
    _OFFER_CACHE.set(cache_key, best)
    return best


# --------------------------------------------------------------------------- gateway helpers
def parse_gateway_arg(s: str) -> dict:
    """'DEN train 75 6.0' -> {'hub_airport','ground_mode','ground_cost','ground_hours'}."""
    parts = s.split()
    if len(parts) < 4:
        raise ValueError(f"--gateway needs 'HUB mode cost hours': got {s!r}")
    return {"hub_airport": parts[0].upper(), "ground_mode": parts[1].lower(),
            "ground_cost": trip.num(parts[2]), "ground_hours": trip.num(parts[3])}


def auto_gateways(dest_airport: str) -> list[dict]:
    """Gateway suggestions from gateways.json for a destination airport code."""
    with importlib.resources.files("hopandhaul.data").joinpath("gateways.json").open(
            "r", encoding="utf-8") as f:
        data = json.load(f)
    found = []
    for _region, entries in data.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if str(e.get("dest_airport", "")).upper() == dest_airport.upper():
                for g in e["gateways"]:
                    found.append({"hub_airport": g["hub_airport"], "ground_mode": g["ground_mode"],
                                  "ground_cost": float(g["ground_cost_usd"]),
                                  "ground_hours": float(g["ground_time_h"])})
    return found


# --------------------------------------------------------------------------- orchestration (CLI)
def build_and_evaluate(origin, dest, date, gateways, adults, cabin, nonstop,
                       vot, transfer_buffer, threshold, return_date=None):
    """Duffel flight fares are itinerary totals for ALL passengers (and both directions when
    return_date is set). Ground legs come in as one-way per-person estimates, so they are
    scaled ×adults (per-person modes only) and ×2 on a round-trip to keep the sums honest."""
    options, warnings = [], []
    rt_mult = 2 if return_date else 1
    direct = search_cheapest(origin, dest, date, adults, cabin, nonstop,
                             return_date=return_date)
    if direct:
        options.append(trip.parse_option(
            f"Fly direct to {dest.upper()} | fly {direct['price']} {direct['hours']}"))
        if not direct["fx_ok"]:
            warnings.append(f"Direct fare in {direct['currency']} — no FX rate, treated as USD.")
        elif direct["converted"]:
            warnings.append(f"Direct fare converted {direct['currency']}->USD (approx).")
    else:
        warnings.append(f"No Duffel offers {origin}->{dest} on {date}.")

    for g in gateways:
        fly = search_cheapest(origin, g["hub_airport"], date, adults, cabin, nonstop,
                              return_date=return_date)
        if not fly:
            warnings.append(f"No offers {origin}->{g['hub_airport']}; skipped that gateway.")
            continue
        ground_cost = trip.scale_leg_cost(g["ground_mode"], g["ground_cost"], adults) * rt_mult
        name = f"{g['hub_airport']} + {g['ground_mode']}"
        options.append(trip.parse_option(
            f"{name} | fly {fly['price']} {fly['hours']} ; "
            f"{g['ground_mode']} {ground_cost} {g['ground_hours']}"))

    if not options:
        return None, warnings
    res = trip.evaluate(options, threshold=threshold, vot=vot,
                        transfer_buffer=transfer_buffer, travelers=adults)
    return res, warnings


def print_setup_help():
    print("Duffel key not set — live flight pricing is unavailable.\n")
    print("Get one free:")
    print("  1) https://app.duffel.com  -> Developers -> Access tokens -> create a TEST token")
    print("  2) store it (either works):")
    print("       setx DUFFEL_API_KEY \"duffel_test_...\"        (new shell), or")
    print("       add DUFFEL_API_KEY to secrets.local.json in this folder")
    print("\nMeanwhile the agent prices flights via web search and still runs trip.py.")


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    trip._force_utf8()
    p = argparse.ArgumentParser(description="Live Duffel flight fetch -> trip.py ($200 rule).")
    p.add_argument("--from", dest="origin", help="origin airport IATA (e.g. JFK)")
    p.add_argument("--to", dest="dest", help="final destination airport IATA (e.g. ASE)")
    p.add_argument("--date", help="departure date YYYY-MM-DD")
    p.add_argument("--return-date", dest="return_date", default=None,
                   help="return date YYYY-MM-DD -> price REAL round-trip fares "
                        "(often much better than 2x one-way); ground legs are doubled")
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
    p.add_argument("--probe", action="store_true", help="one live O->D->date lookup, print summary")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()

    if not have_keys():
        print_setup_help()
        return 2

    if args.probe:
        if not (args.origin and args.dest and args.date):
            p.error("--probe needs --from, --to and --date")
        try:
            best = search_cheapest(args.origin, args.dest, args.date, args.adults,
                                   args.cabin, args.nonstop, return_date=args.return_date)
        except net.FetchError as e:
            print(f"Duffel request failed: {e}")
            return 3
        if not best:
            print("No offers returned.")
            return 1
        rt = f" ROUND-TRIP (ret {args.return_date})" if best.get("rt") else ""
        print(f"cheapest {args.origin.upper()}->{args.dest.upper()} {args.date}{rt}: "
              f"${best['price']} ({best['native_price']} {best['currency']}"
              f"{' ~USD' if best['converted'] else ''}), {best['hours']}h out, "
              f"{best['stops']} stop(s), {best['carrier']}")
        return 0

    if not (args.origin and args.dest and args.date):
        p.error("--from, --to and --date are required for a live search")

    gateways = [parse_gateway_arg(g) for g in args.gateway]
    if args.auto_gateways:
        gateways += auto_gateways(args.dest)
    if not gateways:
        print("No gateways given. Add --gateway '...' or --auto-gateways to test the split logic.")

    try:
        res, warnings = build_and_evaluate(
            args.origin, args.dest, args.date, gateways, args.adults, args.cabin,
            args.nonstop, args.vot, args.transfer_buffer, args.threshold,
            return_date=args.return_date)
    except net.FetchError as e:
        print(f"Duffel request failed: {e}")
        return 3

    for w in warnings:
        print(f"WARN  {w}")
    if res is None:
        print("No priceable options — nothing to recommend.")
        return 1
    rt_note = " · ROUND-TRIP fares (times shown = outbound)" if args.return_date else ""
    print(f"(source: Duffel {'LIVE' if is_live_key() else 'TEST'} · flights live · "
          f"ground = estimate, verify{rt_note})\n")
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
    check("ISO8601 P1DT2H -> 26.0", iso8601_to_hours("P1DT2H") == 26.0)

    usd, conv = to_usd(100.0, "USD")
    check("USD passthrough (no conversion flag)", usd == 100.0 and conv is False)
    gbp, conv2 = to_usd(100.0, "GBP")
    check("GBP converts to USD (>100, flagged)", gbp > 100 and conv2 is True)
    unk, conv3 = to_usd(100.0, "ZZZ")
    check("unknown currency passes through, unconverted", unk == 100.0 and conv3 is False)

    o = _parse_offer({"total_amount": "241.50", "total_currency": "USD",
                      "owner": {"iata_code": "AA"},
                      "slices": [{"duration": "PT5H30M", "segments": [{}, {}]}]})
    check("offer parses to price/hours/stops/carrier",
          o["price"] == 241.5 and o["hours"] == 5.5 and o["stops"] == 1 and o["carrier"] == "AA")
    check("one-way offer is not flagged rt", o["rt"] is False)

    b1 = _offer_request_body("jfk", "ase", "2026-08-15")
    check("one-way body has 1 slice, 1 adult",
          len(b1["data"]["slices"]) == 1 and len(b1["data"]["passengers"]) == 1)
    b2 = _offer_request_body("JFK", "ASE", "2026-08-15", return_date="2026-08-22", adults=3)
    check("RT body has 2 slices (reversed) and 3 adults",
          len(b2["data"]["slices"]) == 2
          and b2["data"]["slices"][1]["origin"] == "ASE"
          and b2["data"]["slices"][1]["destination"] == "JFK"
          and b2["data"]["slices"][1]["departure_date"] == "2026-08-22"
          and len(b2["data"]["passengers"]) == 3)
    ort = _parse_offer({"total_amount": "480.00", "total_currency": "USD",
                        "owner": {"iata_code": "UA"},
                        "slices": [{"duration": "PT5H", "segments": [{}]},
                                   {"duration": "PT4H30M", "segments": [{}]}]})
    check("RT offer flags rt + return hours",
          ort["rt"] is True and ort["hours"] == 5.0 and ort["hours_return"] == 4.5)
    check("ground scaling: bus ×3 adults, rental ×1",
          trip.scale_leg_cost("bus", 20, 3) == 60 and trip.scale_leg_cost("rental", 80, 3) == 80)

    g = parse_gateway_arg("DEN train 75 6.0")
    check("gateway arg parses", g["hub_airport"] == "DEN" and g["ground_cost"] == 75.0)
    ag = auto_gateways("ASE")
    check("auto-gateways finds Aspen (ASE) incl. DEN",
          any(x["hub_airport"] == "DEN" for x in ag) and len(ag) >= 2)

    # offline end-to-end: prove the engine wiring works with mock live numbers.
    opts = [trip.parse_option("Fly direct to ASE | fly 620 5.5"),
            trip.parse_option("DEN + train | fly 210 3.0 ; train 75 6.0")]
    r = trip.evaluate(opts, threshold=200, transfer_buffer=1.0)
    check("wired engine recommends the qualifying split", r["recommended"] == "DEN + train")
    check("have_keys() is a bool", isinstance(have_keys(), bool))

    # baggage + fare-conditions surfaced from an offer Duffel already returns them on
    offer_with_extras = {
        "total_amount": "300.00", "total_currency": "USD", "owner": {"iata_code": "DL"},
        "conditions": {"refund_before_departure": {"allowed": False},
                       "change_before_departure": {"allowed": True}},
        "slices": [{"duration": "PT3H", "segments": [
            {"passengers": [{"baggages": [{"type": "checked", "quantity": 1}]}]}]}],
    }
    parsed_extra = _parse_offer(offer_with_extras)
    check("checked-bag allowance is surfaced, not discarded",
          parsed_extra["checked_bags_included"] == 1)
    check("fare refundable/changeable flags are surfaced",
          parsed_extra["refundable"] is False and parsed_extra["changeable"] is True)
    no_baggage_data = _parse_offer({"total_amount": "300.00", "total_currency": "USD",
                                    "owner": {"iata_code": "DL"},
                                    "slices": [{"duration": "PT3H", "segments": [{}]}]})
    check("missing baggage data -> None, not a false 0",
          no_baggage_data["checked_bags_included"] is None)

    check("FX table has a grep-able as-of date", bool(FX_AS_OF) and FX_AS_OF[:4].isdigit())

    # offer cache: widened key includes cabin/nonstop/return_date, not just origin/dest/date
    key_a = ("JFK", "ASE", "2026-08-15", 1, "economy", False, None)
    key_b = ("JFK", "ASE", "2026-08-15", 1, "business", False, None)
    _OFFER_CACHE.set(key_a, {"price": 200})
    _OFFER_CACHE.set(key_b, {"price": 900})
    check("offer cache keeps economy and business fares separate",
          _OFFER_CACHE.get(key_a)["price"] == 200 and _OFFER_CACHE.get(key_b)["price"] == 900)
    check("net.TTLCache is the backing cache type", isinstance(_OFFER_CACHE, net.TTLCache))

    check("rate limiter is sized off Duffel's 120 req/60s free-tier limit",
          isinstance(_RATE_LIMIT, net.TokenBucket) and _RATE_LIMIT.rate == 120 / 60)

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (offline checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
