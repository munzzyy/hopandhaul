#!/usr/bin/env python3
"""
duffel.py - live flight-price fetcher for travel-scout via the Duffel Flights API.

Cole's flight data source. Creates an offer request for each candidate O->D->date, reads the
cheapest offer, normalizes it to USD, and (in CLI mode) attaches a ground leg per gateway and
hands the whole thing to trip.py - which applies Cole's $200 fly-then-train rule.

Auth is a single bearer key (no OAuth token dance): DUFFEL_API_KEY (a `duffel_test_...` or
`duffel_live_...` token). Read from env or secrets.local.json via _secrets. Stdlib urllib only.
No key configured (or a route Duffel has no offer for) isn't a hard failure: the CLI falls back
to geo.py's distance ESTIMATE per flight leg, same as server.py's map UI - see
_price_flight_cli(). Every priced option also carries a leg-by-leg itinerary (itinerary.py):
real airport names, an example (or, once live, real) clock schedule, per-leg price provenance,
and a verify link - printed as plain text by format_itineraries().

Currency: Duffel returns each offer in the airline's filing currency. To keep the money math
honest (flight + ground summed in one currency) we convert to USD - with today's real ECB
rate via frankfurter.dev (keyless) when the network allows, else a labelled approximate
bundled table; the native amount + currency are preserved and any conversion is flagged.

Examples:
  hopandhaul duffel --from JFK --to ASE --date 2026-08-15 --auto-gateways --vot 30
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

from datetime import datetime

from . import _secrets, geo, itinerary, trip  # deterministic engine + report
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

# Approximate USD value of 1 unit of each currency. Labelled ESTIMATE - the tool tells Cole to
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


# Live daily rates from frankfurter.dev (keyless, no quota, ECB-sourced) upgrade the static
# table above whenever the network allows: one call, cached for the session, ~30 majors. The
# static table stays as the offline fallback and for the long tail of currencies the ECB set
# doesn't carry. to_usd() reports which source priced a conversion so provenance can say so.
_FX_LIVE = {"rates": None, "tried": False}
_FRANKFURTER = "https://api.frankfurter.dev/v1/latest?base=USD"


def _live_rates() -> dict | None:
    """{currency: usd_per_unit} from today's ECB fix, or None. One attempt per process."""
    if not _FX_LIVE["tried"]:
        _FX_LIVE["tried"] = True
        try:
            out = net.fetch_json(_FRANKFURTER, headers={"Accept": "application/json"},
                                 timeout=6, max_retries=0)
            rates = out.get("rates") or {}
            # frankfurter answers units-per-USD; invert to USD-per-unit like FX_USD
            _FX_LIVE["rates"] = {c: 1.0 / r for c, r in rates.items() if r}
            _FX_LIVE["date"] = out.get("date")
        except (net.FetchError, OSError, ValueError, ZeroDivisionError):
            _FX_LIVE["rates"] = None
    return _FX_LIVE["rates"]


def fx_source() -> str:
    if _FX_LIVE.get("rates"):
        return f"ECB daily rates via frankfurter.dev ({_FX_LIVE.get('date', 'today')})"
    return f"approximate table (as of {FX_AS_OF})"


# --------------------------------------------------------------------------- helpers
def have_keys() -> bool:
    return _secrets.has("DUFFEL_API_KEY")


def is_live_key() -> bool:
    return (_secrets.get("DUFFEL_API_KEY") or "").startswith("duffel_live")


def to_usd(amount: float, currency: str) -> tuple[float, bool]:
    """(usd_amount, converted?). converted is False when currency is USD or unknown.
    Prefers today's real ECB rate (frankfurter.dev, keyless) and falls back to the bundled
    approximate table offline."""
    cur = (currency or "USD").upper()
    if cur == "USD":
        return round(amount, 2), False
    live = _live_rates()
    rate = (live or {}).get(cur) or FX_USD.get(cur)
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
    """Pure request-body builder (unit-testable). A return_date adds the second slice - 
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
    bag add-on isn't actually cheapest - this is what lets the UI say so."""
    counts = []
    for seg in first_slice.get("segments") or []:
        for pax in seg.get("passengers") or []:
            for bag in pax.get("baggages") or []:
                if bag.get("type") == "checked":
                    counts.append(int(bag.get("quantity", 0) or 0))
    return min(counts) if counts else None


