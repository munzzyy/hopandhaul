#!/usr/bin/env python3
"""
geo.py — spatial + price-estimation layer for the travel-scout map UI.

Turns a lat/lng map click into: the nearest airport, a set of candidate "cheaper gateway
airport + ground leg" splits (curated from gateways.json *and* auto-discovered from the airport
DB anywhere on Earth), and — when no live API key is set — transparent distance-based fare and
ground estimates so the map still "calculates" instantly at $0.

Real data raises the floor under those estimates: ferry legs only exist along real corridors
(data/ferries.json — operators, crossing times, sourced fares), land legs never cross open sea
(data/landgrid.json + sea_gap()), and US fares are clamped into the real BTS market band for
the route (data/fareanchors.json). Everything else is labelled ESTIMATE everywhere it
surfaces — a reasonable model, not a quote; the server upgrades flight legs to live Duffel
pricing when a key exists.

Pure stdlib. Importable by server.py. Run `python geo.py --selftest`.
"""
from __future__ import annotations

import base64
import datetime
import importlib.resources
import json
import math

_DATA_PKG = "hopandhaul.data"

# ---- fare model (ESTIMATE) -------------------------------------------------
# Concave curve calibrated against real cheapest one-way economy fares (2025-26):
#   fare ≈ 30 + 1.8·√km + 0.012·km
# e.g. ~$65 LGA-BOS, ~$190 JFK-LAX, ~$230 JFK-LHR, ~$350 JFK-NRT — then adjusted for
# airport size (small/resort fields carry a premium, both ends), hub-to-hub competition,
# the ROUTE MARKET (intra-Europe LCC saturation ≠ US domestic ≠ thin intra-Africa
# competition — see ROUTE_MULT), and the travel DATE (booking lead time, seasonality,
# day of week). Still an ESTIMATE.
FLIGHT_CURVE = (30.0, 1.8, 0.012)          # (base, per_sqrt_km, per_km)
FLIGHT_FLOOR = 45.0
NA_SHORT_FLOOR = 65.0                      # US/Canada short hops: fixed costs dominate
SMALL_AIRPORT_PREMIUM = {1: 0.0, 2: 0.18, 3: 0.75}   # by hub tier (full at dest, half at origin)
HUB_COMPETITION_DISCOUNT = 0.92            # both ends tier-1: competitive trunk route
FLIGHT_FIXED_H = 1.1                       # per-flight ground/air overhead (hours)
CONNECTION_H = 1.6                         # likely-connection time when a tiny field is far away

# Route-market multiplier by (region, region) pair — competition/LCC density varies by
# market far more than by distance. NA-NA = 1.0 is the calibration baseline. Keyed by
# sorted region pair; pairs not listed: 1.15 if Africa is involved, else 1.0.
ROUTE_MULT = {
    ("EU", "EU"): 0.55,      # Ryanair/Wizz/easyJet saturation
    ("SEA", "SEA"): 0.55,    # AirAsia/Scoot/VietJet/Lion
    ("IN", "IN"): 0.55,      # IndiGo et al.
    ("KR", "KR"): 0.60, ("CN", "CN"): 0.75, ("JP", "JP"): 0.85,
    ("NA", "NA"): 1.00, ("LATAM", "LATAM"): 0.95, ("AU", "AU"): 0.90,
    ("ME", "ME"): 0.85, ("ZA", "ZA"): 0.80, ("AF", "AF"): 1.60,
    ("RU", "RU"): 1.20,      # thin domestic network across Russia/Central Asia, few LCCs
    ("EU", "NA"): 0.85,      # transatlantic is brutally competitive
    ("EU", "ME"): 0.80, ("EU", "IN"): 0.90, ("CN", "EU"): 0.90,
    ("EU", "JP"): 0.90, ("EU", "SEA"): 0.90, ("EU", "LATAM"): 0.95,
    ("AF", "EU"): 0.90,      # LCC/charter to Morocco, Egypt, Canaries-adjacent
    ("AF", "NA"): 1.15, ("AF", "ME"): 1.00, ("AF", "ZA"): 1.15,
    ("JP", "KR"): 0.80, ("KR", "SEA"): 0.85, ("JP", "SEA"): 0.80,
    ("CN", "SEA"): 0.85, ("CN", "JP"): 0.90, ("CN", "KR"): 0.90,
    ("IN", "SEA"): 0.80, ("IN", "ME"): 0.85, ("ME", "SEA"): 0.90,
    ("JP", "NA"): 1.25, ("CN", "NA"): 1.30, ("AU", "NA"): 1.05,
    ("LATAM", "NA"): 0.95, ("AU", "SEA"): 0.85,
    ("EU", "RU"): 0.95, ("CN", "RU"): 0.95, ("RU", "SEA"): 1.05,
}
assert all(k == tuple(sorted(k)) for k in ROUTE_MULT), \
    "ROUTE_MULT keys must be pre-sorted — _route_mult only ever looks up a sorted tuple"


def _route_mult(region_a: str, region_b: str) -> float:
    key = tuple(sorted((region_a, region_b)))
    if key in ROUTE_MULT:
        return ROUTE_MULT[key]
    if "AF" in key:
        return 1.15          # thin competition on most unlisted Africa pairs
    return 1.0

# lead-time booking curve: (max_days_out, fare multiplier) — walk-up fares are much dearer,
# ~3-6 weeks out is the sweet spot, ultra-early long-haul prices slightly high.
LEAD_CURVE = [(3, 1.45), (6, 1.30), (13, 1.18), (20, 1.08), (45, 1.00),
              (90, 0.96), (180, 1.00), (99999, 1.05)]
MONTH_MULT = {1: 0.92, 2: 0.92, 3: 1.03, 4: 1.00, 5: 1.00, 6: 1.10,
              7: 1.12, 8: 1.08, 9: 0.93, 10: 0.93, 11: 0.97, 12: 1.15}
DOW_MULT = {0: 1.00, 1: 0.96, 2: 0.96, 3: 1.00, 4: 1.06, 5: 1.00, 6: 1.06}  # Mon..Sun
DATE_MULT_CLAMP = (0.75, 1.75)

def _flight_speed_kmh(d: float) -> float:
    """Effective block speed incl. taxi/climb — long-haul cruises faster."""
    return 700.0 if d < 1500 else (800.0 if d < 6000 else 850.0)

# ---- ground model (ESTIMATE) ----------------------------------------------
ROAD_WINDING = 1.2          # straight-line -> road/rail distance factor
GROUND = {   # default (rest of world) — mode: (speed_kmh, base_$, per_km_$)
    "drive":   (85, 15, 0.20),
    "car":     (85, 15, 0.20),
    "rental":  (85, 20, 0.20),
    "bus":     (70, 4, 0.08),
    "coach":   (70, 4, 0.08),
    "shuttle": (65, 6, 0.15),
    "train":   (110, 6, 0.12),
    "rail":    (110, 6, 0.12),
    "ferry":   (40, 10, 0.25),
    "ground":  (80, 8, 0.15),
}
# Regional overrides: rail quality/cost varies enormously (EU high-speed vs Amtrak vs
# Shinkansen vs Chinese HSR vs South/Southeast Asia). Missing modes fall back to GROUND.
REGION_GROUND = {
    "EU":    {"train": (150, 8, 0.15), "rail": (150, 8, 0.15), "bus": (75, 5, 0.06),
              "drive": (90, 20, 0.26), "car": (90, 20, 0.26), "rental": (90, 25, 0.26)},
    "NA":    {"train": (95, 8, 0.15), "rail": (95, 8, 0.15), "bus": (80, 5, 0.09),
              "drive": (90, 15, 0.20), "car": (90, 15, 0.20), "rental": (90, 22, 0.20)},
    "JP":    {"train": (190, 10, 0.22), "rail": (190, 10, 0.22), "bus": (70, 8, 0.10)},
    "KR":    {"train": (150, 5, 0.09), "rail": (150, 5, 0.09), "bus": (80, 4, 0.05)},
    "CN":    {"train": (200, 5, 0.075), "rail": (200, 5, 0.075), "bus": (65, 4, 0.05)},
    "IN":    {"train": (55, 3, 0.03), "rail": (55, 3, 0.03), "bus": (55, 3, 0.04)},
    "SEA":   {"train": (60, 3, 0.04), "rail": (60, 3, 0.04), "bus": (65, 3, 0.045)},
    "LATAM": {"bus": (75, 3, 0.05), "train": (70, 5, 0.08), "rail": (70, 5, 0.08)},
    "AU":    {"train": (100, 8, 0.13), "rail": (100, 8, 0.13), "drive": (90, 18, 0.22)},
    "ME":    {"bus": (80, 4, 0.05), "drive": (100, 15, 0.15), "car": (100, 15, 0.15)},
    "ZA":    {"bus": (85, 4, 0.06), "drive": (95, 15, 0.18), "car": (95, 15, 0.18)},
    "AF":    {"bus": (60, 3, 0.05), "train": (50, 4, 0.05), "rail": (50, 4, 0.05),
              "drive": (70, 15, 0.22), "car": (70, 15, 0.22)},
    "RU":    {"train": (70, 6, 0.06), "rail": (70, 6, 0.06), "bus": (60, 5, 0.06),
              "drive": (75, 15, 0.18), "car": (75, 15, 0.18)},
}
GROUND_ACCESS_H = 0.3       # station/stop access + egress buffer


