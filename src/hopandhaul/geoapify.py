#!/usr/bin/env python3
"""
geoapify.py - geocoding for travel-scout via the Geoapify Geocoding API.

Turns a typed place ("Aspen, CO", "38 Upper Montagu Street, London", "Chamonix") into
coordinates so the map/agent can plan to a name instead of only a click. Also reverse-geocodes
a lat/lng back to a human label for nicer output.

Key: GEOAPIFY_API_KEY (env or secrets.local.json). Stdlib urllib only.

Examples:
  python -m hopandhaul.geoapify "Aspen, Colorado"
  python -m hopandhaul.geoapify --reverse 39.19 -106.82
  python -m hopandhaul.geoapify --selftest
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

from . import _secrets
from .integrations import net

BASE = "https://api.geoapify.com/v1/geocode"

# 10-minute TTL: place text -> coordinates rarely changes mid-session, and this keeps repeat
# autocomplete keystrokes from re-hitting Geoapify for the same partial query.
_GEOCODE_CACHE = net.TTLCache(ttl_seconds=600, max_size=256)


def have_keys() -> bool:
    return _secrets.has("GEOAPIFY_API_KEY")


def _http_json(url: str, timeout: int = 15) -> dict:
    return net.fetch_json(url, headers={"Accept": "application/json"}, timeout=timeout)


def _clean(r: dict) -> dict:
    """Normalize a Geoapify result row to the fields the UI/agent use."""
    return {
        "lat": r.get("lat"),
        "lng": r.get("lon"),
        "label": r.get("formatted") or r.get("address_line1") or r.get("name") or "",
        "city": r.get("city") or r.get("county") or r.get("state"),
        "country": r.get("country"),
        "country_code": (r.get("country_code") or "").upper(),
        "type": r.get("result_type") or r.get("type"),
    }


def geocode(text: str, limit: int = 5, lang: str = "en", timeout: int = 15) -> list[dict]:
    """Forward geocode a place/address string -> ranked list of candidates."""
    if not have_keys():
        return []
    q = (text or "").strip()
    if not q:
        return []
    cache_key = ("fwd", q.lower(), limit, lang)
    cached = _GEOCODE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    params = {"text": q, "limit": str(limit), "lang": lang,
              "format": "json", "apiKey": _secrets.get("GEOAPIFY_API_KEY")}
    url = f"{BASE}/search?" + urllib.parse.urlencode(params)
    out = _http_json(url, timeout=timeout)
    rows = out.get("results") or []
    result = [_clean(r) for r in rows if r.get("lat") is not None and r.get("lon") is not None]
    _GEOCODE_CACHE.set(cache_key, result)
    return result


def reverse(lat: float, lng: float, lang: str = "en", timeout: int = 15) -> dict | None:
    """Reverse geocode a coordinate -> a single human label."""
    if not have_keys():
        return None
    cache_key = ("rev", round(lat, 5), round(lng, 5), lang)
    cached = _GEOCODE_CACHE.get(cache_key)
    if cached is not None:
        return cached or None
    params = {"lat": str(lat), "lon": str(lng), "lang": lang,
              "format": "json", "apiKey": _secrets.get("GEOAPIFY_API_KEY")}
    url = f"{BASE}/reverse?" + urllib.parse.urlencode(params)
    out = _http_json(url, timeout=timeout)
    rows = out.get("results") or []
    result = _clean(rows[0]) if rows else None
    _GEOCODE_CACHE.set(cache_key, result)
    return result


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(description="Geoapify geocoding for travel-scout.")
    p.add_argument("query", nargs="*", help="place/address to geocode")
    p.add_argument("--reverse", nargs=2, metavar=("LAT", "LNG"), help="reverse geocode a point")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
    if not have_keys():
        print("GEOAPIFY_API_KEY not set (env or secrets.local.json). Geocoding unavailable.")
        return 2

    try:
        if args.reverse:
            res = reverse(float(args.reverse[0]), float(args.reverse[1]))
            out = [res] if res else []
        else:
            q = " ".join(args.query).strip()
            if not q:
                p.error("give a place to geocode, or --reverse LAT LNG")
            out = geocode(q, limit=args.limit)
    except net.FetchError as e:
        print(f"Geoapify request failed: {e}")
        return 3

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        if not out:
            print("No matches.")
        for r in out:
            print(f"  {r['lat']:.4f},{r['lng']:.4f}  {r['label']}  [{r.get('type') or '?'}]")
    return 0


# --------------------------------------------------------------------------- self-test (offline)
def selftest():
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    row = _clean({"lat": 39.19, "lon": -106.82, "formatted": "Aspen, CO, USA",
                  "city": "Aspen", "country": "United States", "country_code": "us",
                  "result_type": "city"})
    check("clean maps lon->lng and formatted->label",
          row["lng"] == -106.82 and row["label"].startswith("Aspen") and row["country_code"] == "US")
    check("clean keeps lat", row["lat"] == 39.19)
    check("have_keys() is a bool", isinstance(have_keys(), bool))
    # empty query short-circuits without a network call
    check("blank query returns []", geocode("   ") == [])

    _GEOCODE_CACHE.set(("fwd", "aspen, co", 5, "en"), [row])
    check("geocode cache stores under the (kind, text, limit, lang) key",
          _GEOCODE_CACHE.get(("fwd", "aspen, co", 5, "en")) == [row])
    check("net.TTLCache is the backing cache type", isinstance(_GEOCODE_CACHE, net.TTLCache))

    # reverse() shares _clean/_http_json with geocode but had no coverage of its
    # own guard or cache-key shape. Deterministic with or without keys: with a
    # key configured the guard short-circuits True; without one, reverse must
    # return None before any network call.
    check("reverse() short-circuits to None without API keys",
          have_keys() or reverse(39.19, -106.82) is None)
    _GEOCODE_CACHE.set(("rev", 39.19, -106.82, "en"), row)
    check("reverse cache round-trips under the (rev, lat, lng, lang) key",
          _GEOCODE_CACHE.get(("rev", 39.19, -106.82, "en")) == row)

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (offline checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