def _parse_dt(s: str | None) -> datetime | None:
    """Duffel's departing_at/arriving_at are local-to-the-airport, naive ISO8601 timestamps
    ('2026-08-15T08:12:00') - no timezone math needed, Duffel already resolved that server-side.
    Tolerant of a trailing 'Z' some providers add out of habit even on a naive local timestamp."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:-1] if s.endswith("Z") else s)
    except ValueError:
        return None


def _parse_segments(first_slice: dict) -> list[dict]:
    """Real per-hop schedule data Duffel already returns and the old parser discarded - this is
    what lets itinerary.py show a live leg's actual departure/arrival clock and carrier instead
    of a synthetic example. from_iata/to_iata only (not full airport records): resolving those
    to name/city is geo.py's job, not this module's - keeps this a pure Duffel-shape parser."""
    out = []
    for seg in first_slice.get("segments") or []:
        dep = _parse_dt(seg.get("departing_at"))
        arr = _parse_dt(seg.get("arriving_at"))
        if not dep or not arr:
            continue   # a segment with no usable schedule data can't drive a real timeline row
        carrier_obj = seg.get("marketing_carrier") or seg.get("operating_carrier") or {}
        out.append({
            "from_iata": (seg.get("origin") or {}).get("iata_code"),
            "to_iata": (seg.get("destination") or {}).get("iata_code"),
            "depart_at": dep,
            "arrive_at": arr,
            "carrier": carrier_obj.get("name") or carrier_obj.get("iata_code"),
            "flight_number": seg.get("marketing_carrier_flight_number")
                or seg.get("operating_carrier_flight_number"),
        })
    return out


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
        "price": usd,                    # USD (converted if needed) - what the engine sums
        "native_price": round(native, 2),
        "currency": cur,
        "converted": converted,
        "fx_ok": cur == "USD" or converted,   # a rate existed (live ECB or bundled table)
        "hours": iso8601_to_hours(first.get("duration", "")),  # OUTBOUND journey time
        "stops": max(0, len(segs) - 1),
        "carrier": owner,
        "source": "duffel",
        "rt": len(slices) >= 2,          # price covers the return slice too
        "checked_bags_included": _checked_bags_included(first),
        "segments": _parse_segments(first),   # real per-hop schedule, for itinerary.py
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
    # Cache key includes every param that changes the fare returned - cabin/nonstop/adults/
    # return_date - not just origin/dest/date. The old cache keyed on the latter only, so a
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
def _airport_or_stub(code: str) -> dict:
    """Full airport record from our own DB when we have one; a bare-bones stand-in when we
    don't (Duffel's IATA universe is bigger than the curated 4175-row airports.json) - so an
    itinerary leg always has SOMETHING to show instead of crashing on an obscure code."""
    return geo.by_iata(code) or {"iata": code.upper(), "name": code.upper(), "city": None}


def _price_flight_cli(origin_a, dest_a, date, adults, cabin, nonstop, return_date):
    """Live if a Duffel key is configured and returns an offer, else a distance ESTIMATE - 
    mirrors server.py's _price_flight() so `hopandhaul duffel` (and the console script) work
    with NO key at all, the same estimate-first-live-if-possible contract the map UI already
    has, instead of the CLI refusing to run without one."""
    if have_keys():
        live = search_cheapest(origin_a["iata"], dest_a["iata"], date, adults, cabin, nonstop,
                               return_date=return_date)
        if live:
            return live
    est = geo.estimate_flight(origin_a, dest_a, date=date)
    price = est["price"] * max(1, adults)
    rt = False
    if return_date:
        est_back = geo.estimate_flight(dest_a, origin_a, date=return_date)
        price += est_back["price"] * max(1, adults)
        rt = True
    return {"price": round(price, 2), "hours": est["hours"], "source": "estimate", "rt": rt,
            "estimate_detail": est, "fx_ok": True, "converted": False, "currency": "USD",
            "native_price": None, "carrier": None, "segments": []}


def _flight_leg_spec_cli(origin_a, dest_a, f, date):
    """itinerary.py leg spec for a duffel.py CLI flight leg - live (real Duffel offer, real
    segment schedule) when _price_flight_cli() found one, else a distance ESTIMATE. Mirrors
    server.py's _flight_leg_spec(). `f["segments"]` is duffel.py's own raw per-hop schedule
    (see _parse_segments) - resolved to full airport records here so the itinerary shows the
    real departure/arrival clock and carrier instead of a synthetic example."""
    is_live = f.get("source") != "estimate"
    segments = None
    if is_live and f.get("segments"):
        segments = [{
            "from": _airport_or_stub(s.get("from_iata") or origin_a["iata"]),
            "to": _airport_or_stub(s.get("to_iata") or dest_a["iata"]),
            "depart_at": s["depart_at"], "arrive_at": s["arrive_at"],
            "carrier": s.get("carrier"), "flight_number": s.get("flight_number"),
        } for s in f["segments"]]
    price_basis = (itinerary.flight_provenance_live(f) if is_live
                   else itinerary.flight_provenance_estimate(f.get("estimate_detail"), date))
    return {
        "mode": "fly", "cost": f["price"], "hours": f["hours"], "from": origin_a, "to": dest_a,
        "price_basis": price_basis,
        "verify_url": itinerary.verify_link("fly", origin_a, dest_a, date),
        "is_live": is_live, "segments": segments,
    }


def _ground_leg_spec_cli(gw_iata_a, dest_a, mode, cost, hours):
    """itinerary.py leg spec for a duffel.py CLI ground leg - always an estimate; gateways
    passed on this CLI (--gateway / --auto-gateways) carry no 'source'/'notes' the way
    geo.py's curated/auto-discovered gateway dicts do, so provenance is generic distance-based."""
    road_km = None
    if gw_iata_a.get("lat") is not None and dest_a.get("lat") is not None:
        road_km = geo.haversine_km(gw_iata_a["lat"], gw_iata_a["lng"],
                                   dest_a["lat"], dest_a["lng"]) * geo.ROAD_WINDING
    return {
        "mode": mode, "cost": cost, "hours": hours, "from": gw_iata_a, "to": dest_a,
        "price_basis": itinerary.ground_provenance({}, road_km),
        "verify_url": itinerary.verify_link(mode, gw_iata_a, dest_a),
        "is_live": False, "segments": None,
    }