# --------------------------------------------------------------------------- data
_AIRPORTS = None
_GATEWAYS = None
_FERRIES = None
_LANDGRID = None


def _read_package_json(filename: str):
    """Read a JSON file shipped as package data — works from a repo checkout AND a
    zipped wheel install alike (importlib.resources resolves either)."""
    ref = importlib.resources.files(_DATA_PKG) / filename
    with ref.open("r", encoding="utf-8") as f:
        return json.load(f)


def airports() -> list[dict]:
    global _AIRPORTS
    if _AIRPORTS is None:
        _AIRPORTS = _read_package_json("airports.json")["airports"]
    return _AIRPORTS


def gateways_db() -> dict:
    global _GATEWAYS
    if _GATEWAYS is None:
        _GATEWAYS = _read_package_json("gateways.json")
    return _GATEWAYS


def ferry_corridors() -> list[dict]:
    """Real passenger-ferry corridors (data/ferries.json): operators, typical crossing time,
    real fare range, sailings/day — researched from operator/aggregator pages, each entry
    carrying its own source URL and as-of date. The engine never invents a boat: a ferry leg
    exists only when one of these corridors connects the two places."""
    global _FERRIES
    if _FERRIES is None:
        _FERRIES = _read_package_json("ferries.json")["corridors"]
    return _FERRIES


_ANCHORS = None
_ANCHORS_ASOF = None


def fare_anchors() -> list[dict]:
    """REAL average fares for the busiest US city-pair markets (data/fareanchors.json, from
    the US DOT/BTS Consumer Airfare Report — public domain). Each anchor: city centroids a/b,
    fare_avg (market average paid), fare_low (average on the lowest-fare carrier), pax_day."""
    global _ANCHORS, _ANCHORS_ASOF
    if _ANCHORS is None:
        raw = _read_package_json("fareanchors.json")
        _ANCHORS = raw["anchors"]
        _ANCHORS_ASOF = raw.get("asof", "")
    return _ANCHORS


def fare_anchors_asof() -> str:
    fare_anchors()
    return _ANCHORS_ASOF or ""


def _landgrid():
    global _LANDGRID
    if _LANDGRID is None:
        raw = _read_package_json("landgrid.json")
        _LANDGRID = {"bits": base64.b64decode(raw["b64"]), "w": raw["w"], "h": raw["h"],
                     "res": raw["res_deg"]}
    return _LANDGRID


_AIRPORTS_BY_IATA = None


def by_iata(code: str) -> dict | None:
    global _AIRPORTS_BY_IATA
    if _AIRPORTS_BY_IATA is None:
        _AIRPORTS_BY_IATA = {a["iata"]: a for a in airports()}
    return _AIRPORTS_BY_IATA.get((code or "").upper())


# --------------------------------------------------------------------------- geometry
def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return round(2 * r * math.asin(min(1.0, math.sqrt(a))), 1)


NEAREST_SOFT_KM = 120     # dist_km above this: last-mile note (existing server.py behavior)
NEAREST_WARN_KM = 400     # above this: stronger "this is a long way off" warning tier
NEAREST_HARD_KM = 700     # above this: refuse — the click has no meaningfully "nearest" airport


def nearest_airport(lat, lng, prefer_hub=False, max_km=NEAREST_HARD_KM) -> dict | None:
    """Closest airport to a point, capped at max_km so a click in the middle of nowhere
    (open ocean, polar regions) can't silently resolve to an airport hundreds of km away
    and get treated as a real plan. Pass max_km=None to disable the cap entirely.

    Returns None if nothing is within max_km. Otherwise the airport dict plus dist_km and
    warn_tier: None (< NEAREST_SOFT_KM), "soft" (< NEAREST_WARN_KM), "hard" (>= NEAREST_WARN_KM
    but still under max_km) — callers decide how loudly to surface each tier."""
    best, best_score = None, None
    for a in airports():
        d = haversine_km(lat, lng, a["lat"], a["lng"])
        if max_km is not None and d > max_km:
            continue
        # a gentle hub bias so a click near a city snaps to the real hub over a tiny strip
        score = d + (a["hub"] - 1) * 20 if prefer_hub else d
        if best_score is None or score < best_score:
            best, best_score = a, score
    if best is None:
        return None
    dist_km = haversine_km(lat, lng, best["lat"], best["lng"])
    warn_tier = "hard" if dist_km >= NEAREST_WARN_KM else ("soft" if dist_km >= NEAREST_SOFT_KM else None)
    return dict(best, dist_km=dist_km, warn_tier=warn_tier)


# --------------------------------------------------------------------------- regions
def region_of(lat: float, lng: float) -> str:
    """Coarse region for ground-transport quality/cost and route-market pricing.
    First match wins — order is load-bearing around the Mediterranean (Malta/Crete/
    Cyprus are EU; Morocco/Egypt AF; Levant/Gulf ME), Korea vs Japan, and Russia/Central
    Asia vs its CN/JP/EU neighbors (Vladivostok, Sakhalin, Kamchatka sit at JP-like
    longitudes but are RU; Mongolia/Kazakhstan sit at CN-like longitudes but aren't CN)."""
    if 41 <= lat <= 82 and 41 <= lng <= 180:
        return "RU"           # Urals through the Russian Far East (Vladivostok, Sakhalin,
                               # Kamchatka), plus Kazakhstan/Kyrgyzstan/Mongolia/China's far
                               # northwest at these longitudes — checked before CN/JP so
                               # they don't claim this territory first
    if 41 <= lat <= 82 and -180 <= lng <= -169:
        return "RU"           # Chukotka, wrapped across the antimeridian
    if 55 <= lat <= 82 and 30 <= lng < 41:
        return "RU"           # European Russia (St Petersburg/Murmansk/Moscow), east of
                               # Finland/the Baltics so those stay EU
    if 33 <= lat <= 39.5 and 124.5 <= lng < 129.4:
        return "KR"
    if 30 <= lat <= 46 and 129.4 <= lng <= 146:
        return "JP"
    if 18 <= lat <= 54 and 97 <= lng <= 127:
        return "CN"
    if 5 <= lat <= 33 and 60 <= lng <= 93:
        return "IN"
    if -11 <= lat <= 25 and 93 <= lng <= 142:
        return "SEA"
    if 34 <= lat < 36 and 12 <= lng <= 36:
        return "EU"          # Mediterranean EU islands: Malta, Crete, Rhodes-south, Cyprus
    if 12 <= lat <= 42 and 33 <= lng <= 63:
        return "ME"          # Levant + Arabian peninsula + Red Sea coasts + east Turkey
    if 27 <= lat <= 35.95 and -13 <= lng <= -1.5:
        return "AF"          # Morocco (south of Tarifa — all of Spain stays EU)
    if 21 <= lat <= 31.8 and 24 <= lng < 33:
        return "AF"          # Egypt interior/Nile (Red Sea resorts price like ME above)
    if (36 <= lat <= 72 and -11 <= lng <= 32) or (27 <= lat <= 40 and -32 <= lng < -13):
        return "EU"          # mainland Europe + Canaries/Madeira/Azores
    if 62 <= lat <= 67.5 and -25 <= lng <= -13:
        return "EU"          # Iceland
    if -59 <= lat <= -44 and -73 <= lng <= -56:
        return "LATAM"       # Tierra del Fuego / far-south Chile-Argentina (below the LATAM box)
    if 58 <= lat <= 84 and -75 <= lng <= -10:
        return "EU"          # Greenland (Danish territory; nearest real ground/fare market)
    if 18 <= lat <= 72 and -170 <= lng <= -52:
        return "NA"          # includes Hawaii (main islands bottom out ~18.9N)
    if -56 <= lat < 24 and -120 <= lng <= -30:
        return "LATAM"
    if -48 <= lat <= -9 and 112 <= lng <= 180:
        return "AU"
    if -35 <= lat <= -22 and 16 <= lng <= 33:
        return "ZA"          # South Africa + Namibia: real domestic LCC competition
    if -35 <= lat <= 37 and -18 <= lng <= 52:
        return "AF"
    if -30 <= lat <= 24 and (-180 <= lng <= -134 or 130 <= lng <= 180):
        return "AU"          # scattered Pacific (Micronesia/French Polynesia/Samoa/Tonga/
                              # Guam) not already caught by the SEA/AU boxes above — Oceania
                              # is the nearest real hub market for route-pricing purposes
    return "OTHER"


