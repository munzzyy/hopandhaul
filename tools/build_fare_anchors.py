#!/usr/bin/env python3
"""
build_fare_anchors.py — generates src/hopandhaul/data/fareanchors.json: REAL average fares
for the busiest contiguous-US city-pair markets, from the US DOT/BTS Consumer Airfare Report
(Table 6, Socrata dataset yj5y-b2ir — US government work, public domain, keyless SODA API).

Why this exists: the engine's US fare estimates were a calibrated curve — a reasonable model,
but a model. BTS publishes the actual average fare paid per city-pair market every quarter,
including the average fare of the lowest-fare carrier on the route (`fare_low`). Anchoring
the estimate to that turns "formula says $x" into "the real market average says $y" for the
routes that carry the vast majority of US domestic passengers.

Method: pull the latest four published quarters, passenger-weight each pair's fare/fare_low
across them (smooths seasonality — the engine's own date multiplier re-adds it), geocode each
BTS city market to a centroid of its matching airports in our own airports.json, and keep the
top pairs by passenger volume. Pairs whose city names can't be confidently matched to airport
cities are dropped and reported — never guessed.

Run:  python tools/build_fare_anchors.py           (writes src/hopandhaul/data/fareanchors.json)
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.normpath(os.path.join(HERE, "..", "src", "hopandhaul", "data"))
SODA = "https://data.transportation.gov/resource/yj5y-b2ir.json"
UA = "hopandhaul/0.7 (https://github.com/munzzyy/hopandhaul)"
MAX_ANCHORS = 1800          # top pairs by passenger volume — covers most US domestic traffic
MIN_PAX_PER_DAY = 10.0      # below ~10 pax/day the quarterly averages get noisy
EXTRA_FARE_LOW = 250.0      # ...but every expensive thin market is kept: that's where the
                            # distance curve misprices and a real number earns its keep
DIST_TOLERANCE = 0.20       # geocode validation: pair distance must match BTS nsmiles

# BTS metro names that don't literally match an airports.json city name.
CITY_ALIASES = {
    "dallas/fort worth": ["dallas", "fort worth", "dallas-fort worth"],
    "new york city": ["new york", "newark"],
    "washington": ["washington", "baltimore"],
    "chicago": ["chicago"],
    "los angeles": ["los angeles", "burbank", "santa ana", "ontario", "long beach"],
    "san francisco": ["san francisco", "oakland", "san jose"],
    "miami": ["miami", "fort lauderdale"],
    "houston": ["houston"],
    "tampa": ["tampa", "st petersburg", "st. petersburg"],
    "raleigh/durham": ["raleigh", "raleigh-durham", "durham"],
    "greensboro/high point": ["greensboro"],
    "minneapolis/st. paul": ["minneapolis", "st. paul", "saint paul"],
    "norfolk": ["norfolk", "newport news"],
    "phoenix": ["phoenix", "mesa"],
    "bristol/johnson city/kingsport": ["bristol"],
    "sarasota/bradenton": ["sarasota"],
    "harlingen/san benito": ["harlingen"],
    "montrose/delta": ["montrose"],
    "hilton head": ["hilton head island", "hilton head"],
    "gulfport/biloxi": ["gulfport"],
    "jacksonville/camp lejeune": ["jacksonville"],
    "beaumont/port arthur": ["beaumont"],
    "saginaw/bay city/midland": ["saginaw"],
    "champaign/urbana": ["champaign"],
    "mission/mcallen/edinburg": ["mcallen"],
    "cedar rapids/iowa city": ["cedar rapids"],
    "bloomington/normal": ["bloomington"],
    "elmira/corning": ["elmira"],
    "faro/block island": ["block island"],
    "college station/bryan": ["college station"],
    "lawton/fort sill": ["lawton"],
    "new bern/morehead/beaufort": ["new bern"],
    "hattiesburg/laurel": ["hattiesburg"],
    "pasco/kennewick/richland": ["pasco"],
    "scranton/wilkes-barre": ["scranton", "wilkes-barre"],
    "louisville": ["louisville"],
    "akron/canton": ["akron"],
    "allentown/bethlehem/easton": ["allentown"],
    "midland/odessa": ["midland"],
    "greenville/spartanburg": ["greenville"],
    "bend/redmond": ["redmond"],
    "panama city": ["panama city beach"],
    "bismarck/mandan": ["bismarck"],
    "ithaca/cortland": ["ithaca"],
    "eureka/arcata": ["arcata/eureka"],
    "jackson/vicksburg": ["jackson"],
    "jefferson city/columbia": ["columbia"],
    "killeen": ["killeen", "fort hood"],
    "gunnison/crested butte": ["gunnison"],
    "hilton head/beaufort": ["hilton head island"],
    "salinas/monterey": ["monterey"],
    "st. cloud": ["st cloud", "saint cloud"],
}


def fetch(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def latest_quarters(n=4) -> list[tuple[int, int]]:
    rows = fetch(SODA + "?" + urllib.parse.urlencode(
        {"$select": "year,quarter", "$group": "year,quarter",
         "$order": "year DESC,quarter DESC", "$limit": str(n)}))
    return [(int(r["year"]), int(r["quarter"])) for r in rows]


def fetch_quarter(year: int, quarter: int) -> list[dict]:
    out, offset = [], 0
    while True:
        q = {"$select": "city1,city2,nsmiles,passengers,fare,fare_low",
             "$where": f"year={year} AND quarter={quarter}",
             "$limit": "50000", "$offset": str(offset)}
        rows = fetch(SODA + "?" + urllib.parse.urlencode(q))
        out.extend(rows)
        if len(rows) < 50000:
            return out
        offset += 50000


def load_airport_cities() -> dict[str, list[tuple[float, float]]]:
    with open(os.path.join(DATA_DIR, "airports.json"), encoding="utf-8") as f:
        airports = json.load(f)["airports"]
    cities: dict[str, list[tuple[float, float]]] = {}
    for a in airports:
        if a.get("country") != "US" or not a.get("city"):
            continue
        cities.setdefault(a["city"].strip().lower(), []).append((a["lat"], a["lng"]))
    return cities


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, a ** 0.5))


def city_points(bts_city: str, cities: dict) -> list[tuple[float, float]]:
    """BTS 'City, ST (Metropolitan Area)' -> CANDIDATE centroids, one per geographic cluster
    of same-named airport cities. US city names repeat across states ('Portland', 'Jackson',
    'Columbus') and BTS's state suffix doesn't exist in our airport DB — so instead of guessing,
    every cluster is returned and the caller validates the pair against BTS's own published
    route distance (nsmiles). A wrong Portland can't survive that check."""
    name = re.sub(r"\s*\(Metropolitan Area\)\s*", "", bts_city).strip()
    name = name.rsplit(",", 1)[0].strip().lower()       # drop the state
    pts = []
    for c in CITY_ALIASES.get(name, [name]):
        pts.extend(cities.get(c, []))
    if not pts:
        return []
    clusters: list[list[tuple[float, float]]] = []
    for p in pts:
        for cl in clusters:
            if any(haversine_km(p[0], p[1], q[0], q[1]) <= 150 for q in cl):
                cl.append(p)
                break
        else:
            clusters.append([p])
    out = []
    for cl in clusters:
        lat = sum(p[0] for p in cl) / len(cl)
        lng = sum(p[1] for p in cl) / len(cl)
        out.append((round(lat, 2), round(lng, 2)))
    return out