def build_and_evaluate(origin, dest, date, gateways, adults, cabin, nonstop,
                       vot, transfer_buffer, threshold, return_date=None):
    """Duffel flight fares are itinerary totals for ALL passengers (and both directions when
    return_date is set). Ground legs come in as one-way per-person estimates, so they are
    scaled ×adults (per-person modes only) and ×2 on a round-trip to keep the sums honest.

    Every flight leg is priced live when a Duffel key is configured and returns an offer, else
    a distance ESTIMATE (see _price_flight_cli) - this runs with no key at all, same as the map
    UI. Each returned option also carries an 'itinerary' (see itinerary.py): real airport names,
    an example (or, once live, real) clock schedule, per-leg price provenance, and a one-click
    verify link."""
    options, warnings, leg_specs_by_name = [], [], {}
    rt_mult = 2 if return_date else 1
    origin_a, dest_a = _airport_or_stub(origin), _airport_or_stub(dest)
    direct = _price_flight_cli(origin_a, dest_a, date, adults, cabin, nonstop, return_date)
    direct_name = f"Fly direct to {dest.upper()}"
    options.append(trip.parse_option(
        f"{direct_name} | fly {direct['price']} {direct['hours']}"))
    leg_specs_by_name[direct_name] = [_flight_leg_spec_cli(origin_a, dest_a, direct, date)]
    if direct["source"] == "estimate":
        warnings.append(f"No live Duffel offer {origin}->{dest}; priced with a distance ESTIMATE.")
    elif not direct["fx_ok"]:
        warnings.append(f"Direct fare in {direct['currency']} — no FX rate, treated as USD.")
    elif direct["converted"]:
        warnings.append(f"Direct fare converted {direct['currency']}->USD (approx).")

    for g in gateways:
        gw_a = _airport_or_stub(g["hub_airport"])
        fly = _price_flight_cli(origin_a, gw_a, date, adults, cabin, nonstop, return_date)
        if fly["source"] == "estimate":
            warnings.append(f"No live Duffel offer {origin}->{g['hub_airport']}; "
                            "priced with a distance ESTIMATE.")
        ground_cost = trip.scale_leg_cost(g["ground_mode"], g["ground_cost"], adults) * rt_mult
        name = f"{g['hub_airport']} + {g['ground_mode']}"
        options.append(trip.parse_option(
            f"{name} | fly {fly['price']} {fly['hours']} ; "
            f"{g['ground_mode']} {ground_cost} {g['ground_hours']}"))
        leg_specs_by_name[name] = [
            _flight_leg_spec_cli(origin_a, gw_a, fly, date),
            _ground_leg_spec_cli(gw_a, dest_a, g["ground_mode"], ground_cost, g["ground_hours"]),
        ]

    res = trip.evaluate(options, threshold=threshold, vot=vot,
                        transfer_buffer=transfer_buffer, travelers=adults)
    for o in res["options"]:
        o["itinerary"] = itinerary.build_timeline(
            leg_specs_by_name.get(o["name"], []), date=date, transfer_buffer_h=transfer_buffer)
    return res, warnings