# --------------------------------------------------------------------------- estimates
def is_past_date(date_str: str | None, today: datetime.date | None = None) -> bool:
    """True if date_str parses to a real YYYY-MM-DD date strictly before today. A bad/
    missing date isn't "past" here — that's a separate, already-handled invalid case."""
    if not date_str:
        return False
    try:
        d = datetime.date.fromisoformat(str(date_str))
    except ValueError:
        return False
    return d < (today or datetime.date.today())


def fare_date_multiplier(date_str: str | None, today: datetime.date | None = None) -> float:
    """Fare multiplier for a YYYY-MM-DD travel date: booking lead time × season × weekday.
    1.0 when no/invalid/past date — a past date has no meaningful booking-lead-time curve,
    so this deliberately falls back to neutral rather than guessing; callers that care
    whether the date was actually in the past should check is_past_date() themselves
    (estimate_flight does, and flags it in its output). Deterministic when `today` is
    supplied (tests)."""
    if not date_str or is_past_date(date_str, today):
        return 1.0
    try:
        d = datetime.date.fromisoformat(str(date_str))
    except ValueError:
        return 1.0
    today = today or datetime.date.today()
    days_out = (d - today).days
    lead = 1.0
    for max_days, mult in LEAD_CURVE:
        if days_out <= max_days:
            lead = mult
            break
    m = lead * MONTH_MULT.get(d.month, 1.0) * DOW_MULT.get(d.weekday(), 1.0)
    lo, hi = DATE_MULT_CLAMP
    return round(max(lo, min(hi, m)), 3)


# US fare anchoring: bookable-cheapest sits between roughly 45% and 100% of the lowest-fare
# carrier's AVERAGE paid fare (DB1B averages mix advance buys with walk-ups), so the anchor's
# job is to BOUND the curve, not replace it — it catches the routes the formula misprices
# (thin regional monopolies it underprices) and leaves well-calibrated trunk routes alone.
ANCHOR_MATCH_KM = 60.0        # airport must sit this close to the BTS city-market centroid
ANCHOR_LO_FRAC = 0.45         # cheapest bookable is rarely below 45% of the low-carrier avg
ANCHOR_HI_FRAC = 1.00         # ...and never above it, for a "cheapest fare" estimate


def fare_anchor_for(orig: dict, dest: dict) -> dict | None:
    """The busiest real BTS city-pair market covering these two airports, or None."""
    best = None
    for an in fare_anchors():
        for p, q in ((an["a"], an["b"]), (an["b"], an["a"])):
            if (haversine_km(orig["lat"], orig["lng"], p[0], p[1]) <= ANCHOR_MATCH_KM
                    and haversine_km(dest["lat"], dest["lng"], q[0], q[1]) <= ANCHOR_MATCH_KM):
                if best is None or an["pax_day"] > best["pax_day"]:
                    best = an
                break
    return best


def estimate_flight(orig: dict, dest: dict, date: str | None = None,
                    today: datetime.date | None = None) -> dict:
    """ESTIMATE cheapest one-way economy fare + flight time between two airports.
    Route-market aware (region-pair competition), date-aware when a YYYY-MM-DD date
    is given (booking curve, season, weekday), and connection-aware for tiny fields.
    US domestic pairs are additionally ANCHORED to the real BTS market fares for the
    route (see fare_anchors): the model fare is clamped into the band real fares imply,
    and the real averages ride along in the output for provenance."""
    d = haversine_km(orig["lat"], orig["lng"], dest["lat"], dest["lng"])
    base, per_sqrt, per_km = FLIGHT_CURVE
    fare = base + per_sqrt * math.sqrt(d) + per_km * d
    # small/expensive airports raise fares — fully at the destination, half at the origin.
    fare *= (1 + SMALL_AIRPORT_PREMIUM.get(dest["hub"], 0.0))
    fare *= (1 + 0.5 * SMALL_AIRPORT_PREMIUM.get(orig["hub"], 0.0))
    if orig["hub"] == 1 and dest["hub"] == 1:
        fare *= HUB_COMPETITION_DISCOUNT
    r_o = region_of(orig["lat"], orig["lng"])
    r_d = region_of(dest["lat"], dest["lng"])
    rm = _route_mult(r_o, r_d)
    fare *= rm

    anchor = None
    anchor_adjusted = False
    if r_o == "NA" and r_d == "NA":
        anchor = fare_anchor_for(orig, dest)
        if anchor:
            lo = ANCHOR_LO_FRAC * anchor["fare_low"]
            hi = ANCHOR_HI_FRAC * anchor["fare_low"]
            clamped = max(lo, min(hi, fare))
            anchor_adjusted = abs(clamped - fare) >= 0.5
            fare = clamped

    dm = fare_date_multiplier(date, today)
    fare *= dm
    floor = NA_SHORT_FLOOR if (r_o == r_d == "NA" and d < 400) else FLIGHT_FLOOR
    fare = max(floor, fare)
    hours = FLIGHT_FIXED_H + d / _flight_speed_kmh(d)
    # a tiny field far from the origin almost always means one connection — count it.
    connects = d > 2000 and (orig["hub"] == 3 or dest["hub"] == 3)
    if connects:
        hours += CONNECTION_H
    out = {"price": round(fare / 5) * 5, "hours": round(hours, 1),
           "distance_km": d, "source": "estimate", "route_mult": rm,
           "regions": f"{r_o}-{r_d}", "likely_connection": connects}
    if anchor:
        out["anchor"] = {"fare_avg": anchor["fare_avg"], "fare_low": anchor["fare_low"],
                         "pax_day": anchor["pax_day"], "asof": fare_anchors_asof(),
                         "adjusted": anchor_adjusted}
    if dm != 1.0:
        out["date_mult"] = dm
    if is_past_date(date, today):
        out["past_date"] = True   # date_mult was left neutral — there's no real lead-time
                                   # curve for a date that's already gone; callers should warn
    return out


# Distance breakpoints for the default drive -> train -> bus progression, by region group.
MODE_BREAKS = {
    ("EU", "JP", "CN", "KR"): [(80, "drive"), (650, "train"), (None, "bus")],
    ("NA",): [(250, "drive"), (None, "bus")],
    ("LATAM", "ME", "ZA", "AF"): [(120, "drive"), (None, "bus")],
    ("OTHER",): [(120, "drive"), (450, "train"), (None, "bus")],
}
# Near a breakpoint, pick whichever of the two neighboring modes has the lower generalized
# cost (cash cost + time priced at a modest shadow value-of-time) instead of a hard cutoff.
# This makes the choice itself continuous: the crossover happens wherever the two modes'
# real cost/time curves actually cross, which barely moves for a few km of distance either
# way, instead of at a fixed km figure that flips instantly regardless of how close the two
# options actually were. Blending the two modes' raw numbers together (rather than picking
# one) was tried and rejected — train and bus diverge enough in speed/cost over a long haul
# that averaging them invents a fake third option instead of smoothing a real one.
MODE_TIE_BAND_KM = 25
SHADOW_VOT_USD_PER_HOUR = 15.0


def _mode_breaks(region: str) -> list:
    for regions, breaks in MODE_BREAKS.items():
        if region in regions:
            return breaks
    return MODE_BREAKS[("OTHER",)]


def _ground_leg(dist_km: float, mode: str, region: str) -> dict:
    """Raw (unrounded) cost/time for one mode at one distance."""
    table = REGION_GROUND.get(region, {})
    speed, base, per_km = table.get(mode) or GROUND.get(mode, GROUND["ground"])
    road_km = dist_km * ROAD_WINDING
    hours = road_km / speed + GROUND_ACCESS_H
    cost = base + per_km * road_km
    return {"cost": cost, "hours": hours, "road_km": road_km}


def _score(dist_km: float, mode: str, region: str) -> float:
    leg = _ground_leg(dist_km, mode, region)
    return leg["cost"] + leg["hours"] * SHADOW_VOT_USD_PER_HOUR


def pick_ground_mode(dist_km: float, region: str = "OTHER") -> str:
    """Sensible default ground mode by distance for auto-discovered gateways.
    Region-aware: strong-rail regions train much farther; the US mostly drives/buses.
    Within MODE_TIE_BAND_KM of a breakpoint, defers to whichever neighboring mode scores
    lower on cost+time instead of snapping on the exact km figure — see MODE_TIE_BAND_KM."""
    breaks = _mode_breaks(region)
    limits = [b[0] for b in breaks if b[0] is not None]
    modes_in_order = [b[1] for b in breaks]
    for i, limit in enumerate(limits):
        if dist_km <= limit + MODE_TIE_BAND_KM:
            if dist_km > limit - MODE_TIE_BAND_KM:
                a, b = modes_in_order[i], modes_in_order[i + 1]
                return a if _score(dist_km, a, region) <= _score(dist_km, b, region) else b
            return modes_in_order[i]
    return modes_in_order[-1]


