#!/usr/bin/env python3
"""
build_land_grid.py — generates src/hopandhaul/data/landgrid.json: a packed 0.25-degree
land/water bitmap of Earth, rasterized from Natural Earth's 1:50m land polygons.

Why this exists: geo.py's gateway discovery proposes ground legs between airport pairs from
straight-line distance alone, which happily invented trains across the Gulf of Finland and
ferries between islands no boat serves. The engine needs to know when a leg's path crosses
open sea, so it can demand a real ferry corridor (data/ferries.json) or drop the leg — that
check has to work offline, in both the Python engine and the browser port, which means
bundled data, not a live API.

Source: Natural Earth 1:50m land (public domain, https://www.naturalearthdata.com/), fetched
as GeoJSON from the naturalearth/naturalearth-vector GitHub mirror. Run this script to
regenerate the grid; the output JSON is committed as package data.

Method: even-odd scanline rasterization of every polygon at each grid row's center latitude
(GeoJSON edges are straight lines in lat/lng space, same space as the grid), then every
polygon vertex's cell is additionally marked land so a small island or coastal sliver thinner
than a cell never silently disappears. A cell is land if its center falls inside land OR any
coastline vertex touches it — biased toward land, so the sea-gap detector only fires on
genuinely open water, never on a bridged strait or a river mouth.

Layout: row 0 = northernmost band (lat 90..90-res), col 0 = westernmost (lng -180..-180+res).
Bit (row*W + col), packed MSB-first per byte, base64-encoded into the JSON.

Run:  python tools/build_land_grid.py            (writes src/hopandhaul/data/landgrid.json)
      python tools/build_land_grid.py --check    (also prints a validation table of known
                                                  land/water points and famous crossings)
"""
from __future__ import annotations

import base64
import json
import math
import os
import sys
import urllib.request

RES_DEG = 0.25
W = int(round(360 / RES_DEG))          # 1440
H = int(round(180 / RES_DEG))          # 720
SOURCE_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
              "master/geojson/ne_50m_land.geojson")
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "src", "hopandhaul", "data", "landgrid.json")