def print_setup_help():
    print("Duffel key not set — live flight pricing is unavailable.\n")
    print("Get one free:")
    print("  1) https://app.duffel.com  -> Developers -> Access tokens -> create a TEST token")
    print("  2) store it (either works):")
    print("       setx DUFFEL_API_KEY \"duffel_test_...\"        (new shell), or")
    print("       add DUFFEL_API_KEY to secrets.local.json in this folder")
    print("\nMeanwhile the agent prices flights via web search and still runs trip.py.")


def _fmt_money(x: float) -> str:
    """Mirrors trip.py's private _fmt_money - duplicated here rather than reached into across
    modules, same one-function-worth-of-footprint every other module in this repo keeps local."""
    return f"${x:,.0f}" if abs(x - round(x)) < 0.005 else f"${x:,.2f}"


def _airport_label(a: dict) -> str:
    """'ASE (Aspen)' not 'ASE (Aspen, Aspen)' - resort/small airports in airports.json often
    have name == city, and repeating it reads like a data bug."""
    city = a.get("city")
    if city and city != a.get("name"):
        return f"{a['iata']} ({a['name']}, {city})"
    return f"{a['iata']} ({a['name']})"


def _format_itinerary_block(option: dict) -> str:
    """Human-readable leg-by-leg schedule for one priced option - real airports, an example
    (or, when live, a real) clock schedule, per-leg price provenance, and a verify link. This is
    what turns 'DEN + train  $XXX' into something a person can actually check."""
    itin = option.get("itinerary") or {}
    legs = itin.get("legs") or []
    if not legs:
        return ""
    lines = [f"    {option['name']}:"]
    if itin.get("example_day"):
        lines.append("      (example schedule, not a real booking — see 'verify' links below)")
    for i, leg in enumerate(legs, 1):
        frm, to = leg["from"], leg["to"]
        tag = "LIVE" if leg["is_live"] else "est."
        carrier = f" — {leg['carrier']}" + (f" {leg['flight_number']}" if leg.get("flight_number") else "") \
            if leg.get("carrier") else ""
        lines.append(
            f"      {i}. {leg['mode'].upper():<6} {_airport_label(frm)} -> {_airport_label(to)}")
        lines.append(
            f"         {leg['depart_day']} {leg['depart_clock']} -> {leg['arrive_day']} {leg['arrive_clock']}"
            f"  ({leg['duration_h']:g}h)  {tag}{carrier}")
        if leg.get("checkin_by"):
            lines.append(f"         be at the airport by {leg['checkin_by']['day']} {leg['checkin_by']['clock']}")
        lines.append(f"         {_fmt_money(leg['cost'])} · {leg['price_basis']}")
        lines.append(f"         verify: {leg['verify_url']}")
    return "\n".join(lines)