def estimate_ground(dist_km: float, mode: str, region: str = "OTHER") -> dict:
    """ESTIMATE cost + time for a ground leg of a given straight-line distance."""
    mode = mode.lower()
    leg = _ground_leg(dist_km, mode, region)
    cost, hours = round(leg["cost"] / 5) * 5, round(leg["hours"], 1)
    return {"cost": max(5, cost), "hours": hours, "mode": mode,
            "road_km": round(leg["road_km"], 1)}


# --------------------------------------------------------------------------- landmasses
# Ground legs can't cross open water. Airports on islands (or road-isolated towns like
# Juneau) get an explicit landmass label; everything else defaults to a continent bucket.
# Cross-landmass gateway pairs are only kept when close enough for a plausible ferry.
ISLAND_LANDMASS = {
    # Mediterranean
    "JTR": "santorini", "JMK": "mykonos", "PAS": "paros", "CFU": "corfu", "ZTH": "zakynthos",
    "EFL": "kefalonia", "JSI": "skiathos", "HER": "crete", "CHQ": "crete", "JSH": "crete",
    "RHO": "rhodes", "KGS": "kos", "MJT": "lesbos", "SMI": "samos", "JNX": "naxos",
    "MLA": "malta", "LCA": "cyprus", "PFO": "cyprus",
    "PMO": "sicily", "CTA": "sicily", "TPS": "sicily",
    "CAG": "sardinia", "OLB": "sardinia", "AHO": "sardinia", "AJA": "corsica", "BIA": "corsica",
    "PMI": "mallorca", "IBZ": "ibiza", "MAH": "menorca",
    # Atlantic
    "TFS": "tenerife", "TFN": "tenerife", "LPA": "grancanaria", "ACE": "lanzarote",
    "FUE": "fuerteventura", "SPC": "lapalma", "FNC": "madeira", "PDL": "azores",
    "KEF": "iceland", "FAE": "faroe", "SID": "capeverde",
    "DUB": "ireland", "ORK": "ireland", "SNN": "ireland", "NOC": "ireland",
    "BFS": "ireland", "BHD": "ireland",
    "JER": "jersey", "GCI": "guernsey", "IOM": "isleofman",
    "LSI": "shetland", "KOI": "orkney",
    "MHQ": "aland", "VBY": "gotland", "RNN": "bornholm",
    "YYT": "newfoundland",
    # Caribbean + nearby
    "HAV": "cuba", "VRA": "cuba", "PUJ": "hispaniola", "SDQ": "hispaniola",
    "POP": "hispaniola", "STI": "hispaniola", "PAP": "hispaniola",
    "SJU": "puertorico", "BQN": "puertorico", "PSE": "puertorico",
    "STT": "stthomas", "STX": "stcroix", "SXM": "stmartin", "SBH": "stbarts",
    "AUA": "aruba", "CUR": "curacao", "BON": "bonaire", "MBJ": "jamaica", "KIN": "jamaica",
    "GCM": "cayman", "BGI": "barbados", "ANU": "antigua", "SKB": "stkitts", "EIS": "tortola",
    "UVF": "stlucia", "GND": "grenada", "POS": "trinidad", "FDF": "martinique",
    "PTP": "guadeloupe", "NAS": "newprovidence", "FPO": "grandbahama", "GGT": "exuma",
    "PLS": "provo", "CZM": "cozumel", "RTB": "roatan", "ADZ": "sanandres", "BZE": "mainland",
    # Indian Ocean / Africa
    "ZNZ": "zanzibar", "MRU": "mauritius", "RUN": "reunion", "SEZ": "seychelles",
    "TNR": "madagascar", "CMB": "srilanka", "MLE": "maldives",
    # Asia-Pacific
    "CJU": "jeju", "OKA": "okinawa", "TPE": "taiwan", "KHH": "taiwan", "SYX": "hainan",
    "HKG": "mainland", "MFM": "mainland", "SIN": "mainland",   # bridge/causeway-connected
    "PEN": "mainland", "HKT": "mainland",                      # bridges to the mainland
    "LGK": "langkawi", "USM": "samui", "PQC": "phuquoc",
    "BKI": "borneo", "BWN": "borneo",
    "CGK": "java", "YIA": "java", "SUB": "java", "KNO": "sumatra", "DPS": "bali",
    "LOP": "lombok", "LBJ": "flores",
    "MNL": "luzon", "CRK": "luzon", "CEB": "cebu", "KLO": "panay", "MPH": "panay",
    "TAG": "bohol", "PPS": "palawan",
    "HNL": "oahu", "OGG": "maui", "KOA": "hawaiibig", "ITO": "hawaiibig", "LIH": "kauai",
    "AKL": "nznorth", "WLG": "nznorth", "CHC": "nzsouth", "ZQN": "nzsouth",
    "HBA": "tasmania", "HTI": "whitsundays",
    "YYJ": "vancouverisland", "YWH": "vancouverisland", "YCD": "vancouverisland",
    "YQQ": "vancouverisland", "FRD": "sanjuan", "ESD": "sanjuan",
    "NAN": "fiji", "PPT": "tahiti", "BOB": "borabora", "RAR": "rarotonga", "GUM": "guam",
    "YGR": "magdalen",
    "IPC": "easterisland", "ACK": "nantucket", "MVY": "marthasvineyard",
    "JNU": "juneau", "KTN": "ketchikan",   # SE Alaska: no road out
}
_CONTINENT = {"NA": "americas", "LATAM": "americas", "EU": "eurasia", "ME": "eurasia",
              "CN": "eurasia", "IN": "eurasia", "SEA": "eurasia", "KR": "korea",
              "JP": "japan", "AU": "australia", "AF": "africa", "ZA": "africa"}


def landmass_of(a: dict) -> str:
    lm = ISLAND_LANDMASS.get(a["iata"])
    if lm and lm != "mainland":
        return lm
    return _CONTINENT.get(region_of(a["lat"], a["lng"]), "other")


# --------------------------------------------------------------------------- open water
# The landmass labels above catch islands, but not two mainland airports separated by a gulf
# (Helsinki-Tallinn, Bari-Patras) — straight-line "train" legs across open sea were this
# engine's worst fiction. data/landgrid.json is a 0.25-degree land/water bitmap (Natural Earth
# 1:50m, public domain, tools/build_land_grid.py) sampled along the great-circle path. Coastal
# cells are deliberately land-biased in the grid build, so only genuinely open water registers.
WATER_RUN_MIN_KM = 22.0     # longest contiguous open-water stretch that blocks a land leg
WATER_RESCUE_RUN_KM = 11.0  # an offset path only rescues if it is nearly water-free
WATER_OFFSET_KM = 40.0      # coast-hugging rescue: parallel paths this far to either side
FIXED_LINK_NEAR_KM = 120.0  # a bridge/tunnel within this of the water gap keeps the land leg

# Real bridges/tunnels that carry road or rail across water the grid sees as a gap. Each is a
# fact you can check on a map; the rescue rule is "a fixed link sits near the blocked water".
FIXED_LINKS = [
    ("Channel Tunnel", 51.01, 1.50),
    ("Oresund Bridge", 55.57, 12.85),
    ("Great Belt Bridge", 55.34, 11.03),
    ("Seikan Tunnel", 41.30, 140.30),
    ("Osman Gazi Bridge", 40.75, 29.51),
    ("Canakkale 1915 Bridge", 40.31, 26.45),
    ("HK-Zhuhai-Macau Bridge", 22.30, 113.75),
    ("Overseas Highway (Florida Keys)", 24.75, -81.02),
    ("Chesapeake Bay Bridge-Tunnel", 37.03, -76.08),
    ("Confederation Bridge (PEI)", 46.22, -63.75),
    ("King Fahd Causeway", 26.18, 50.26),
    ("Penang Bridge", 5.35, 100.35),
    ("Seto-Ohashi Bridge", 34.40, 133.81),
    ("Akashi Kaikyo Bridge", 34.62, 135.02),
    ("Rio-Niteroi Bridge", -22.87, -43.16),
]
# Deliberately NOT listed: bridges over straits too narrow for the grid to flag anyway
# (Oresund is listed only because the Great Belt pairs need it; Oland, Pontchartrain and
# similar never trigger) — every extra entry is a chance to falsely rescue a big crossing
# whose midpoint happens to fall nearby, which is exactly how an Oland entry once "bridged"
# Gdansk to Stockholm.


def is_land(lat: float, lng: float) -> bool:
    g = _landgrid()
    row = min(g["h"] - 1, max(0, int(math.floor((90.0 - lat) / g["res"]))))
    col = min(g["w"] - 1, max(0, int(math.floor((lng + 180.0) / g["res"]))))
    i = row * g["w"] + col
    return bool(g["bits"][i >> 3] & (0x80 >> (i & 7)))


