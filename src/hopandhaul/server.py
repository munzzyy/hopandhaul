#!/usr/bin/env python3
"""
server.py - tiny localhost server for the travel-scout click-the-map UI.

Serves ui/index.html and a JSON API. On a map click the browser calls /api/plan with the
clicked lat/lng; the server finds the nearest airport, discovers cheaper-hub + ground gateways
(geo.py), prices every leg (live Duffel via flights.py if keys are set, else transparent
distance ESTIMATES), and runs the deterministic engine (trip.py) with Cole's $200 rule. Each
option also gets a rough co2e_kg estimate (emissions.py) and the response points out whichever
option is lowest-carbon ("greenest") - informational only, never used to pick "recommended".

Security: binds 127.0.0.1 only; rejects requests whose Host isn't localhost (DNS-rebinding
guard); serves packaged ui/ assets, realpath-checked (no path traversal); no writes; no third-party code;
no outbound network call ever builds its target host from client input (see SECURITY.md).

Run:  python -m hopandhaul.server [--port 8770]
Test: python -m hopandhaul.server --selftest    (offline end-to-end, no network/keys)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import importlib.resources
import json
import os
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import __version__, emissions, geo, itinerary, trip
from .integrations import net
try:
    from . import flights     # live flight pricing (Duffel, optional key)
except Exception:  # pragma: no cover
    flights = None
try:
    from . import places      # geocoding: Photon keyless, Geoapify when keyed
except Exception:  # pragma: no cover
    places = None
try:
    from . import weather      # destination conditions (Open-Meteo, keyless)
except Exception:  # pragma: no cover
    weather = None
try:
    from . import transit      # real ground schedules (Transitous, keyless)
except Exception:  # pragma: no cover
    transit = None

# UI root resolved via importlib.resources so this works from a repo checkout AND a
# real (non-editable) pip install alike - never trust a path built from request input.
_UI_ROOT = str(importlib.resources.files("hopandhaul") / "ui")
INDEX = os.path.join(_UI_ROOT, "index.html")
DEFAULT_PORT = int(os.environ.get("TRAVEL_PORT", "8770"))
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Static UI assets are served from the packaged ui/ dir only, keyed by extension, with a
# realpath check that refuses anything resolving outside ui/ (no path traversal).
_UI_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
}
_UI_REAL = os.path.realpath(_UI_ROOT)


def _resolve_ui_asset(url_path):
    """Map a request path to a file inside ui/, or None. Only known asset extensions,
    and the resolved realpath must stay within ui/ - this is the traversal guard."""
    rel = url_path.lstrip("/")
    ctype = _UI_TYPES.get(os.path.splitext(rel)[1].lower())
    if not rel or not ctype:
        return None
    real = os.path.realpath(os.path.join(_UI_ROOT, rel))
    if real != _UI_REAL and not real.startswith(_UI_REAL + os.sep):
        return None
    if not os.path.isfile(real):
        return None
    return real, ctype

# ------------------------------------------------------------------- error contract
# Every endpoint returns {"ok": true, ...} or {"ok": false, "error": <human-safe>, "code": <str>}.
# Exception internals (message, traceback, type name) are logged server-side only, never in
# the response body - an attacker probing this API shouldn't learn anything about the host,
# file paths, or dependency versions from an error string.
def _err(code: str, message: str) -> dict:
    return {"ok": False, "error": message, "code": code}


def _log_exc(where: str, e: BaseException) -> None:
    print(f"[{where}] {type(e).__name__}: {e}", file=sys.stderr)


# ------------------------------------------------------------------- input validation
# Shared, strict per-param validators for anything arriving from an HTTP query string.
# Query params are always str -> str lists (urllib.parse_qs); every value below is
# parsed, type-checked, and range/length-checked before it reaches plan()/geo.py/trip.py.
class ValidationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


MAX_QUERY_TEXT_LEN = 200          # geocode query string
MAX_IATA_LEN = 4                  # real IATA codes are 3 chars; allow a little slack, no more
MAX_TRAVELERS = 9
MIN_THRESHOLD = 0.0
MAX_THRESHOLD = 100_000.0
MIN_GROUND_H = 0.0
MAX_GROUND_H = 48.0
MIN_BUFFER_H = 0.0
MAX_BUFFER_H = 24.0
MAX_VOT = 10_000.0


def _require(q: dict, name: str) -> str:
    vals = q.get(name)
    if not vals or vals[0] == "":
        raise ValidationError(f"{name} is required")
    return vals[0]


def _optional(q: dict, name: str) -> str | None:
    vals = q.get(name)
    if not vals or vals[0] == "":
        return None
    return vals[0]


def _v_lat(raw: str) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValidationError("lat must be a number")
    if not (-90.0 <= v <= 90.0):
        raise ValidationError("lat must be between -90 and 90")
    return v


def _v_lng(raw: str) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValidationError("lng must be a number")
    if not (-180.0 <= v <= 180.0):
        raise ValidationError("lng must be between -180 and 180")
    return v


def _v_iata(raw: str) -> str:
    v = raw.strip().upper()
    # ASCII A-Z only - str.isalpha() also accepts Unicode letters (e.g. accented or
    # non-Latin scripts), which are never valid IATA codes and would pass through
    # unchecked to geo.by_iata()/provider calls.
    if not v or len(v) > MAX_IATA_LEN or not all("A" <= c <= "Z" for c in v):
        raise ValidationError("origin must be a short airport code (letters only)")
    return v


def _v_date(raw: str, field: str) -> str:
    v = raw.strip()
    if len(v) != 10 or v[4] != "-" or v[7] != "-":
        raise ValidationError(f"{field} must be YYYY-MM-DD")
    year, month, day = v[:4], v[5:7], v[8:10]
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        raise ValidationError(f"{field} must be YYYY-MM-DD")
    try:
        datetime.date(int(year), int(month), int(day))
    except ValueError:
        raise ValidationError(f"{field} is not a real calendar date")
    return v


def _v_float_range(raw: str, field: str, lo: float, hi: float) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a number")
    if not (lo <= v <= hi):
        raise ValidationError(f"{field} must be between {lo} and {hi}")
    return v


def _v_int_range(raw: str, field: str, lo: int, hi: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a whole number")
    if not (lo <= v <= hi):
        raise ValidationError(f"{field} must be between {lo} and {hi}")
    return v


def _v_bool_flag(raw: str | None) -> bool:
    return (raw or "0") in ("1", "true", "yes")


def _v_query_text(raw: str, field: str = "q") -> str:
    v = raw.strip()
    if not v:
        raise ValidationError(f"{field} is required")
    if len(v) > MAX_QUERY_TEXT_LEN:
        raise ValidationError(f"{field} is too long (max {MAX_QUERY_TEXT_LEN} chars)")
    return v


def parse_plan_params(q: dict) -> dict:
    """Validate every /api/plan query param. Raises ValidationError with a safe message."""
    lat = _v_lat(_require(q, "lat"))
    lng = _v_lng(_require(q, "lng"))
    out = {"dest_lat": lat, "dest_lng": lng}

    origin = _optional(q, "origin")
    out["origin_iata"] = _v_iata(origin) if origin else "JFK"

    date = _optional(q, "date")
    out["date"] = _v_date(date, "date") if date else None

    ret = _optional(q, "ret")
    out["ret"] = _v_date(ret, "ret") if ret else None
    if out["ret"] and out["date"] and out["ret"] < out["date"]:
        raise ValidationError("return date must be on or after the depart date")

    vot = _optional(q, "vot")
    out["vot"] = _v_float_range(vot, "vot", 0.0, MAX_VOT) if vot else None

    threshold = _optional(q, "threshold")
    out["threshold"] = (_v_float_range(threshold, "threshold", MIN_THRESHOLD, MAX_THRESHOLD)
                        if threshold else trip.DEFAULT_THRESHOLD)

    max_ground_h = _optional(q, "maxGroundH")
    out["max_ground_h"] = (_v_float_range(max_ground_h, "maxGroundH", MIN_GROUND_H, MAX_GROUND_H)
                           if max_ground_h else 6.0)

    out["roundtrip"] = _v_bool_flag(_optional(q, "round"))

    travelers = _optional(q, "travelers")
    out["travelers"] = _v_int_range(travelers, "travelers", 1, MAX_TRAVELERS) if travelers else 1

    buffer_ = _optional(q, "buffer")
    out["transfer_buffer"] = (_v_float_range(buffer_, "buffer", MIN_BUFFER_H, MAX_BUFFER_H)
                              if buffer_ else 1.0)
    return out


def parse_geocode_params(q: dict) -> dict:
    text = _v_query_text(_require(q, "q"))
    limit = _optional(q, "limit")
    n = _v_int_range(limit, "limit", 1, 10) if limit else 6
    return {"text": text, "limit": n}


def parse_nearest_params(q: dict) -> dict:
    lat = _v_lat(_require(q, "lat"))
    lng = _v_lng(_require(q, "lng"))
    return {"lat": lat, "lng": lng}


# --------------------------------------------------------------------------- rate limiting
class TokenBucket:
    """Thread-safe token bucket. Caps how fast the server opens outbound provider calls,
    independent of how many browser clients are clicking - protects the Duffel account's
    real rate limit (120 req/60s) from a burst of concurrent map clicks."""

    def __init__(self, rate_per_s: float, capacity: float):
        self.rate = rate_per_s
        self.capacity = capacity
        self.tokens = capacity
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def try_take(self, n: float = 1.0) -> bool:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last
            self.last = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False


# Sized under Duffel's published 120 req/60s: 2 req/s sustained, small burst allowance.
_DUFFEL_BUCKET = TokenBucket(rate_per_s=2.0, capacity=6.0)
PLAN_TIME_BUDGET_S = 20.0   # wall-clock ceiling for the whole concurrent gateway fan-out


# --------------------------------------------------------------------------- planning
_OFFER_CACHE: dict = {}
_OFFER_CACHE_LOCK = threading.Lock()
OFFER_CACHE_TTL_S = 600     # repeat clicks re-use live offers for 10 min (fares don't move that fast)


def _cached_live_search(session, origin_iata, dest_iata, date, adults, return_date):
    key = (origin_iata, dest_iata, date, return_date, adults)
    now = time.time()
    with _OFFER_CACHE_LOCK:
        hit = _OFFER_CACHE.get(key)
        if hit and now - hit[0] < OFFER_CACHE_TTL_S:
            return hit[1]
    if not _DUFFEL_BUCKET.try_take():
        return None    # rate-limited: caller falls back to the distance estimate
    live = flights.search_cheapest(session, origin_iata, dest_iata, date,
                                   adults=adults, return_date=return_date)
    if live:
        with _OFFER_CACHE_LOCK:
            _OFFER_CACHE[key] = (now, live)
            if len(_OFFER_CACHE) > 512:      # bound memory
                oldest = sorted(_OFFER_CACHE.items(), key=lambda kv: kv[1][0])[:256]
                for k, _ in oldest:
                    _OFFER_CACHE.pop(k, None)
    return live


def _price_flight(origin, dest, date, ret, travelers, session, ctx, deadline):
    """Return {'price','hours','source','rt', ...} - TOTAL for all travelers; a real round-trip
    fare when the provider priced the return (rt True), else one-way. Live if possible, else a
    date-aware distance estimate. Also carries whatever provenance itinerary.py needs to explain
    the number: 'estimate_detail' (geo.estimate_flight's own dict) on the estimate branch,
    'segments'/'carrier'/'native_price'/'currency'/'converted' on the live branch - never
    discarded here just because _flight_cost()/the option string only needs the price."""
    if session and date and time.monotonic() < deadline:
        try:
            live = _cached_live_search(session, origin["iata"], dest["iata"], date,
                                       travelers, ret)
            if live:
                ctx["live_used"] = True
                ctx["provider"] = live["source"]
                if live.get("converted"):
                    ctx["fx_used"] = True
                elif live.get("currency") not in (None, "USD"):
                    ctx["fx_unknown"] = live["currency"]
                return {"price": live["price"], "hours": live["hours"],
                        "source": live["source"], "rt": live.get("rt", False),
                        "segments": live.get("segments", []), "carrier": live.get("carrier"),
                        "native_price": live.get("native_price"), "currency": live.get("currency"),
                        "converted": live.get("converted", False)}
        # network/HTTP-shaped failures only - a real bug in the normalization code should
        # surface as a crash, not silently and permanently masquerade as "provider is down".
        # net.FetchError is what fetch_json() raises for every wrapped provider failure
        # (401/429/5xx/timeout/bad-json) - without it here, a live-but-failing key broke the
        # WHOLE /api/plan instead of degrading this leg to the distance estimate.
        except (net.FetchError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                ConnectionError, ValueError, KeyError):
            ctx["live_error"] = True
    est = geo.estimate_flight(origin, dest, date=date)
    price = est["price"] * max(1, travelers)
    rt = False
    if ret:                                  # estimate the return with its own date multiplier
        est_back = geo.estimate_flight(dest, origin, date=ret)
        price += est_back["price"] * max(1, travelers)
        rt = True
    ctx["est_used"] = True
    return {"price": round(price, 2), "hours": est["hours"], "source": "estimate", "rt": rt,
            "estimate_detail": est}


def _resolve_segment_airport(iata, fallback):
    """A live segment's endpoint by IATA, from our own airport DB (never trust a provider's own
    name/city formatting when we already have a curated one) - falls back to the leg's known
    origin/dest record on the rare code our DB doesn't carry, so a timeline row never ends up
    with a blank name instead of just slightly-wrong provenance."""
    return geo.by_iata(iata) or fallback


def _flight_leg_spec(origin, dest, f, cost, date):
    """itinerary.py leg spec for a flight leg, built from _price_flight()'s return dict - 
    `cost` is the already-computed group/round-trip total (_flight_cost()'s output), passed in
    rather than re-read from `f["price"]` so the itinerary never disagrees with the option's own
    printed cost."""
    is_live = f.get("source") not in (None, "estimate")
    segments = None
    if is_live and f.get("segments"):
        segments = [{
            "from": _resolve_segment_airport(s.get("from_iata"), origin),
            "to": _resolve_segment_airport(s.get("to_iata"), dest),
            "depart_at": s["depart_at"], "arrive_at": s["arrive_at"],
            "carrier": s.get("carrier"), "flight_number": s.get("flight_number"),
        } for s in f["segments"]]
        price_basis = itinerary.flight_provenance_live(f)
    else:
        price_basis = itinerary.flight_provenance_estimate(f.get("estimate_detail"), date)
    return {
        "mode": "fly", "cost": round(cost, 2), "hours": f["hours"],
        "from": origin, "to": dest, "price_basis": price_basis,
        "verify_url": itinerary.verify_link("fly", origin, dest, date),
        "is_live": is_live, "segments": segments,
    }


def _ground_leg_spec(g, dest, cost, road_km):
    """itinerary.py leg spec for a ground leg - always an estimate (see README: no free, open
    multimodal fares API worth calling here)."""
    return {
        "mode": g["ground_mode"], "cost": round(cost, 2), "hours": g["ground_hours"],
        "from": g, "to": dest,
        "price_basis": itinerary.ground_provenance(g, road_km),
        "verify_url": itinerary.verify_link(g["ground_mode"], g, dest),
        "is_live": False, "segments": None,
    }


def plan(dest_lat, dest_lng, origin_iata="JFK", date=None, vot=None, threshold=200.0,
         max_ground_h=6.0, roundtrip=False, fetch_weather=True, travelers=1,
         ret=None, transfer_buffer=1.0, allow_live=True, allow_transit=True):
    origin = geo.by_iata(origin_iata)
    if not origin:
        return {"ok": False, "error": f"unknown origin airport '{origin_iata}'", "code": "unknown_origin"}
    # prefer_hub=True so a click near a city snaps to the field with real airline service
    # instead of the literal closest point on the map (a seaplane base, a business-aviation
    # field) - matches what /api/nearest already does.
    dest = geo.nearest_airport(dest_lat, dest_lng, prefer_hub=True)
    if not dest:
        return {"ok": False, "error": "no airport found near that point", "code": "no_airport_near_point"}
    # clicking on (or right next to) your own origin airport has no flight to plan - without
    # this guard the engine happily prices a same-airport "direct flight" off the NA short-hop
    # floor and recommends it with full confidence.
    if dest["iata"] == origin["iata"]:
        return {"ok": False,
                "error": "that point resolves to your origin airport — no flight needed",
                "code": "origin_is_destination"}

    travelers = max(1, min(9, int(travelers)))
    if ret:
        roundtrip = True
    rt_mult = 2 if roundtrip else 1          # ground legs ride both ways on a round-trip

    gws = geo.discover_gateways(dest, origin=origin, max_ground_h=max_ground_h)

    # REAL ground schedules (Transitous, keyless): look up each transit-able gateway leg's
    # actual timetable to the clicked point, concurrently, under a short budget. A hit
    # replaces the leg's formula duration with the real door-to-door time and carries the
    # real operators into provenance. Fares on those legs remain estimates - GTFS has none.
    if allow_transit and transit and gws:
        lookups = [g for g in gws if g["ground_mode"] in ("train", "bus", "ferry")]
        if lookups:
            # 2 workers max - Transitous is a shared community instance and asks callers to
            # keep request volume low. The wait ceiling sits ABOVE the per-call timeout (8s
            # in transit.ground_options), so a slow-but-successful lookup is never discarded
            # by its own coordinator.
            ex_t = concurrent.futures.ThreadPoolExecutor(max_workers=min(2, len(lookups)))
            futs = {ex_t.submit(transit.ground_options, g["lat"], g["lng"],
                                dest_lat, dest_lng, date, g["ground_mode"]): g
                    for g in lookups}
            done_t, _ = concurrent.futures.wait(list(futs), timeout=9.0)
            for f in done_t:
                g = futs[f]
                try:
                    tr = f.result()
                except Exception:
                    tr = None
                if tr:
                    tr = dict(tr)
                    tr["line"] = transit.describe(tr)
                    g["transit"] = tr
                    g["ground_hours"] = tr["duration_h"]   # a real timetable beats a formula
            ex_t.shutdown(wait=False, cancel_futures=True)

    ctx = {"live_used": False, "est_used": False, "live_error": False}
    session = None
    if allow_live and flights and flights.have_keys() and date:
        try:
            session = flights.open_session()
        except Exception:
            session = None
            ctx["live_error"] = True

    # Price every flight leg (direct + each gateway hub). When live, run them concurrently - 
    # each live offer-request is a slow, independent round-trip, so a click stays responsive.
    # A shared deadline bounds the whole fan-out: one slow provider degrades to estimates
    # instead of holding every thread for its own full per-call timeout.
    flight_targets = [dest] + list(gws)
    deadline = time.monotonic() + PLAN_TIME_BUDGET_S

    def _price(target):
        local = {}
        return _price_flight(origin, target, date, ret, travelers, session, local, deadline), local

    if session and date and len(flight_targets) > 1:
        # Not a `with` block on purpose: ThreadPoolExecutor.__exit__ calls shutdown(wait=True),
        # which blocks until every submitted worker returns - including ones we've already
        # given up on below. That defeated PLAN_TIME_BUDGET_S entirely: a single hung provider
        # call (e.g. Duffel's own 30s timeout x net.py's retries) held the whole /api/plan
        # response for however long that worker took, not the budget. Instead we shut down
        # with wait=False, cancel_futures=True: any thread still in flight keeps running in
        # the background (its result is discarded, though the offer cache still benefits if
        # it finishes) while this request returns as soon as the deadline is up.
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(flight_targets)))
        futures = [ex.submit(_price, t) for t in flight_targets]
        priced = []
        remaining = max(0.0, deadline - time.monotonic())
        done, not_done = concurrent.futures.wait(futures, timeout=remaining)
        fmap = dict(zip(futures, flight_targets))
        for f in futures:
            if f in done:
                priced.append(f.result())
            else:
                f.cancel()
                target = fmap[f]
                local = {"live_error": True}
                priced.append((_price_flight(origin, target, None, None, travelers,
                                             None, local, deadline), local))
        ex.shutdown(wait=False, cancel_futures=True)
    else:
        priced = [_price(t) for t in flight_targets]
    for _pr, local in priced:              # merge per-worker flags back into the shared ctx
        for k, v in local.items():
            if v:
                ctx[k] = v

    options, geo_by_name, emissions_legs_by_name, leg_specs_by_name, notes = [], {}, {}, {}, []

    def _flight_cost(f):
        """Flight leg cost: already all-travelers; ×2 only when a RT wasn't really priced."""
        if roundtrip and not f.get("rt"):
            return f["price"] * 2
        return f["price"]

    # direct
    df = priced[0][0]
    direct_name = f"Fly direct to {dest['iata']}"
    direct_cost = _flight_cost(df)
    options.append(trip.parse_option(f"{direct_name} | fly {direct_cost} {df['hours']}"))
    geo_by_name[direct_name] = [{"type": "flight", "from": _pt(origin), "to": _pt(dest)}]
    leg_specs_by_name[direct_name] = [_flight_leg_spec(origin, dest, df, direct_cost, date)]
    # emissions distance is always the great-circle flight distance, regardless of whether the
    # fare itself came from a live quote or an estimate - CO2e only cares about km flown, not $.
    direct_km = geo.haversine_km(origin["lat"], origin["lng"], dest["lat"], dest["lng"]) * rt_mult
    emissions_legs_by_name[direct_name] = [{"mode": "fly", "distance_km": direct_km}]

    # splits (fly to a cheaper hub, then ground it) - ground legs are one-way per-person
    # estimates: scale per-person modes ×travelers (vehicles stay flat) and ×2 on a round-trip.
    for g, (gf, _local) in zip(gws, priced[1:]):
        g["fly"] = gf
        ground_cost = trip.scale_leg_cost(g["ground_mode"], g["ground_cost"], travelers) * rt_mult
        fly_cost = _flight_cost(gf)
        name = f"{g['iata']} + {g['ground_mode']}"
        options.append(trip.parse_option(
            f"{name} | fly {fly_cost} {gf['hours']} ; "
            f"{g['ground_mode']} {ground_cost} {g['ground_hours']}"))
        geo_by_name[name] = [
            {"type": "flight", "from": _pt(origin), "to": _pt(g)},
            {"type": "ground", "mode": g["ground_mode"], "from": _pt(g), "to": _pt(dest)},
        ]
        fly_km = geo.haversine_km(origin["lat"], origin["lng"], g["lat"], g["lng"]) * rt_mult
        # ground distance: same road-winding factor geo.py's own estimator uses, so a curated
        # gateway (which only ships a ground_time_h/ground_cost_usd, no distance) gets an
        # emissions figure consistent with an auto-discovered one built from estimate_ground.
        # A real-corridor ferry leg uses the actual port-to-port crossing distance instead - 
        # boats sail the strait, they don't follow a winding road.
        if g.get("ferry"):
            ground_km = g["ferry"]["crossing_km"] * rt_mult
        else:
            ground_km = geo.haversine_km(g["lat"], g["lng"], dest["lat"], dest["lng"]) * geo.ROAD_WINDING * rt_mult
        emissions_legs_by_name[name] = [
            {"mode": "fly", "distance_km": fly_km},
            {"mode": g["ground_mode"], "road_km": ground_km},
        ]
        leg_specs_by_name[name] = [
            _flight_leg_spec(origin, g, gf, fly_cost, date),
            _ground_leg_spec(g, dest, ground_cost, ground_km / max(rt_mult, 1)),
        ]

    res = trip.evaluate(options, threshold=threshold, vot=vot,
                        transfer_buffer=transfer_buffer, travelers=travelers)

    # attach map geometry, a rough CO2e estimate, and a leg-by-leg itinerary to each option,
    # then strip private keys. co2e_kg is ESTIMATED from leg distances (see emissions.py) - 
    # never treated as a booking fact, and never used to pick "recommended"; it's shown
    # alongside cost/time so the person looking at the numbers can weigh it themselves. The
    # itinerary is what turns a bare dollar figure into something a user can actually check - 
    # real airports, an example clock schedule, per-leg price provenance, a verify link.
    clean = {k: v for k, v in res.items() if not k.startswith("_")}
    for o in clean["options"]:
        o["geo"] = geo_by_name.get(o["name"], [])
        o["co2e_kg"] = emissions.co2e_for_option(
            emissions_legs_by_name.get(o["name"], []), travelers=travelers)
        o["itinerary"] = itinerary.build_timeline(
            leg_specs_by_name.get(o["name"], []), date=date, transfer_buffer_h=transfer_buffer)
    greenest = min(clean["options"], key=lambda o: o["co2e_kg"])["name"] if clean["options"] else None
    clean["greenest"] = greenest

    provider = ctx.get("provider", "live")
    if ctx["live_used"] and ctx["est_used"]:
        source = "mixed"
    elif ctx["live_used"]:
        source = f"{provider}-live"          # e.g. "duffel-live"
    else:
        source = "estimate"
    if source == "estimate":
        note = ("Fares are distance-based ESTIMATES"
                + (" (date-adjusted for booking window/season)" if date else "")
                + " — add a date for live fares. Verify before booking.")
        notes.append(note)
    if ctx.get("live_error"):
        notes.append("Some live flight lookups failed and fell back to estimates.")
    if ctx.get("fx_used"):
        notes.append("Some fares were converted to USD at an approximate rate — verify at booking.")
    if ctx.get("fx_unknown"):
        notes.append(f"A fare priced in {ctx['fx_unknown']} had no USD rate and is shown as-is.")
    if travelers > 1:
        notes.append(f"Costs are GROUP TOTALS for {travelers} travelers — per-person fares "
                     f"×{travelers}; drive/rental legs are per vehicle.")
    if roundtrip:
        if ret and ctx["live_used"]:
            notes.append(f"Round-trip priced as REAL return itineraries (back {ret}); "
                         "times shown are the outbound leg.")
        elif ret:
            notes.append(f"Round-trip: outbound + return ({ret}) estimated separately; "
                         "times are the outbound leg.")
        else:
            notes.append("Round-trip: fares shown are ~2× one-way; add a return date for real "
                         "RT pricing. Times are for the outbound leg.")
    if any(g.get("ferry") for g in gws):
        notes.append("Ferry legs are REAL corridors (bundled research, operators + typical "
                     "fares + sailings/day as of the data's date) — schedules vary by day and "
                     "season, so check the operator before relying on a connection.")
    if any(g.get("transit") for g in gws):
        notes.append("Ground legs marked 'live schedule' use real timetables via Transitous "
                     "(transitous.org — community GTFS/OSM data): real operators, departures "
                     "and door-to-door times. Fares on those legs are still estimates.")
    if dest.get("dist_km", 0) > 120:
        notes.append(f"Nearest airport {dest['iata']} is ~{int(dest['dist_km'])} km from the "
                     f"clicked point — the last mile to your exact spot isn't included.")
    notes.append("co2e_kg per option is a rough ESTIMATE from flight/ground distance, not a "
                 "certified footprint — see docs/api.md for the factor basis. The lowest-carbon "
                 "option is flagged as 'greenest' but never auto-recommended over the cheapest.")

    # destination weather - best-effort, never blocks a plan (weather is at the clicked point)
    wx = None
    if fetch_weather and weather and weather.have_keys():
        try:
            wx = weather.for_point(dest_lat, dest_lng, date)
        except Exception:
            wx = None

    return {
        "ok": True,
        "pricing_source": source,
        "date": date,
        "return_date": ret,
        "roundtrip": roundtrip,
        "travelers": travelers,
        "threshold": threshold,
        "vot": vot,
        "origin": _pt(origin, full=True),
        "dest": {**_pt(dest, full=True), "dist_km": dest.get("dist_km"),
                 "click": {"lat": dest_lat, "lng": dest_lng}},
        "gateways": [_gw(g) for g in gws],
        "direct": df,
        "result": clean,
        "weather": wx,
        "notes": notes,
    }


def _pt(a, full=False):
    base = {"iata": a["iata"], "lat": a["lat"], "lng": a["lng"]}
    if full:
        base.update({"name": a["name"], "city": a.get("city"), "hub": a["hub"]})
    return base


def _gw(g):
    out = {"iata": g["iata"], "name": g["name"], "city": g.get("city"),
           "lat": g["lat"], "lng": g["lng"],
           "hub": g["hub"], "ground_mode": g["ground_mode"], "ground_hours": g["ground_hours"],
           "ground_cost": g["ground_cost"], "source": g["source"], "notes": g.get("notes", ""),
           "fly": g.get("fly")}
    if g.get("ferry"):
        out["ferry"] = g["ferry"]
    if g.get("transit"):
        out["transit"] = g["transit"]
    return out


# --------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "hopandhaul/1.0"
    timeout = 15

    def version_string(self):
        # Drop the default "Python/3.x.y" fingerprint from the Server response header - 
        # no reason to hand a probing client the host's Python version.
        return "hopandhaul"

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ALLOWED_HOSTS

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https://a.basemaps.cartocdn.com "
            "https://b.basemaps.cartocdn.com https://c.basemaps.cartocdn.com "
            "https://d.basemaps.cartocdn.com; "
            "font-src 'self'; manifest-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _send_ok(self, payload: dict, code: int = 200):
        body = {"ok": True}
        body.update(payload)
        return self._send(code, body)

    def _send_err(self, code: int, err_code: str, message: str):
        return self._send(code, _err(err_code, message))

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if not self._host_ok():
            return self._send_err(403, "forbidden_host", "forbidden host")
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._serve_file(INDEX, "text/html; charset=utf-8", missing_code=500,
                                    missing_msg="index.html missing")
        asset = _resolve_ui_asset(u.path)
        if asset:
            return self._serve_file(asset[0], asset[1], missing_code=404, missing_msg="asset missing")
        if u.path == "/healthz":
            return self._send_ok({"version": __version__})
        if u.path == "/api/config":
            return self._handle_config()
        if u.path == "/api/geocode":
            return self._handle_geocode(parse_qs(u.query))
        if u.path == "/api/nearest":
            return self._handle_nearest(parse_qs(u.query))
        if u.path == "/api/plan":
            return self._handle_plan(parse_qs(u.query))
        if u.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        return self._send_err(404, "not_found", "not found")

    def _serve_file(self, path, ctype, missing_code, missing_msg):
        try:
            with open(path, "rb") as f:
                return self._send(200, f.read(), ctype)
        except FileNotFoundError:
            return self._send_err(missing_code, "not_found", missing_msg)
        except OSError as e:
            _log_exc("static", e)
            return self._send_err(500, "internal_error", "could not read that file")

    def _handle_config(self):
        # Never echo a key or its fingerprint here - booleans and provider *names* only.
        # This is the one endpoint the browser is guaranteed to call before any secret
        # exists in scope; keep it that way through any future edit.
        fp = flights.provider_name() if flights else None
        return self._send_ok({
            "has_live_keys": fp is not None,
            "flights_provider": fp,                       # "duffel" | None
            "has_geocode": places is not None,            # Photon keyless; Geoapify when keyed
            "geocode_provider": places.provider() if places else None,
            "has_weather": weather is not None,           # Open-Meteo, keyless
            "has_transit": transit is not None,           # Transitous, keyless
            "default_origin": os.environ.get("TRAVEL_ORIGIN", "JFK"),
            "default_threshold": trip.DEFAULT_THRESHOLD,
            "default_travelers": 1,
            "supports_return_date": True,
        })

    def _handle_geocode(self, q):
        try:
            params = parse_geocode_params(q)
        except ValidationError as e:
            return self._send_err(400, "invalid_param", e.message)
        if not places:
            return self._send_err(200, "geocoding_not_configured", "geocoding not configured")
        try:
            results = places.geocode(params["text"], limit=params["limit"])
        except (net.FetchError, urllib.error.HTTPError, urllib.error.URLError,
                ValueError, KeyError) as e:
            _log_exc("/api/geocode", e)
            return self._send_err(200, "geocode_lookup_failed", "geocoding lookup failed")
        except Exception as e:  # never leak internals to the browser; log server-side
            _log_exc("/api/geocode", e)
            return self._send_err(200, "internal_error", "geocoding lookup failed")
        return self._send_ok({"results": results})

    def _handle_nearest(self, q):
        try:
            params = parse_nearest_params(q)
        except ValidationError as e:
            return self._send_err(400, "invalid_param", e.message)
        try:
            a = geo.nearest_airport(params["lat"], params["lng"], prefer_hub=True)
        except Exception as e:
            _log_exc("/api/nearest", e)
            return self._send_err(200, "internal_error", "could not resolve nearest airport")
        if not a:
            return self._send_err(200, "no_airport_found", "no airport found")
        return self._send_ok({"airport": {
            "iata": a["iata"], "name": a["name"], "city": a.get("city"),
            "lat": a["lat"], "lng": a["lng"], "hub": a["hub"]}})

    def _handle_plan(self, q):
        try:
            params = parse_plan_params(q)
        except ValidationError as e:
            return self._send_err(400, "invalid_param", e.message)
        try:
            out = plan(
                params["dest_lat"], params["dest_lng"],
                origin_iata=params["origin_iata"],
                date=params["date"],
                vot=params["vot"],
                threshold=params["threshold"],
                max_ground_h=params["max_ground_h"],
                roundtrip=params["roundtrip"],
                travelers=params["travelers"],
                ret=params["ret"],
                transfer_buffer=params["transfer_buffer"],
            )
        except Exception as e:  # never leak internals to the browser; log server-side
            _log_exc("/api/plan", e)
            return self._send_err(200, "internal_error", "internal error planning that route")
        if not out.get("ok"):
            return self._send_err(200, out.get("code", "plan_failed"), out.get("error", "could not plan that route"))
        return self._send(200, out)


def serve(port=DEFAULT_PORT):
    trip._force_utf8()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    fp = flights.provider_name() if flights else None
    live = f"LIVE flights ({fp})" if fp else "ESTIMATE flights (set DUFFEL_API_KEY for live)"
    geoc = f"geocode ON ({places.provider()})" if places else "geocode off"
    wx = "weather ON (open-meteo)" if weather else "weather off"
    tr = "transit ON (transitous)" if transit else "transit off"
    print(f"hopandhaul map UI  ->  http://127.0.0.1:{port}")
    print(f"  [{live} · {geoc} · {wx} · {tr}]")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


# --------------------------------------------------------------------------- self-test
def selftest():
    trip._force_utf8()
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    check("index.html exists", os.path.exists(INDEX))
    # the UI is split into ES modules + a stylesheet - the asset resolver must serve them
    # (a regression here silently ships an unstyled, mapless page), and refuse traversal.
    check("serves ui/styles.css", _resolve_ui_asset("/styles.css") is not None)
    check("serves ui/app.js", _resolve_ui_asset("/app.js") is not None)
    check("serves vendored leaflet", _resolve_ui_asset("/vendor/leaflet.js") is not None)
    check("refuses path traversal", _resolve_ui_asset("/../server.py") is None
          and _resolve_ui_asset("/../../etc/passwd") is None)
    check("refuses unknown extension", _resolve_ui_asset("/secrets.env") is None)

    # end-to-end plan for a click on Aspen, origin JFK - estimate mode, no network.
    # (allow_live+allow_transit False -> no provider or Transitous calls; fetch_weather=False -> offline)
    out = plan(39.19, -106.82, origin_iata="JFK", vot=30, fetch_weather=False, allow_live=False, allow_transit=False)
    check("plan ok", out.get("ok") is True)
    check("dest resolved to ASE", out["dest"]["iata"] == "ASE")
    check("pricing source is estimate (no date)", out["pricing_source"] == "estimate")
    check("gateways discovered (incl DEN)", any(g["iata"] == "DEN" for g in out["gateways"]))
    # one-way DEN+bus (~$140+$75) saves ~$65 vs direct (~$280) - under $200, KEEP the direct.
    check("marginal split under $200 correctly rejected (rule works)",
          out["result"]["recommended"] == "Fly direct to ASE")
    # drop the rule under the saving and the DEN split should now win.
    out_lo = plan(39.19, -106.82, origin_iata="JFK", threshold=50, fetch_weather=False,
                  allow_live=False, allow_transit=False)
    reclo = out_lo["result"]["recommended"]
    check(f"with a $50 rule the DEN split wins (got {reclo})", reclo.startswith("DEN"))
    recopt = next(o for o in out_lo["result"]["options"] if o["name"] == reclo)
    check("recommended split carries flight+ground map geo", len(recopt.get("geo", [])) == 2)

    # every option carries an itinerary: real airports, an example clock schedule, per-leg
    # price provenance, and a verify link - not just a bare dollar figure.
    check("every option carries an itinerary with the right leg count",
          all(len(o["itinerary"]["legs"]) == o["nlegs"] for o in out_lo["result"]["options"]))
    itin_leg0 = recopt["itinerary"]["legs"][0]
    check("itinerary leg carries real airport identity (iata+name+city), not a bare code",
          itin_leg0["from"]["iata"] == "JFK" and itin_leg0["from"]["name"]
          and itin_leg0["to"]["iata"] == "DEN" and itin_leg0["to"]["name"])
    check("itinerary leg names its price provenance, not just a number",
          "estimate" in itin_leg0["price_basis"].lower())
    check("itinerary leg carries a verify link to Google Flights (a fly leg)",
          itin_leg0["verify_url"].startswith("https://www.google.com/travel/flights"))
    itin_leg1 = recopt["itinerary"]["legs"][1]
    check("the ground leg's verify link is Rome2Rio, not Google Flights",
          itin_leg1["verify_url"].startswith("https://www.rome2rio.com/map/"))
    check("an all-estimate itinerary is flagged example_day, with no live legs",
          recopt["itinerary"]["example_day"] is True and recopt["itinerary"]["any_live"] is False)
    check("a split's second leg departs after the first leg's arrival plus the transfer buffer",
          itin_leg1["depart_clock"] != itin_leg0["depart_clock"])

    # every option carries a co2e_kg estimate, and the response points at whichever is lowest
    check("every option has a non-negative co2e_kg estimate",
          all(o.get("co2e_kg") is not None and o["co2e_kg"] >= 0 for o in out["result"]["options"]))
    greenest_name = out["result"].get("greenest")
    check("top-level 'greenest' points at a real option name",
          greenest_name is not None
          and any(o["name"] == greenest_name for o in out["result"]["options"]))
    greenest_opt = next(o for o in out["result"]["options"] if o["name"] == greenest_name)
    check("'greenest' really is the lowest-co2e_kg option in the set (never picked by cost)",
          all(greenest_opt["co2e_kg"] <= o["co2e_kg"] for o in out["result"]["options"]))
    # the DEN split flies a shorter hop then trains/buses the rest - that should usually beat
    # the all-the-way-direct flight on CO2e even though the direct flight can still be cheaper.
    direct_opt = next(o for o in out["result"]["options"] if o["name"] == "Fly direct to ASE")
    den_opt = next((o for o in out["result"]["options"] if o["name"].startswith("DEN")), None)
    if den_opt:
        check(f"DEN split ({den_opt['co2e_kg']} kg) emits less than flying direct "
              f"({direct_opt['co2e_kg']} kg)", den_opt["co2e_kg"] < direct_opt["co2e_kg"])
    check("emissions note present, labels co2e_kg an estimate",
          any("co2e_kg" in n and "ESTIMATE" in n for n in out["notes"]))

    # click on a major hub (Denver) - should be fine flying direct, no split needed.
    out2 = plan(39.74, -104.99, origin_iata="JFK", fetch_weather=False, allow_live=False, allow_transit=False)
    check("Denver click recommends flying direct", out2["result"]["recommended"].startswith("Fly direct"))

    # a click in Manhattan must resolve to a real airline-served NYC airport, not the closest
    # point on the map (New York Skyports Inc Seaplane Base sits right off the FDR Drive).
    nyc = plan(40.71, -74.0, origin_iata="BOS", fetch_weather=False, allow_live=False, allow_transit=False)
    check(f"Manhattan click resolves to a real NYC airport (got {nyc['dest']['iata']})",
          nyc["dest"]["iata"] in {"JFK", "LGA", "EWR"})

    # a click in central Paris must resolve to CDG/ORY, not LBG (Le Bourget - business
    # aviation, no scheduled airline service).
    paris = plan(48.86, 2.35, origin_iata="JFK", fetch_weather=False, allow_live=False, allow_transit=False)
    check(f"Central Paris click resolves to CDG/ORY, not Le Bourget (got {paris['dest']['iata']})",
          paris["dest"]["iata"] in {"CDG", "ORY"})

    # a mid-ocean click has no airport within the max-km cap, so it's refused cleanly
    # instead of routing to an airport a thousand+ km away.
    out3 = plan(30.0, -40.0, origin_iata="JFK", fetch_weather=False, allow_live=False, allow_transit=False)
    check("ocean click with no airport in range is refused cleanly",
          (not out3["ok"]) and out3.get("code") == "no_airport_near_point")

    # clicking on your own origin airport must be refused, not priced as a real "direct
    # flight" to itself off the NA short-hop floor.
    same_origin = plan(40.64, -73.78, origin_iata="JFK", fetch_weather=False, allow_live=False, allow_transit=False)
    check("clicking your own origin airport is refused, not priced as a same-airport flight",
          (not same_origin["ok"]) and same_origin.get("code") == "origin_is_destination")

    # travelers: 4 people scale flights & buses ×4, but a rental/drive leg stays per-vehicle,
    # so the group total is strictly less than 4× the solo total whenever a drive leg exists.
    solo = plan(39.19, -106.82, origin_iata="JFK", fetch_weather=False, allow_live=False, allow_transit=False)
    grp = plan(39.19, -106.82, origin_iata="JFK", fetch_weather=False, allow_live=False, allow_transit=False,
               travelers=4)
    s_dir = next(o for o in solo["result"]["options"] if o["name"].startswith("Fly direct"))
    g_dir = next(o for o in grp["result"]["options"] if o["name"].startswith("Fly direct"))
    check("group of 4: direct flight cost ×4", abs(g_dir["cost"] - 4 * s_dir["cost"]) < 1)
    s_drv = next((o for o in solo["result"]["options"]
                  if any(leg["mode"] in ("drive", "rental", "car") for leg in o["legs"])), None)
    if s_drv:
        g_drv = next(o for o in grp["result"]["options"] if o["name"] == s_drv["name"])
        check("group of 4: drive-split scales less than ×4 (vehicle shared)",
              g_drv["cost"] < 4 * s_drv["cost"] - 1)
    check("group note present", any("GROUP TOTALS" in n for n in grp["notes"]))

    # round-trip with a return date (estimate mode): flight cost ≈ out + back, ground ×2.
    ow = plan(39.19, -106.82, origin_iata="JFK", date="2026-08-15", fetch_weather=False,
              allow_live=False, allow_transit=False)
    rt = plan(39.19, -106.82, origin_iata="JFK", date="2026-08-15", ret="2026-08-22",
              fetch_weather=False, allow_live=False, allow_transit=False)
    ow_dir = next(o for o in ow["result"]["options"] if o["name"].startswith("Fly direct"))
    rt_dir = next(o for o in rt["result"]["options"] if o["name"].startswith("Fly direct"))
    check("RT direct cost > one-way and < 2.6× (separate date multipliers)",
          ow_dir["cost"] < rt_dir["cost"] < 2.6 * ow_dir["cost"])
    check("RT note mentions the return", any("return" in n.lower() for n in rt["notes"]))
    check("dated estimate note mentions date adjustment",
          any("date-adjusted" in n for n in ow["notes"]))

    # ---- reliability regression: a live-but-failing key (401/429/5xx surfaced by net.py as
    # FetchError) must degrade that flight leg to the distance ESTIMATE, not break the whole
    # plan. Before this fix _price_flight()'s except tuple didn't include net.FetchError, so
    # this raised straight out of plan() and the endpoint returned {"ok": false}.
    import unittest.mock as _mock

    class _FakeSession:
        provider = "duffel"

    def _raise_fetch_error(*a, **kw):
        raise net.FetchError("HTTP 401 from api.duffel.com after 1 attempt(s)", status=401)

    with _mock.patch.object(flights, "have_keys", return_value=True), \
         _mock.patch.object(flights, "open_session", return_value={"provider": "duffel"}), \
         _mock.patch.object(flights, "search_cheapest", side_effect=_raise_fetch_error):
        out_fallback = plan(39.19, -106.82, origin_iata="JFK", date="2026-08-15",
                            fetch_weather=False, allow_live=True, allow_transit=False)
    check("a FetchError from the live provider still returns ok:True",
          out_fallback.get("ok") is True)
    check("pricing degrades to estimate on a live-provider FetchError",
          out_fallback.get("pricing_source") == "estimate")
    check("a live_error note is surfaced when the live lookup failed",
          any("fell back to estimates" in n for n in out_fallback["notes"]))

    # ---- wall-clock regression: PLAN_TIME_BUDGET_S must actually bound plan()'s runtime, not
    # just its own bookkeeping. Before this fix the `with ThreadPoolExecutor(...)` block's
    # __exit__ called shutdown(wait=True) and blocked until every hung worker returned - 
    # even the ones already given up on and re-priced as estimates - so a slow provider held
    # the whole response for its own timeout, not the budget. Mock search_cheapest to sleep
    # well past a short budget and confirm plan() returns quickly anyway.
    import time as _time

    def _slow_search_cheapest(*a, **kw):
        _time.sleep(3.0)
        raise net.FetchError("should never be awaited by plan()", status=599)

    # patch this module's OWN globals (not "hopandhaul.server" by dotted path) - run via
    # `python -m hopandhaul.server --selftest` this module is loaded as __main__, so a
    # string-path patch would patch a second, separately-imported copy of the module and
    # never touch the PLAN_TIME_BUDGET_S that the running plan() actually reads.
    _this_module = sys.modules[__name__]
    budget_start = _time.monotonic()
    with _mock.patch.object(flights, "have_keys", return_value=True), \
         _mock.patch.object(flights, "open_session", return_value={"provider": "duffel"}), \
         _mock.patch.object(flights, "search_cheapest", side_effect=_slow_search_cheapest), \
         _mock.patch.object(_this_module, "PLAN_TIME_BUDGET_S", 1.0):
        out_budget = plan(39.19, -106.82, origin_iata="JFK", date="2026-08-15",
                          fetch_weather=False, allow_live=True, allow_transit=False)
    budget_elapsed = _time.monotonic() - budget_start
    check(f"plan() returns near the 1s budget, not the 3s provider hang (took {budget_elapsed:.2f}s)",
          budget_elapsed < 2.0)
    check("a plan that hit the time budget still returns ok:True",
          out_budget.get("ok") is True)
    check("pricing degrades to estimate when the deadline is hit before any live result",
          out_budget.get("pricing_source") == "estimate")

    # ---- a successful live offer must produce a "live" itinerary leg: real segment times/
    # carrier from Duffel, not the synthetic 08:00-anchored example schedule.
    def _fake_live_search(session, origin_iata, dest_iata, date, adults, return_date):
        return {"price": 241.5, "hours": 5.5, "stops": 0, "carrier": "United Airlines",
                "currency": "USD", "converted": False, "source": "duffel", "rt": False,
                "checked_bags_included": 1, "refundable": False, "changeable": True,
                "native_price": 241.5,
                "segments": [{"from_iata": origin_iata, "to_iata": dest_iata,
                             "depart_at": datetime.datetime(2026, 8, 15, 8, 12),
                             "arrive_at": datetime.datetime(2026, 8, 15, 10, 5),
                             "carrier": "United Airlines", "flight_number": "UA1234"}]}

    with _mock.patch.object(flights, "have_keys", return_value=True), \
         _mock.patch.object(flights, "open_session", return_value={"provider": "duffel"}), \
         _mock.patch.object(flights, "search_cheapest", side_effect=_fake_live_search):
        out_live = plan(39.19, -106.82, origin_iata="JFK", date="2026-08-15",
                        fetch_weather=False, allow_live=True, allow_transit=False)
    check("a successful live search prices the plan live", out_live.get("pricing_source") != "estimate")
    live_direct = next(o for o in out_live["result"]["options"] if o["name"].startswith("Fly direct"))
    live_leg0 = live_direct["itinerary"]["legs"][0]
    check("a live-priced leg's itinerary uses the real segment clock times, not the synthetic 08:00 anchor",
          live_leg0["depart_clock"] == "08:12" and live_leg0["arrive_clock"] == "10:05")
    check("a live-priced leg carries the real carrier/flight number",
          live_leg0["is_live"] is True and live_leg0["carrier"] == "United Airlines"
          and live_leg0["flight_number"] == "UA1234")
    check("a live leg's price provenance says 'live', not 'estimate'",
          "live" in live_leg0["price_basis"].lower())
    check("an itinerary with a live leg is not flagged example_day",
          live_direct["itinerary"]["example_day"] is False)

    # ---- error-contract + input-validation checks (no HTTP needed - call the validators
    # and handlers' underlying helpers directly, matching what Handler does with parse_qs output)
    def qs(**kw):
        return {k: [v] for k, v in kw.items()}

    try:
        parse_plan_params(qs(lat="not-a-number", lng="-106.82"))
        check("plan: non-numeric lat rejected", False)
    except ValidationError:
        check("plan: non-numeric lat rejected", True)

    try:
        parse_plan_params(qs(lat="200", lng="-106.82"))
        check("plan: out-of-range lat rejected", False)
    except ValidationError:
        check("plan: out-of-range lat rejected", True)

    try:
        parse_plan_params(qs(lat="39.19", lng="-106.82", origin="THISISWAYTOOLONG"))
        check("plan: over-length origin rejected", False)
    except ValidationError:
        check("plan: over-length origin rejected", True)

    try:
        parse_plan_params(qs(lat="39.19", lng="-106.82", threshold="-5"))
        check("plan: negative threshold rejected", False)
    except ValidationError:
        check("plan: negative threshold rejected", True)

    try:
        parse_plan_params(qs(lat="39.19", lng="-106.82", travelers="50"))
        check("plan: travelers over cap rejected", False)
    except ValidationError:
        check("plan: travelers over cap rejected", True)

    try:
        parse_plan_params(qs(lat="39.19", lng="-106.82", date="2026-08-15", ret="2026-08-01"))
        check("plan: return date before depart date rejected", False)
    except ValidationError:
        check("plan: return date before depart date rejected", True)

    same_day = parse_plan_params(qs(lat="39.19", lng="-106.82", date="2026-08-15", ret="2026-08-15"))
    check("plan: return date equal to depart date is allowed",
          same_day["date"] == same_day["ret"] == "2026-08-15")

    try:
        parse_plan_params(qs(lat="39.19", lng="-106.82", origin="J3K"))
        check("plan: origin with a digit is rejected (ASCII-letters-only)", False)
    except ValidationError:
        check("plan: origin with a digit is rejected (ASCII-letters-only)", True)

    try:
        parse_plan_params(qs(lat="39.19", lng="-106.82", origin="JİK"))  # Unicode dotted I
        check("plan: non-ASCII 'letter' origin is rejected", False)
    except ValidationError:
        check("plan: non-ASCII 'letter' origin is rejected", True)

    try:
        parse_plan_params(qs(lat="39.19", lng="-106.82", date="2026-13-40"))
        check("plan: invalid calendar date rejected", False)
    except ValidationError:
        check("plan: invalid calendar date rejected", True)

    good = parse_plan_params(qs(lat="39.19", lng="-106.82", origin="jfk", travelers="4"))
    check("plan: valid params parsed and normalized (origin upper-cased)",
          good["origin_iata"] == "JFK" and good["travelers"] == 4)

    try:
        parse_geocode_params(qs(q=""))
        check("geocode: empty q rejected", False)
    except ValidationError:
        check("geocode: empty q rejected", True)

    try:
        parse_geocode_params(qs(q="x" * (MAX_QUERY_TEXT_LEN + 1)))
        check("geocode: over-length q rejected", False)
    except ValidationError:
        check("geocode: over-length q rejected", True)

    try:
        parse_geocode_params(qs(q="Aspen", limit="999"))
        check("geocode: limit clamped/rejected outside 1-10", False)
    except ValidationError:
        check("geocode: limit clamped/rejected outside 1-10", True)

    try:
        parse_nearest_params(qs(lat="39.19", lng="200"))
        check("nearest: out-of-range lng rejected", False)
    except ValidationError:
        check("nearest: out-of-range lng rejected", True)

    # geocode error shape: no geoapify keys configured in this offline selftest run, so the
    # handler-level fallback path returns the generic {ok:false, error, code} shape - never a
    # raw exception string (this is the exact bug DESIGN.md flags: f"{type(e).__name__}: {e}").
    err = _err("geocode_lookup_failed", "geocoding lookup failed")
    check("error contract: geocode error shape has ok/error/code, no exception internals",
          err == {"ok": False, "error": "geocoding lookup failed", "code": "geocode_lookup_failed"}
          and "Traceback" not in err["error"] and "Error:" not in err["error"])

    # rate limiter: a bucket with 0 capacity should never grant a token.
    empty_bucket = TokenBucket(rate_per_s=0.0, capacity=0.0)
    check("rate limiter: empty bucket refuses a token", empty_bucket.try_take() is False)
    full_bucket = TokenBucket(rate_per_s=1.0, capacity=3.0)
    took = [full_bucket.try_take() for _ in range(3)]
    check("rate limiter: capacity grants exactly its burst size", all(took) and not full_bucket.try_take())

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (server checks)")
    return 1 if fails else 0


def main(argv=None) -> int:
    """Console-script entry point (`hopandhaul-serve`)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    serve(args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
