#!/usr/bin/env python3
"""
build_basemap.py - generates src/hopandhaul/data/basemap.json: simplified world land,
lake, and country-border geometry the browser draws itself on canvas (atlas.js), instead of
renting raster tiles from a provider that now watermarks every image "API KEY REQUIRED".

Why this exists: CARTO's free basemap tiles stopped working without a key, so the very first
thing every visitor saw was a watermarked map. Drawing the base map from our own bundled
geometry keeps the app at zero keys forever, makes it work fully offline (it's already an
installable PWA), and lets every one of the 8 themes tint the map to match instead of being
stuck with one hardcoded raster style.

Source: Natural Earth 1:50m (public domain, https://www.naturalearthdata.com/), fetched as
GeoJSON from the naturalearth/naturalearth-vector GitHub mirror - land, lakes, and the
country boundary-lines layer (not the country polygons: the app only ever needs the lines
between countries, and that file is a fraction of the size). fitBounds() caps the app's own
zoom at 8 and this is a destination-picker map, not turn-by-turn navigation, so 1:50m detail
is plenty; an optional live OpenStreetMap layer (map.js) covers anyone who wants street level.

Method: Douglas-Peucker simplification (own ~20-line implementation, no deps) at two
tolerances, coarse for whole-world views (zoom <= 4) and fine for closer ones (zoom >= 5), then
coordinates are rounded to 3 decimal places (~110m at the equator - well under a pixel at the
zooms this data is ever shown at). Two tiers instead of one keeps the world view light without
starving continent-level views of coastline detail.

Run:  python tools/build_basemap.py            (writes src/hopandhaul/data/basemap.json)
"""
from __future__ import annotations

import json
import math
import os
import sys
import urllib.request

RAW = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
SOURCES = {
    "land": RAW + "ne_50m_land.geojson",
    "lakes": RAW + "ne_50m_lakes.geojson",
    "borders": RAW + "ne_50m_admin_0_boundary_lines_land.geojson",
}
# (ring-min-points, coarse-tolerance-deg, fine-tolerance-deg) per layer - borders and lakes
# are already sparse relative to the coastline, so they get lighter simplification.
TOLERANCE = {
    "land": (0.35, 0.05),
    "lakes": (0.25, 0.04),
    "borders": (0.25, 0.04),
}
MIN_RING_POINTS = 4     # a closed ring simpler than a triangle isn't worth keeping
MIN_LINE_POINTS = 2
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "src", "hopandhaul", "data", "basemap.json")


def fetch_geojson(url: str) -> dict:
    print(f"fetching {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "hopandhaul-basemap-build"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    print(f"  {len(data.get('features', []))} features")
    return data


def rdp(points: list[list[float]], epsilon: float) -> list[list[float]]:
    """Douglas-Peucker line simplification. Iterative (explicit stack), not recursive - a
    near-straight coastline segment can be thousands of points deep, past Python's default
    recursion limit."""
    n = len(points)
    if n < 3:
        return points
    keep = bytearray(n)
    keep[0] = keep[n - 1] = 1
    stack = [(0, n - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        x1, y1 = points[start]
        x2, y2 = points[end]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy)
        best_d, best_i = -1.0, -1
        for i in range(start + 1, end):
            x0, y0 = points[i]
            d = (math.hypot(x0 - x1, y0 - y1) if norm == 0
                 else abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / norm)
            if d > best_d:
                best_d, best_i = d, i
        if best_d > epsilon:
            keep[best_i] = 1
            stack.append((start, best_i))
            stack.append((best_i, end))
    return [p for i, p in enumerate(points) if keep[i]]


def quantize(points: list[list[float]]) -> list[list[float]]:
    """Round to 3 decimals and drop points that collapse onto their predecessor - RDP already
    removed the geometrically redundant ones, but rounding can create fresh duplicates."""
    out = []
    for x, y in points:
        p = [round(x, 3), round(y, 3)]
        if not out or out[-1] != p:
            out.append(p)
    return out


def _paths(feature) -> list[list[list[float]]]:
    """Every line/ring in a feature as a flat list of [lng, lat] paths, whatever the geometry
    type (Point/MultiPoint layers never appear in these three sources)."""
    geom = feature.get("geometry") or {}
    t, c = geom.get("type"), geom.get("coordinates") or []
    if t == "Polygon":
        return list(c)
    if t == "MultiPolygon":
        return [ring for poly in c for ring in poly]
    if t == "LineString":
        return [c]
    if t == "MultiLineString":
        return list(c)
    return []


def simplify_layer(geojson: dict, epsilon: float, min_points: int) -> list[list[list[float]]]:
    out = []
    for feat in geojson.get("features", []):
        for path in _paths(feat):
            simplified = quantize(rdp(path, epsilon))
            if len(simplified) >= min_points:
                out.append(simplified)
    return out


def build_tier(raw: dict, tier: int) -> dict:
    layers = {}
    for name, geojson in raw.items():
        eps = TOLERANCE[name][tier]
        min_pts = MIN_LINE_POINTS if name == "borders" else MIN_RING_POINTS
        layers[name] = simplify_layer(geojson, eps, min_pts)
        print(f"  {name} tier={tier}: {len(layers[name])} paths")
    return layers


def write_basemap(raw: dict, path: str = OUT_PATH):
    out = {
        "_README": ("Simplified world geometry for the self-drawn atlas base layer (see "
                    "src/hopandhaul/ui/atlas.js). Two detail tiers: 'coarse' for whole-world "
                    "views, 'fine' for continent level and closer. Each layer is a flat list "
                    "of [lng, lat] paths - 'land' and 'lakes' are closed rings (fill), "
                    "'borders' are open polylines (stroke). Rasterized from Natural Earth "
                    "1:50m (public domain) by tools/build_basemap.py - regenerate with that "
                    "script, don't hand-edit."),
        "source": "Natural Earth 1:50m (public domain), https://www.naturalearthdata.com/",
        "coarse": build_tier(raw, 0),
        "fine": build_tier(raw, 1),
    }
    path = os.path.normpath(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), sort_keys=True)
        f.write("\n")
    print(f"wrote {path}  ({os.path.getsize(path)} bytes)")


def main(argv=None) -> int:
    raw = {name: fetch_geojson(url) for name, url in SOURCES.items()}
    write_basemap(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