def _path_points(lat1, lng1, lat2, lng2, n: int) -> list:
    """n+1 points along the great circle, spherical interpolation. Every point is rounded to
    4 decimals (~11 m) before use so the JS port's trig can't disagree at a grid-cell edge."""
    phi1, lam1, phi2, lam2 = (math.radians(v) for v in (lat1, lng1, lat2, lng2))
    v1 = (math.cos(phi1) * math.cos(lam1), math.cos(phi1) * math.sin(lam1), math.sin(phi1))
    v2 = (math.cos(phi2) * math.cos(lam2), math.cos(phi2) * math.sin(lam2), math.sin(phi2))
    dot = max(-1.0, min(1.0, v1[0] * v2[0] + v1[1] * v2[1] + v1[2] * v2[2]))
    om = math.acos(dot)
    pts = []
    for k in range(n + 1):
        t = k / n
        if om < 1e-9:
            a, b = 1 - t, t
        else:
            a = math.sin((1 - t) * om) / math.sin(om)
            b = math.sin(t * om) / math.sin(om)
        x, y, z = a * v1[0] + b * v2[0], a * v1[1] + b * v2[1], a * v1[2] + b * v2[2]
        pts.append((round(math.degrees(math.atan2(z, math.sqrt(x * x + y * y))), 4),
                    round(math.degrees(math.atan2(y, x)), 4)))
    return pts