def format_itineraries(res: dict) -> str:
    """Itinerary blocks for every option in an evaluate()d result, in the same order as the
    options table trip.format_report() already printed."""
    blocks = [_format_itinerary_block(o) for o in res["options"]]
    blocks = [b for b in blocks if b]
    if not blocks:
        return ""
    return "ITINERARIES (real airports, example or live clock times, verify before booking):\n\n" \
        + "\n\n".join(blocks)


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

    if args.probe:
        if not have_keys():
            print_setup_help()
            return 2
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
        p.error("--from, --to and --date are required")

    if not have_keys():
        print("(No DUFFEL_API_KEY configured — flight legs priced with distance ESTIMATES, "
              "same as the map UI with no key. See README.md for how to add one.)\n")

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
    flights_src = (f"Duffel {'LIVE' if is_live_key() else 'TEST'}" if have_keys() else "ESTIMATE")
    print(f"(source: flights {flights_src} · ground = estimate, verify{rt_note})\n")
    if args.json:
        print(json.dumps({k: v for k, v in res.items() if not k.startswith("_")}, indent=2))
    else:
        print(trip.format_report(res, args.origin, args.dest))
        print()
        itin_block = format_itineraries(res)
        if itin_block:
            print(itin_block)
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

    # keep the selftest genuinely offline: pin the one-shot live-FX lookup to "already tried,
    # nothing came back" so to_usd() exercises the bundled fallback table deterministically.
    _FX_LIVE["tried"], _FX_LIVE["rates"] = True, None
    usd, conv = to_usd(100.0, "USD")
    check("USD passthrough (no conversion flag)", usd == 100.0 and conv is False)
    gbp, conv2 = to_usd(100.0, "GBP")
    check("GBP converts to USD (>100, flagged)", gbp > 100 and conv2 is True)
    unk, conv3 = to_usd(100.0, "ZZZ")
    check("unknown currency passes through, unconverted", unk == 100.0 and conv3 is False)
    check("offline FX source names the bundled table + its as-of date",
          "approximate table" in fx_source() and FX_AS_OF in fx_source())
    _FX_LIVE["rates"] = {"GBP": 1.30}
    check("a live ECB rate takes precedence over the bundled table when present",
          to_usd(100.0, "GBP") == (130.0, True) and "frankfurter" in fx_source())
    _FX_LIVE["rates"] = None

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

    # build_and_evaluate() end-to-end, offline (mocked search_cheapest): every option must
    # come back with a leg-by-leg itinerary - real airports, live schedule for the flight leg,
    # provenance + a verify link for both legs.
    import unittest.mock as _mock

    def _fake_search_cheapest(origin, dest, date, adults=1, cabin="economy", nonstop=False,
                              return_date=None):
        if dest.upper() == "EGE":
            return None    # simulates "no Duffel offer for this gateway"
        return {"price": 210.0, "hours": 3.0, "stops": 0, "carrier": "United Airlines",
                "currency": "USD", "converted": False, "fx_ok": True, "source": "duffel",
                "rt": False, "checked_bags_included": 1, "refundable": False, "changeable": True,
                "native_price": 210.0,
                "segments": [{"from_iata": origin.upper(), "to_iata": dest.upper(),
                             "depart_at": datetime(2026, 8, 15, 9, 30),
                             "arrive_at": datetime(2026, 8, 15, 12, 30),
                             "carrier": "United Airlines", "flight_number": "UA55"}]}

    # patch this module's OWN globals (not a dotted string path) - same reasoning as the
    # equivalent guard in server.py's selftest: run via `python -m hopandhaul.duffel --selftest`
    # this module is loaded as __main__, so a string-path patch risks hitting a second,
    # separately-imported copy instead of the one build_and_evaluate() actually calls into.
    _this_module = sys.modules[__name__]
    with _mock.patch.object(_this_module, "search_cheapest", side_effect=_fake_search_cheapest), \
         _mock.patch.object(_this_module, "have_keys", return_value=True):
        res_cli, warn_cli = build_and_evaluate(
            "JFK", "ASE", "2026-08-15",
            [{"hub_airport": "DEN", "ground_mode": "bus", "ground_cost": 75.0, "ground_hours": 4.0},
             {"hub_airport": "EGE", "ground_mode": "rental", "ground_cost": 90.0, "ground_hours": 2.0}],
            adults=1, cabin="economy", nonstop=False, vot=None, transfer_buffer=1.0, threshold=200)
    check("build_and_evaluate returns a result", res_cli is not None)
    check("a gateway with no Duffel offer degrades to a distance ESTIMATE, not a hard skip",
          any("EGE" in w and "ESTIMATE" in w for w in warn_cli)
          and any(o["name"].startswith("EGE") for o in res_cli["options"]))
    ege_opt = next(o for o in res_cli["options"] if o["name"].startswith("EGE"))
    check("the ESTIMATE-fallback gateway's flight leg is correctly flagged not live",
          ege_opt["itinerary"]["legs"][0]["is_live"] is False)
    den_opt = next(o for o in res_cli["options"] if o["name"].startswith("DEN"))
    check("CLI-built option carries a 2-leg itinerary", len(den_opt["itinerary"]["legs"]) == 2)
    cli_fly_leg = den_opt["itinerary"]["legs"][0]
    check("CLI flight leg resolves real airport identity via geo.by_iata, not a bare code",
          cli_fly_leg["from"]["iata"] == "JFK" and cli_fly_leg["from"]["name"])
    check("CLI flight leg uses the real Duffel segment clock/carrier, not a synthetic schedule",
          cli_fly_leg["is_live"] is True and cli_fly_leg["depart_clock"] == "09:30"
          and cli_fly_leg["carrier"] == "United Airlines" and cli_fly_leg["flight_number"] == "UA55")
    check("CLI flight leg's price provenance says 'live'", "live" in cli_fly_leg["price_basis"].lower())
    cli_ground_leg = den_opt["itinerary"]["legs"][1]
    check("CLI ground leg carries a Rome2Rio verify link", cli_ground_leg["verify_url"].startswith(
        "https://www.rome2rio.com/map/"))
    itin_text = format_itineraries(res_cli)
    check("format_itineraries renders every priced option with a leg count, not just the winner",
          itin_text.count(" -> ") >= 2 * len(res_cli["options"]))
    check("format_itineraries includes the verify links as plain URLs (CLI text output)",
          "verify: https://" in itin_text)

    # with NO key configured at all, build_and_evaluate() must still work end to end (distance
    # ESTIMATES throughout, same as the map UI with no key) rather than requiring one - this is
    # the whole point of _price_flight_cli's fallback.
    with _mock.patch.object(_this_module, "have_keys", return_value=False):
        res_nokey, warn_nokey = build_and_evaluate(
            "JFK", "ASE", "2026-08-15",
            [{"hub_airport": "DEN", "ground_mode": "bus", "ground_cost": 75.0, "ground_hours": 4.0}],
            adults=1, cabin="economy", nonstop=False, vot=None, transfer_buffer=1.0, threshold=200)
    check("build_and_evaluate works with zero keys configured (falls back to ESTIMATE)",
          res_nokey is not None and len(res_nokey["options"]) == 2)
    check("every leg is flagged not-live when no key is configured",
          all(leg["is_live"] is False for o in res_nokey["options"] for leg in o["itinerary"]["legs"]))
    check("a no-key run still warns per flight leg that it's an ESTIMATE",
          all("ESTIMATE" in w for w in warn_nokey) and len(warn_nokey) == 2)

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

    # real segment schedule data - what lets itinerary.py show a live leg's actual times/carrier
    # instead of a synthetic example, and what the old parser silently discarded.
    offer_with_segments = {
        "total_amount": "241.50", "total_currency": "USD",
        "owner": {"iata_code": "AA"},
        "slices": [{"duration": "PT5H30M", "segments": [
            {"origin": {"iata_code": "JFK"}, "destination": {"iata_code": "DEN"},
             "departing_at": "2026-08-15T08:12:00", "arriving_at": "2026-08-15T10:05:00",
             "marketing_carrier": {"iata_code": "UA", "name": "United Airlines"},
             "marketing_carrier_flight_number": "1234"},
            {"origin": {"iata_code": "DEN"}, "destination": {"iata_code": "ASE"},
             "departing_at": "2026-08-15T11:20:00Z", "arriving_at": "2026-08-15T12:05:00Z",
             "marketing_carrier": {"iata_code": "UA", "name": "United Airlines"},
             "marketing_carrier_flight_number": "5678"},
        ]}],
    }
    parsed_segs = _parse_offer(offer_with_segments)["segments"]
    check("segments are parsed for a connecting itinerary (2 hops)", len(parsed_segs) == 2)
    check("segment carries real IATA endpoints",
          parsed_segs[0]["from_iata"] == "JFK" and parsed_segs[0]["to_iata"] == "DEN")
    check("segment carries real carrier name + flight number, not invented",
          parsed_segs[0]["carrier"] == "United Airlines" and parsed_segs[0]["flight_number"] == "1234")
    check("segment departure/arrival parse to real datetimes",
          parsed_segs[0]["depart_at"] == datetime(2026, 8, 15, 8, 12)
          and parsed_segs[0]["arrive_at"] == datetime(2026, 8, 15, 10, 5))
    check("a trailing 'Z' some providers add to a naive local timestamp is tolerated",
          parsed_segs[1]["depart_at"] == datetime(2026, 8, 15, 11, 20))
    check("an offer with no segment data at all yields an empty list, not a crash",
          _parse_offer({"total_amount": "100", "total_currency": "USD", "owner": {},
                       "slices": [{"duration": "PT1H", "segments": []}]})["segments"] == [])
    check("a segment missing usable departure/arrival timestamps is skipped, not fabricated",
          _parse_offer({"total_amount": "100", "total_currency": "USD", "owner": {},
                       "slices": [{"duration": "PT1H", "segments": [{"origin": {"iata_code": "JFK"}}]}]}
                      )["segments"] == [])

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