def main() -> int:
    quarters = latest_quarters(4)
    print(f"latest published quarters: {quarters}")
    rows = []
    for y, q in quarters:
        got = fetch_quarter(y, q)
        print(f"  {y}Q{q}: {len(got)} market rows")
        rows.extend(got)

    # passenger-weighted aggregate per unordered city pair
    agg: dict[tuple[str, str], dict] = {}
    for r in rows:
        try:
            pax = float(r["passengers"])
            fare = float(r["fare"])
            fare_low = float(r["fare_low"])
            miles = float(r["nsmiles"])
        except (KeyError, ValueError):
            continue
        key = tuple(sorted((r["city1"], r["city2"])))
        a = agg.setdefault(key, {"pax": 0.0, "fare_w": 0.0, "low_w": 0.0, "miles": miles})
        a["pax"] += pax
        a["fare_w"] += fare * pax
        a["low_w"] += fare_low * pax

    cities = load_airport_cities()
    anchors, unmatched, dist_rejected = [], set(), 0
    unmatched_pax, total_pax = 0.0, 0.0
    for (c1, c2), a in agg.items():
        pax_day = a["pax"] / len(quarters)      # average daily passengers across the window
        total_pax += pax_day
        if pax_day < MIN_PAX_PER_DAY:
            continue
        pts1, pts2 = city_points(c1, cities), city_points(c2, cities)
        if not pts1 or not pts2:
            for c, p in ((c1, pts1), (c2, pts2)):
                if not p:
                    unmatched.add(c)
            unmatched_pax += pax_day
            continue
        # geocode validation against BTS's own route distance: keep the candidate pairing
        # whose great-circle distance matches nsmiles; ambiguous or no match -> drop.
        want_km = a["miles"] * 1.609344
        tol = max(30.0, want_km * DIST_TOLERANCE)
        good = [(p1, p2) for p1 in pts1 for p2 in pts2
                if abs(haversine_km(p1[0], p1[1], p2[0], p2[1]) - want_km) <= tol]
        if len(good) != 1:
            dist_rejected += 1
            unmatched_pax += pax_day
            continue
        p1, p2 = good[0]
        anchors.append({
            "a": list(p1), "b": list(p2),
            "fare_avg": round(a["fare_w"] / a["pax"]),
            "fare_low": round(a["low_w"] / a["pax"]),
            "pax_day": round(pax_day),
        })
    anchors.sort(key=lambda x: -x["pax_day"])
    # top markets by volume, PLUS every expensive thin market — high-fare monopoly routes are
    # exactly where the distance curve underprices and the real number earns its keep.
    kept = anchors[:MAX_ANCHORS]
    extra = [x for x in anchors[MAX_ANCHORS:] if x["fare_low"] >= EXTRA_FARE_LOW]
    kept = kept + extra
    print(f"high-fare thin markets added beyond the top {MAX_ANCHORS}: {len(extra)}; "
          f"pairs dropped by the distance check: {dist_rejected}")
    kept_pax = sum(x["pax_day"] for x in kept)
    print(f"pairs aggregated: {len(agg)}; matched anchors: {len(anchors)}; kept top {len(kept)}")
    print(f"passenger coverage of kept anchors: {100 * kept_pax / total_pax:.1f}% "
          f"(unmatched city names cost {100 * unmatched_pax / total_pax:.1f}%)")
    if unmatched:
        worst = sorted(unmatched)[:20]
        print(f"unmatched BTS city names ({len(unmatched)}): {worst}")

    qlabel = f"{quarters[-1][0]}Q{quarters[-1][1]}-{quarters[0][0]}Q{quarters[0][1]}"
    out = {
        "_README": ("REAL average fares for the busiest US city-pair markets, from the US "
                    "DOT/BTS Consumer Airfare Report Table 6 (public domain, dataset "
                    "yj5y-b2ir), passenger-weighted across the latest four published "
                    "quarters. fare_low = average fare of the lowest-fare carrier in the "
                    "market; fare_avg = market average; pax_day = average daily passengers. "
                    "a/b are city-market centroids from our own airport DB. Regenerate with "
                    "tools/build_fare_anchors.py — don't hand-edit."),
        "source": "US DOT/BTS Consumer Airfare Report Table 6 (data.transportation.gov yj5y-b2ir)",
        "asof": qlabel,
        "anchors": kept,
    }
    path = os.path.join(DATA_DIR, "fareanchors.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
        f.write("\n")
    print(f"wrote {path} ({os.path.getsize(path)} bytes, asof {qlabel})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