def water_path_stats(lat1, lng1, lat2, lng2) -> dict:
    """Sample the great-circle path against the land grid. Returns the water fraction, the
    longest contiguous open-water run in km, and that run's midpoint (for the fixed-link test)."""
    d = haversine_km(lat1, lng1, lat2, lng2)
    n = max(8, min(96, int(d // 12) + 1))
    pts = _path_points(lat1, lng1, lat2, lng2, n)
    step = d / n
    frac_n = 0
    run = best = 0.0
    run_start = None
    mid = None
    for i, (la, ln) in enumerate(pts):
        if is_land(la, ln):
            run = 0.0
            continue
        frac_n += 1
        if run == 0.0:
            run_start = i
        run += step
        if run > best:
            best = run
            mid = pts[(run_start + i) // 2]
    return {"water_frac": round(frac_n / len(pts), 4), "max_run_km": round(best, 1),
            "run_mid": mid, "dist_km": d}


def _bearing_rad(lat1, lng1, lat2, lng2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    return math.atan2(math.sin(dl) * math.cos(p2),
                      math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))


def _offset_point(lat, lng, theta_rad, dist_km) -> tuple:
    """Destination point `dist_km` away on bearing theta — used to shift a path sideways."""
    d = dist_km / 6371.0
    p1, l1 = math.radians(lat), math.radians(lng)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(theta_rad))
    l2 = l1 + math.atan2(math.sin(theta_rad) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    lng2 = (math.degrees(l2) + 540.0) % 360.0 - 180.0
    return round(math.degrees(p2), 4), round(lng2, 4)


def sea_gap(a: dict, b: dict) -> bool:
    """True when open sea genuinely separates two points and no land detour plausibly exists.

    Three chances to stay a land leg, each anchored in something real:
      1. the direct path's longest open-water run is short (bridged straits, river mouths);
      2. a path shifted WATER_OFFSET_KM to either side is nearly water-free — the chord just
         cut across a bay the real road hugs (Barcelona-Valencia, Auckland-Wellington);
      3. a known FIXED_LINK (bridge/tunnel) sits near the blocked water (Channel Tunnel).
    Only when all three fail is the pair truly sea-separated. The rescue threshold is
    deliberately tighter than the trigger — a parallel path that still crosses the gulf a
    little further along is not a detour. Thresholds calibrated against a corpus of real
    pairs — see selftest()."""
    stats = water_path_stats(a["lat"], a["lng"], b["lat"], b["lng"])
    if stats["max_run_km"] < WATER_RUN_MIN_KM:
        return False
    theta = _bearing_rad(a["lat"], a["lng"], b["lat"], b["lng"])
    for side in (1.0, -1.0):
        o1 = _offset_point(a["lat"], a["lng"], theta + side * math.pi / 2, WATER_OFFSET_KM)
        o2 = _offset_point(b["lat"], b["lng"], theta + side * math.pi / 2, WATER_OFFSET_KM)
        if water_path_stats(o1[0], o1[1], o2[0], o2[1])["max_run_km"] < WATER_RESCUE_RUN_KM:
            return False
    mid = stats["run_mid"]
    if mid:
        for _name, llat, llng in FIXED_LINKS:
            if haversine_km(mid[0], mid[1], llat, llng) <= FIXED_LINK_NEAR_KM:
                return False
    return True


# --------------------------------------------------------------------------- ferry corridors
PORT_MATCH_KM = 60.0        # a corridor port must sit this close to the airport it serves
CROSSING_DOMINANT = 0.5     # crossing >= this fraction of the leg: the ferry IS the connection
MIN_FERRY_FREQ_PER_DAY = 0.65  # ~5 sailings/week is plannable; a twice-a-week boat isn't a
                               # dependable same-day gateway leg and never qualifies
FERRY_BOARDING_H = 0.5      # be-at-the-port buffer, on top of the airport->port transfer


ACCESS_WATER_RUN_KM = 15.0  # airport->port transfer may not itself cross open water

_PORT_LANDMASS_CACHE: dict = {}


def _port_landmass(port: dict) -> str:
    """A port's landmass, inferred from its nearest airport's label. The grid can't resolve
    narrow straits (they're deliberately land-biased), so this is what stops an island airport
    from "bussing" to a port on the far shore. Cached — a port's landmass never changes."""
    key = (port["lat"], port["lng"])
    lm = _PORT_LANDMASS_CACHE.get(key)
    if lm is None:
        near = nearest_airport(port["lat"], port["lng"], max_km=None)
        lm = landmass_of(near) if near else "other"
        _PORT_LANDMASS_CACHE[key] = lm
    return lm


def _access_ok(airport: dict, port: dict) -> bool:
    """The overland transfer from an airport to its matched port must actually be overland —
    without this, an island airport (Friday Harbor) "matched" a port on the far shore of the
    strait and the engine priced a bus ride across open water. Two independent guards: the
    port must be on the airport's own landmass, and the path to it must not cross open sea."""
    if haversine_km(airport["lat"], airport["lng"], port["lat"], port["lng"]) < 8:
        return True    # walking distance; grid resolution is coarser than a harbor
    if _port_landmass(port) != landmass_of(airport):
        return False
    stats = water_path_stats(airport["lat"], airport["lng"], port["lat"], port["lng"])
    return stats["max_run_km"] < ACCESS_WATER_RUN_KM


def ferry_corridor_for(a: dict, b: dict, max_port_km: float = PORT_MATCH_KM) -> dict | None:
    """Best real ferry corridor connecting the areas around two airports, either orientation.
    Returns the corridor plus which port serves which side and the access/crossing distances,
    or None — in which case there is no boat and the engine must not invent one."""
    best = None
    for c in ferry_corridors():
        pa, pb = c["port_a"], c["port_b"]
        for a_port, b_port in ((pa, pb), (pb, pa)):
            a_km = haversine_km(a["lat"], a["lng"], a_port["lat"], a_port["lng"])
            if a_km > max_port_km:
                continue
            b_km = haversine_km(b["lat"], b["lng"], b_port["lat"], b_port["lng"])
            if b_km > max_port_km:
                continue
            if not (_access_ok(a, a_port) and _access_ok(b, b_port)):
                continue
            score = a_km + b_km
            if best is None or score < best["_score"]:
                best = {**c, "a_port": a_port, "b_port": b_port,
                        "a_access_km": a_km, "b_access_km": b_km,
                        "crossing_km": haversine_km(a_port["lat"], a_port["lng"],
                                                    b_port["lat"], b_port["lng"]),
                        "_score": score}
    if best:
        best.pop("_score", None)
    return best


def ferry_leg_from_corridor(match: dict, region: str) -> dict:
    """Cost/time for a gateway ferry leg built from a REAL corridor: a transit estimate for the
    airport->port transfer, then the corridor's published crossing time and real fare floor.
    The fare is the only non-estimate number in the ground table — flagged so provenance can
    say which part is real."""
    access = estimate_ground(match["a_access_km"], "bus", region)
    fare_lo = match.get("price_usd_lo")
    fare = float(fare_lo) if fare_lo is not None else estimate_ground(
        match["crossing_km"], "ferry", region)["cost"]
    hours = access["hours"] + FERRY_BOARDING_H + float(match["duration_h"])
    return {
        "cost": round(access["cost"] + fare), "hours": round(hours, 1),
        "access_cost": access["cost"], "access_hours": access["hours"],
        "fare_usd": round(fare), "fare_is_real": fare_lo is not None,
        "crossing_km": round(match["crossing_km"], 1),
    }


def _ferry_note(match: dict) -> str:
    ops = ", ".join(match.get("operators") or []) or "operator n/a"
    freq = match.get("frequency_per_day")
    freq_s = f"~{freq:g}/day" if freq else "frequency n/a"
    seas = ", seasonal" if match.get("seasonal") else ""
    return (f"real ferry: {match['a_port']['name']} → {match['b_port']['name']} "
            f"({ops}), ~{match['duration_h']:g}h crossing, {freq_s}{seas}")


# --------------------------------------------------------------------------- gateway discovery
def curated_gateways(dest_iata: str) -> list[dict]:
    out = []
    for region, entries in gateways_db().items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if str(e.get("dest_airport", "")).upper() != (dest_iata or "").upper():
                continue
            for g in e["gateways"]:
                a = by_iata(g["hub_airport"])
                if not a:
                    continue
                out.append({
                    "iata": a["iata"], "name": a["name"], "city": a.get("city"),
                    "lat": a["lat"], "lng": a["lng"],
                    "hub": a["hub"], "ground_mode": g["ground_mode"],
                    "ground_hours": float(g["ground_time_h"]),
                    "ground_cost": float(g["ground_cost_usd"]),
                    "notes": g.get("notes", ""), "source": "curated",
                })
    return out


def discover_gateways(dest: dict, origin: dict | None = None, max_ground_h: float = 6.0,
                      max_gateways: int = 4) -> list[dict]:
    """
    Candidate 'fly into a cheaper hub, then ground it' airports for a destination.
    Curated pairs first (best data), then auto-discovered from the airport DB: any better-
    connected airport within ground range whose ground leg fits under max_ground_h.
    """
    result = curated_gateways(dest["iata"])
    seen = {g["iata"] for g in result}

    # a gateway must be materially better-connected than the destination to be worth it.
    # dest hub3 -> gateways of hub<=2 ; dest hub2 -> hub 1 ; dest hub1 -> (rarely helps) none.
    max_gw_hub = {1: 0, 2: 1, 3: 2}.get(dest["hub"], 0)

    region = region_of(dest["lat"], dest["lng"])
    max_km = 700 if region in ("EU", "JP", "CN", "KR") else 500   # HSR regions ground farther
    lm_dest = landmass_of(dest)

    cands = []
    for a in airports():
        if a["iata"] == dest["iata"] or a["iata"] in seen:
            continue
        if origin and a["iata"] == origin["iata"]:
            continue
        if a["hub"] > max_gw_hub:
            continue
        d = haversine_km(dest["lat"], dest["lng"], a["lat"], a["lng"])
        if d < 25 or d > max_km:       # too close to differ, or too far to "train the rest"
            continue

        # Water honesty, three rules. The engine never invents a boat and never runs a train
        # over open sea:
        #   1. a real ferry corridor spanning most of the leg IS the connection (Helsinki-
        #      Tallinn) — real fare, real crossing time, regardless of landmass labels;
        #   2. different landmasses with no corridor -> no leg at all (there is no Oahu-Maui
        #      ferry, however close the islands are);
        #   3. same landmass but open sea on the path with no land detour (sea_gap) -> no leg
        #      (no train from Turku to Tallinn).
        ferry = None
        corridor = ferry_corridor_for(a, dest)
        usable = (corridor is not None
                  and (corridor.get("frequency_per_day") or 0) >= MIN_FERRY_FREQ_PER_DAY)
        if usable and corridor["crossing_km"] >= CROSSING_DOMINANT * d:
            ferry = corridor
        elif landmass_of(a) != lm_dest:
            if not usable:
                continue
            ferry = corridor
        elif sea_gap(a, dest):
            continue

        if ferry:
            mode = "ferry"
            leg = ferry_leg_from_corridor(ferry, region)
            g = {"cost": leg["cost"], "hours": leg["hours"]}
            if g["hours"] > max_ground_h:
                continue
            cands.append({
                "iata": a["iata"], "name": a["name"], "city": a.get("city"),
                "lat": a["lat"], "lng": a["lng"],
                "hub": a["hub"], "ground_mode": mode, "ground_hours": g["hours"],
                "ground_cost": g["cost"], "notes": _ferry_note(ferry), "source": "auto",
                "ferry": {
                    "id": ferry["id"], "name": ferry["name"],
                    "operators": ferry.get("operators") or [],
                    "duration_h": ferry["duration_h"],
                    "frequency_per_day": ferry.get("frequency_per_day"),
                    "seasonal": bool(ferry.get("seasonal")),
                    "price_usd_lo": ferry.get("price_usd_lo"),
                    "price_usd_hi": ferry.get("price_usd_hi"),
                    "price_asof": ferry.get("price_asof"),
                    "port_a": ferry["a_port"]["name"], "port_b": ferry["b_port"]["name"],
                    "crossing_km": leg["crossing_km"], "fare_usd": leg["fare_usd"],
                    "fare_is_real": leg["fare_is_real"],
                    "access_cost": leg["access_cost"], "access_hours": leg["access_hours"],
                },
                "_dist": d,
            })
            continue

        mode = pick_ground_mode(d, region)
        g = estimate_ground(d, mode, region)
        if g["hours"] > max_ground_h:
            continue
        cands.append({
            "iata": a["iata"], "name": a["name"], "city": a.get("city"),
            "lat": a["lat"], "lng": a["lng"],
            "hub": a["hub"], "ground_mode": mode, "ground_hours": g["hours"],
            "ground_cost": g["cost"], "notes": f"auto: ~{int(d)}km {mode}", "source": "auto",
            "_dist": d,
        })
    cands.sort(key=lambda c: (c["hub"], c["_dist"]))   # best-connected & closest first
    for c in cands:
        if len(result) >= max_gateways:
            break
        c.pop("_dist", None)
        result.append(c)
    return result[:max_gateways]


# --------------------------------------------------------------------------- self-test
def selftest():
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    # haversine: JFK<->LAX is ~3970 km
    d = haversine_km(40.641, -73.778, 33.942, -118.408)
    check(f"haversine JFK-LAX ~3970km (got {d})", 3900 <= d <= 4050)

    # nearest airport to downtown Aspen (~39.19,-106.82) is ASE
    na = nearest_airport(39.19, -106.82)
    check(f"nearest to Aspen center is ASE (got {na['iata']})", na["iata"] == "ASE")

    # nearest to central London snaps sensibly to a London airport
    lon = nearest_airport(51.507, -0.128, prefer_hub=True)
    check(f"nearest to London is a London field (got {lon['iata']})",
          lon["iata"] in {"LHR", "LCY", "LGW", "STN", "LTN"})

    # nearest to central Paris must land on CDG/ORY (scheduled airline service), not Le
    # Bourget (LBG — business aviation only; used to carry hub tier 1, outranking the real
    # commercial fields under prefer_hub's scoring).
    par = nearest_airport(48.86, 2.35, prefer_hub=True)
    check(f"nearest to Paris is CDG/ORY, not LBG (got {par['iata']})",
          par["iata"] in {"CDG", "ORY"})

    # general-aviation/business-aviation fields with no scheduled airline service must never
    # outrank a real commercial airport under prefer_hub — confirm each was actually demoted.
    for code in ("LBG", "TEB", "PDK", "FTW", "OPF", "BED"):
        a = by_iata(code)
        check(f"{code} is tiered as no-scheduled-service (hub 3), not a real hub",
              a is not None and a["hub"] == 3)

    # flight estimate JFK->ASE should carry a small-airport premium and beat JFK->DEN's raw base
    jfk, ase, den = by_iata("JFK"), by_iata("ASE"), by_iata("DEN")
    fa, fd = estimate_flight(jfk, ase), estimate_flight(jfk, den)
    check(f"ASE estimate > DEN estimate (ASE {fa['price']} vs DEN {fd['price']})",
          fa["price"] > fd["price"])
    check("flight estimate returns hours>0", fa["hours"] > 0)

    # gateway discovery for Aspen includes curated DEN + EGE
    gws = discover_gateways(ase, origin=jfk, max_ground_h=8)
    codes = {g["iata"] for g in gws}
    check(f"Aspen gateways include DEN & EGE (got {sorted(codes)})", {"DEN", "EGE"} <= codes)
    check("gateways carry ground_mode/hours/cost",
          all("ground_mode" in g and g["ground_hours"] > 0 for g in gws))
    # itinerary.py's leg labels need a full airport name+city for every gateway, curated and
    # auto alike — not just the bare code discover_gateways used to leave off "city" for.
    check("every gateway (curated and auto) carries a city, for itinerary display",
          all("city" in g and g["city"] for g in gws))

    # a major hub (DEN) as destination yields few/no gateways (already cheap & connected)
    dg = discover_gateways(den, origin=jfk)
    check(f"major-hub destination yields no split gateways (got {len(dg)})", len(dg) == 0)

    # ground estimate monotonic in distance
    g1, g2 = estimate_ground(50, "train"), estimate_ground(300, "train")
    check("ground cost/time grow with distance", g2["cost"] > g1["cost"] and g2["hours"] > g1["hours"])

    # fare curve is calibrated: JFK-LAX cheapest one-way lands in a realistic band
    lax = by_iata("LAX")
    fx = estimate_flight(jfk, lax)
    check(f"JFK-LAX estimate ${fx['price']} in $120-$260 band", 120 <= fx["price"] <= 260)

    # date awareness: tomorrow >> six weeks out; deterministic via fixed `today`
    t0 = datetime.date(2026, 7, 1)
    close_in = fare_date_multiplier("2026-07-02", today=t0)
    sweet = fare_date_multiplier("2026-08-12", today=t0)
    check(f"walk-up fare mult ({close_in}) > 6-weeks-out ({sweet})", close_in > sweet)
    check("no/invalid date -> neutral 1.0",
          fare_date_multiplier(None) == 1.0 and fare_date_multiplier("not-a-date") == 1.0)
    fd = estimate_flight(jfk, lax, date="2026-07-02", today=t0)
    check("close-in dated estimate prices above undated", fd["price"] > fx["price"])

    # a past date gets flagged rather than silently treated as a normal neutral-multiplier
    # date — fare_date_multiplier still returns 1.0 (no honest curve for a date that's
    # already gone), but estimate_flight's output says so explicitly
    check("past date -> neutral multiplier but still flagged",
          fare_date_multiplier("2026-06-01", today=t0) == 1.0
          and is_past_date("2026-06-01", today=t0)
          and not is_past_date("2026-07-02", today=t0)
          and not is_past_date(None, today=t0))
    f_past = estimate_flight(jfk, lax, date="2026-06-01", today=t0)
    check("estimate_flight surfaces past_date instead of pricing it like a real future date",
          f_past.get("past_date") is True and "past_date" not in fx)

    # regions: Paris EU, Denver NA, Tokyo JP; EU rail beats NA rail on speed
    check("region boxes (Paris/Denver/Tokyo)",
          region_of(48.86, 2.35) == "EU" and region_of(39.74, -104.99) == "NA"
          and region_of(35.68, 139.77) == "JP")
    eu_t, na_t = estimate_ground(300, "train", "EU"), estimate_ground(300, "train", "NA")
    check("EU train faster than NA train over 300km", eu_t["hours"] < na_t["hours"])

    # region edge cases around the Mediterranean + Korea/Japan + southern Africa
    check("Malaga/Malta/Crete/Cyprus are EU",
          region_of(36.67, -4.49) == "EU" and region_of(35.86, 14.48) == "EU"
          and region_of(35.34, 25.18) == "EU" and region_of(34.88, 33.62) == "EU")
    check("Marrakech + Cairo are AF; Tel Aviv is ME",
          region_of(31.61, -8.04) == "AF" and region_of(30.12, 31.41) == "AF"
          and region_of(32.01, 34.89) == "ME")
    check("Seoul/Jeju are KR; Fukuoka is JP",
          region_of(37.56, 126.79) == "KR" and region_of(33.51, 126.49) == "KR"
          and region_of(33.59, 130.45) == "JP")
    check("Johannesburg is ZA; Nairobi is AF; Tenerife is EU",
          region_of(-26.13, 28.23) == "ZA" and region_of(-1.32, 36.93) == "AF"
          and region_of(28.04, -16.57) == "EU")

    # regression: the Tierra del Fuego box used to read "-44 <= lat <= -59", which can never
    # be true (lat can't be both >= -44 and <= -59) — an always-false condition that silently
    # fell through to the later, wider LATAM box. Harmless in that one case (same answer either
    # way), but worth a direct check since it's exactly the kind of dead conditional that's
    # easy to reintroduce.
    check("Cape Horn / Tierra del Fuego resolves to LATAM (regression: unsatisfiable bounds)",
          region_of(-57.0, -65.0) == "LATAM")

    # route-market calibration: same curve, different competition
    bcn, fco2 = by_iata("BCN"), by_iata("FCO")
    eu_pair = estimate_flight(bcn, fco2)
    check(f"intra-EU BCN-FCO ${eu_pair['price']} in LCC band $40-$95",
          40 <= eu_pair["price"] <= 95)
    ord_, den2 = by_iata("ORD"), by_iata("DEN")
    us_pair = estimate_flight(ord_, den2)
    check(f"US ORD-DEN ${us_pair['price']} in $85-$160 band", 85 <= us_pair["price"] <= 160)
    lhr = by_iata("LHR")
    tatl = estimate_flight(jfk, lhr)
    check(f"transatlantic JFK-LHR ${tatl['price']} in $150-$260 band",
          150 <= tatl["price"] <= 260)
    acc, los2 = by_iata("ACC"), by_iata("LOS")
    af_pair = estimate_flight(acc, los2)
    vce2, fco3 = by_iata("VCE"), by_iata("FCO")
    eu_short = estimate_flight(vce2, fco3)
    check(f"thin-competition AF pair (ACC-LOS ${af_pair['price']}) prices well above a "
          f"same-scale EU pair (VCE-FCO ${eu_short['price']})",
          af_pair["price"] >= 1.5 * eu_short["price"])
    lga, bos2 = by_iata("LGA"), by_iata("BOS")
    na_short = estimate_flight(lga, bos2)
    check(f"NA short-hop floor: LGA-BOS ${na_short['price']} >= $65", na_short["price"] >= 65)

    # connection realism: far-away tiny field carries a likely-connection time bump
    f_ase = estimate_flight(jfk, ase)
    f_den = estimate_flight(jfk, den)
    check("JFK->ASE flags a likely connection (+time) vs JFK->DEN nonstop",
          f_ase["likely_connection"] and not f_den["likely_connection"]
          and f_ase["hours"] > f_den["hours"] + 1.0)

    # water awareness: no trains across the Aegean / Cook Strait / Korea Strait
    jtr = by_iata("JTR")
    jtr_gws = discover_gateways(jtr, origin=by_iata("LHR"), max_ground_h=9)
    check("Santorini auto-gateways never propose land modes (ferry/curated only)",
          all(g["ground_mode"] in ("ferry", "bus", "shuttle") or g["source"] == "curated"
              for g in jtr_gws)
          and all(g["ground_mode"] == "ferry" for g in jtr_gws if g["source"] == "auto"))
    check("Istanbul is not a 'ground' gateway to Santorini",
          not any(g["iata"] in ("IST", "SAW") for g in jtr_gws))
    her_gw = next((g for g in jtr_gws if g["iata"] == "HER"), None)
    check("Santorini's Heraklion gateway rides the REAL corridor (SeaJets-class ~1.9h "
          "crossing + port transfer, real fare), not the old 40km/h formula that said 3.9h",
          her_gw is not None and her_gw["ferry"]["fare_is_real"]
          and abs(her_gw["ferry"]["duration_h"] - 1.9) < 0.5
          and her_gw["ground_hours"] < 3.5 and her_gw["ferry"]["price_usd_lo"] >= 40)
    check("Santorini never offers a twice-a-week-boat 'gateway' (Karpathos/Sitia dropped)",
          not any(g["iata"] in ("AOK", "JSH") for g in jtr_gws))
    cju = by_iata("CJU")
    cju_gws = discover_gateways(cju, origin=by_iata("ICN"))
    check(f"Jeju's mainland-Korea gateways are ferry-only, never a land mode (got "
          f"{[(g['iata'], g['ground_mode']) for g in cju_gws]})",
          all(g["ground_mode"] == "ferry" for g in cju_gws
              if landmass_of(by_iata(g["iata"])) == "korea"))
    yyj = by_iata("YYJ")
    yyj_gws = discover_gateways(yyj, origin=by_iata("JFK"), max_ground_h=8)
    check("Vancouver Island gateways from the mainland are ferries",
          all(g["ground_mode"] == "ferry" for g in yyj_gws
              if g["source"] == "auto" and landmass_of(by_iata(g["iata"])) == "americas"))
    check("landmass labels: HER=crete, DUB=ireland, LHR=eurasia, JFK=americas",
          landmass_of(by_iata("HER")) == "crete" and landmass_of(by_iata("DUB")) == "ireland"
          and landmass_of(by_iata("LHR")) == "eurasia"
          and landmass_of(by_iata("JFK")) == "americas")

    # ---- the land grid + sea_gap: no land legs across open sea, no false flags on real
    # roads/bridges/tunnels. Each case is a real-world pair the thresholds were calibrated on.
    check("land grid: Kansas land, mid-Atlantic water, Gulf of Finland water, Crete land",
          is_land(38.5, -98.0) and not is_land(30.0, -40.0)
          and not is_land(59.85, 25.1) and is_land(35.2, 24.9))
    gap_corpus = [
        # (origin, dest, expect_gap, why)
        ("TKU", "TLL", True, "Turku-Tallinn: no direct ferry, open Baltic on every path"),
        ("LPP", "TLL", True, "Lappeenranta-Tallinn: Gulf of Finland"),
        ("OUL", "TLL", True, "Oulu-Tallinn: gulf crossing regardless of the long land run-up"),
        ("ATH", "ADB", True, "Athens-Izmir: the Aegean; the land route is 3-4x the chord"),
        ("ARN", "RIX", True, "Stockholm-Riga: open Baltic (the ferry stopped in 2020)"),
        ("GDN", "NYO", True, "Gdansk-Stockholm: open Baltic"),
        ("DOH", "DXB", True, "Doha-Dubai: Persian Gulf chord"),
        ("BCN", "VLC", False, "Barcelona-Valencia: chord clips the sea, the coast road is real"),
        ("AKL", "WLG", False, "Auckland-Wellington: SH1 hugs the coast the chord leaves"),
        ("ATH", "SKG", False, "Athens-Thessaloniki: real rail, gulf-clipping chord"),
        ("CPH", "AAR", False, "Copenhagen-Aarhus: Great Belt fixed link"),
        ("CPH", "GOT", False, "Copenhagen-Gothenburg: Oresund bridge"),
        ("LHR", "CDG", False, "London-Paris: Channel Tunnel"),
        ("IST", "ADB", False, "Istanbul-Izmir: road rounds the Marmara"),
        ("MIA", "EYW", False, "Miami-Key West: Overseas Highway"),
        ("SPU", "DBV", False, "Split-Dubrovnik: coastal road"),
        ("GOA", "NCE", False, "Genoa-Nice: coastal rail"),
        ("HEL", "TKU", False, "Helsinki-Turku: plain overland"),
    ]
    for o_c, d_c, expect_gap, why in gap_corpus:
        got = sea_gap(by_iata(o_c), by_iata(d_c))
        check(f"sea_gap {o_c}->{d_c} is {expect_gap} ({why})", got is expect_gap)

    # ---- ferry corridors: real boats only, with real numbers
    hel, tll = by_iata("HEL"), by_iata("TLL")
    m = ferry_corridor_for(hel, tll)
    check("Helsinki-Tallinn matches the real corridor with a real fare and sane crossing",
          m is not None and m["id"] == "helsinki-tallinn" and m["price_usd_lo"] >= 10
          and 1.5 <= m["duration_h"] <= 3.5 and 60 <= m["crossing_km"] <= 110)
    check("there is NO Honolulu-Maui ferry corridor (the engine must not invent one)",
          ferry_corridor_for(by_iata("HNL"), by_iata("OGG")) is None)
    check("an island airport can't 'bus' to a port across a strait (Friday Harbor vs "
          "Port Angeles rejected by the access guard)",
          not _access_ok(by_iata("FRD"), {"lat": 48.12, "lng": -123.44}))
    tll_gws = discover_gateways(tll, origin=by_iata("JFK"))
    hel_gw = next((g for g in tll_gws if g["iata"] == "HEL"), None)
    check("Tallinn's Helsinki gateway is the REAL ferry (real fare, ~2-3h door timeline), "
          "not a train across the gulf",
          hel_gw is not None and hel_gw["ground_mode"] == "ferry"
          and hel_gw["ferry"]["fare_is_real"] and hel_gw["ferry"]["price_usd_lo"] >= 10
          and 2.0 <= hel_gw["ground_hours"] <= 5.0)
    check("no Baltic train fictions to Tallinn (Turku/Lappeenranta/Mariehamn/Vaasa/Oulu all "
          "dropped — sea gap, no corridor)",
          not any(g["iata"] in ("TKU", "LPP", "MHQ", "VAA", "OUL") for g in tll_gws))
    ogg_gws = discover_gateways(by_iata("OGG"), origin=by_iata("LAX"))
    check("Maui gets NO Honolulu gateway at all — the inter-island ferry shut down in 2009 "
          "and the engine no longer pretends otherwise",
          not any(g["iata"] == "HNL" for g in ogg_gws))
    ferry_gws = [g for g in tll_gws + jtr_gws + yyj_gws if g.get("ferry")]
    check("every auto ferry gateway names its ports, operators and sailings/day in notes",
          ferry_gws and all(g["ferry"]["operators"] and g["ferry"]["port_a"]
                            and (g["ferry"]["frequency_per_day"] or 0) >= MIN_FERRY_FREQ_PER_DAY
                            and "real ferry" in g["notes"] for g in ferry_gws))

    # ROUTE_MULT regression guard: CN-EU and AF-EU used to be dead/inverted (unsorted keys
    # that _route_mult's sorted lookup could never hit) — confirm both directions now agree
    # with each other and with the module-load assertion that keeps them sorted
    check("ROUTE_MULT lookup is direction-independent for CN-EU and AF-EU",
          _route_mult("EU", "CN") == _route_mult("CN", "EU") == 0.90
          and _route_mult("EU", "AF") == _route_mult("AF", "EU") == 0.90)
    check("ROUTE_MULT keys are all pre-sorted (regression guard also enforced at import time)",
          all(k == tuple(sorted(k)) for k in ROUTE_MULT))

    # nearest_airport cap: a click deep in Mongolia used to silently resolve to Beijing,
    # ~1163km away, with no warning — it must now either land on a real nearby field (thanks
    # to the OurAirports floor) or be refused outright by the hard cap, never silently
    # return something implausibly far with no signal
    mongolia_hit = nearest_airport(47.9, 106.9)
    check(f"Mongolia click resolves near, not to a far-off country (got "
          f"{mongolia_hit and mongolia_hit['iata']} at "
          f"{mongolia_hit and mongolia_hit['dist_km']}km)",
          mongolia_hit is not None and mongolia_hit["dist_km"] < NEAREST_WARN_KM)
    check("nearest_airport tags warn_tier None for a close hit",
          nearest_airport(51.507, -0.128)["warn_tier"] is None)
    remote_ocean = nearest_airport(-45.0, -100.0, max_km=50)
    check(f"nearest_airport hard-caps: a mid-ocean click within a tight max_km finds nothing "
          f"(got {remote_ocean})", remote_ocean is None)
    far_but_capped = nearest_airport(-45.0, -100.0)
    check("default max_km still refuses an airport far beyond the hard cap",
          far_but_capped is None or far_but_capped["dist_km"] <= NEAREST_HARD_KM)

    # region_of gaps: Iceland/Faroes/Greenland, Russia/Central Asia, Pacific islands, and
    # the Vladivostok-tagged-as-Japan bug are all closed
    check("Iceland, Faroes, Greenland resolve to EU (nearest real fare/ground market)",
          region_of(64.13, -21.9) == "EU" and region_of(62.0, -6.79) == "EU"
          and region_of(64.18, -51.69) == "EU")
    check("Vladivostok is RU, not JP; Moscow and Siberia are RU",
          region_of(43.12, 131.9) == "RU" and region_of(55.75, 37.62) == "RU"
          and region_of(55.03, 82.92) == "RU")
    check("Kazakhstan/Kyrgyzstan/Mongolia are RU, not silently OTHER or mis-tagged CN",
          region_of(43.24, 76.9) == "RU" and region_of(42.87, 74.59) == "RU"
          and region_of(47.9, 106.9) == "RU")
    check("Helsinki/Tallinn stay EU despite sitting next to the new RU box",
          region_of(60.17, 24.94) == "EU" and region_of(59.44, 24.75) == "EU")
    check("Pacific islands (Fiji, Tahiti, Guam, Hawaii) no longer fall into OTHER",
          region_of(-17.75, 177.44) != "OTHER" and region_of(-17.65, -149.6) != "OTHER"
          and region_of(13.48, 144.79) != "OTHER" and region_of(21.31, -157.86) == "NA")

    # pick_ground_mode smoothing: a small step near a distance breakpoint must not swing
    # cost or time by an amount bigger than a same-mode step would — the old hard cliffs
    # (drive/train at 80km EU, drive/bus at 250km NA) could flip a recommendation on 2km
    for region, lo, hi in (("EU", 78, 82), ("NA", 248, 252)):
        g_lo = estimate_ground(lo, pick_ground_mode(lo, region), region)
        g_hi = estimate_ground(hi, pick_ground_mode(hi, region), region)
        check(f"{region} ground estimate near its old cliff ({lo}-{hi}km) has no runaway "
              f"jump (cost {g_lo['cost']}->{g_hi['cost']}, hours {g_lo['hours']}->{g_hi['hours']})",
              abs(g_hi["cost"] - g_lo["cost"]) <= 25 and abs(g_hi["hours"] - g_lo["hours"]) <= 1.0)
    check("pick_ground_mode picks the better-scoring mode near a boundary, not a fixed cliff",
          pick_ground_mode(65, "EU") == "train")  # EU train is cheap+fast enough to win early

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (geo checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print("geo.py — import me, or run with --selftest")