def fetch_geojson(url: str = SOURCE_URL) -> dict:
    print(f"fetching {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "hopandhaul-landgrid-build"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    print(f"  {len(data.get('features', []))} features")
    return data


def _rings(feature) -> list[list[list[float]]]:
    geom = feature.get("geometry") or {}
    t, coords = geom.get("type"), geom.get("coordinates") or []
    if t == "Polygon":
        return list(coords)
    if t == "MultiPolygon":
        return [ring for poly in coords for ring in poly]
    return []


def _row_lat(row: int) -> float:
    return 90.0 - RES_DEG * (row + 0.5)


def _col_of_lng(lng: float) -> int:
    c = int(math.floor((lng + 180.0) / RES_DEG))
    return min(W - 1, max(0, c))


def _row_of_lat(lat: float) -> int:
    r = int(math.floor((90.0 - lat) / RES_DEG))
    return min(H - 1, max(0, r))


def rasterize(geojson: dict) -> bytearray:
    bits = bytearray(W * H // 8)

    def set_cell(row: int, col: int):
        i = row * W + col
        bits[i >> 3] |= 0x80 >> (i & 7)

    features = geojson.get("features", [])
    for fi, feat in enumerate(features):
        rings = _rings(feat)
        if not rings:
            continue
        # Bucket this feature's edges by the grid rows their lat-span crosses, so each row's
        # scanline only looks at edges that can actually intersect it.
        edges_by_row: dict[int, list[tuple[float, float, float, float]]] = {}
        for ring in rings:
            n = len(ring)
            for i in range(n - 1):        # GeoJSON rings repeat the first point at the end
                x1, y1 = ring[i][0], ring[i][1]
                x2, y2 = ring[i + 1][0], ring[i + 1][1]
                if y1 == y2:
                    continue               # horizontal edge: no scanline crossing
                lo, hi = (y1, y2) if y1 < y2 else (y2, y1)
                r_hi = _row_of_lat(lo)     # lower lat -> larger row index
                r_lo = _row_of_lat(hi)
                for row in range(r_lo, r_hi + 1):
                    y = _row_lat(row)
                    if lo <= y < hi:       # half-open: a vertex on the line counts once
                        edges_by_row.setdefault(row, []).append((x1, y1, x2, y2))
        for row, edges in edges_by_row.items():
            y = _row_lat(row)
            xs = []
            for x1, y1, x2, y2 in edges:
                xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
            xs.sort()
            for j in range(0, len(xs) - 1, 2):
                x_lo, x_hi = xs[j], xs[j + 1]
                c0 = int(math.ceil((x_lo + 180.0) / RES_DEG - 0.5 - 1e-9))
                c1 = int(math.floor((x_hi + 180.0) / RES_DEG - 0.5 + 1e-9))
                for col in range(max(0, c0), min(W - 1, c1) + 1):
                    set_cell(row, col)
        # Vertex pass: any cell a coastline vertex touches is land (keeps small islands).
        for ring in rings:
            for pt in ring:
                set_cell(_row_of_lat(pt[1]), _col_of_lng(pt[0]))
        if (fi + 1) % 200 == 0:
            print(f"  rasterized {fi + 1}/{len(features)} features")
    return bits


def is_land(bits: bytes, lat: float, lng: float) -> bool:
    i = _row_of_lat(lat) * W + _col_of_lng(lng)
    return bool(bits[i >> 3] & (0x80 >> (i & 7)))


def write_grid(bits: bytes, path: str = OUT_PATH):
    land_cells = sum(bin(b).count("1") for b in bits)
    out = {
        "_README": ("Packed land/water bitmap, row 0 = lat 90..89.75 N, col 0 = lng -180..."
                    "-179.75, bit index row*w+col, MSB-first per byte, base64. Rasterized from "
                    "Natural Earth 1:50m land polygons (public domain) by "
                    "tools/build_land_grid.py — regenerate with that script, don't hand-edit. "
                    "Land-biased on coasts: cells touched by any coastline vertex count as "
                    "land, so the sea-gap check only fires on genuinely open water."),
        "res_deg": RES_DEG,
        "w": W,
        "h": H,
        "land_cells": land_cells,
        "source": "Natural Earth 1:50m land (public domain), via " + SOURCE_URL,
        "b64": base64.b64encode(bytes(bits)).decode("ascii"),
    }
    path = os.path.normpath(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=0)
        f.write("\n")
    pct = 100.0 * land_cells / (W * H)
    print(f"wrote {path}  ({os.path.getsize(path)} bytes, {land_cells} land cells = {pct:.1f}%)")


CHECKS = [
    # (name, lat, lng, expect_land)
    ("Kansas plains", 38.5, -98.0, True),
    ("Sahara", 23.0, 10.0, True),
    ("Siberia", 62.0, 100.0, True),
    ("Mid-Atlantic", 30.0, -40.0, False),
    ("Mid-Pacific", 0.0, -140.0, False),
    ("Gulf of Finland (mid)", 59.85, 25.1, False),
    ("Aegean open water (Evia-Chios gap)", 38.1, 25.3, False),
    ("Adriatic (Gargano-Lastovo gap)", 42.3, 16.6, False),
    ("English Channel (mid)", 50.3, -0.5, False),
    ("Great Britain (inland)", 52.5, -1.5, True),
    ("Ireland (inland)", 53.3, -7.7, True),
    ("Sardinia", 40.1, 9.0, True),
    ("Crete", 35.2, 24.9, True),
    ("Aland main island", 60.2, 19.9, True),
    ("Oahu", 21.45, -158.0, True),
    ("Maui", 20.8, -156.3, True),
    ("Kauai channel (Hawaii)", 21.3, -157.0, False),
    ("Tasman Sea", -38.0, 160.0, False),
    ("Lake Michigan counts as land-mass detour", 43.5, -87.2, None),  # informational only
]


def run_checks(bits: bytes) -> int:
    fails = 0
    for name, lat, lng, expect in CHECKS:
        got = is_land(bits, lat, lng)
        if expect is None:
            print(f"  [info] {name}: {'land' if got else 'water'}")
            continue
        ok = got == expect
        fails += (not ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: expected {'land' if expect else 'water'}, "
              f"got {'land' if got else 'water'}")
    return fails


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    gj = fetch_geojson()
    bits = rasterize(gj)
    fails = run_checks(bits) if "--check" in argv or True else 0
    if fails:
        print(f"\n{fails} validation checks FAILED — not writing the grid")
        return 1
    write_grid(bits)
    return 0


if __name__ == "__main__":
    sys.exit(main())
