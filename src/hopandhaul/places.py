#!/usr/bin/env python3
"""
places.py - place search that works with ZERO keys.

Geocoding used to require a Geoapify key, which meant the search box was dead on a fresh
install. Photon (photon.komoot.io - Apache-2.0 software, OSM data, keyless, fair-use) now
answers by default; a Geoapify key, when present, still takes precedence (higher volume
allowance, address-level results). Same normalized result shape either way, so server.py and
the UI don't care which provider answered.

Photon usage note: fair-use, no hard published quota - this module caches, sends an
identifying User-Agent, and is only ever called from a local, single-user server. Result data
is OSM -> attribution "© OpenStreetMap contributors" ships in the README/UI.

Run:  python -m hopandhaul.places "Aspen, Colorado"
      python -m hopandhaul.places --selftest     (offline, no network)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

from . import __version__
from . import geoapify
from .integrations import net

PHOTON_BASE = "https://photon.komoot.io/api/"
UA = f"hopandhaul/{__version__} (https://github.com/munzzyy/hopandhaul)"

_PHOTON_CACHE = net.TTLCache(ttl_seconds=600, max_size=256)


def available() -> bool:
    """Place search is always available now - Photon needs no key."""
    return True


def provider() -> str:
    return "geoapify" if geoapify.have_keys() else "photon"


def _photon_clean(feat: dict) -> dict | None:
    """Normalize a Photon GeoJSON feature to the same shape geoapify._clean produces."""
    geom = feat.get("geometry") or {}
    coords = geom.get("coordinates") or []
    props = feat.get("properties") or {}
    if len(coords) < 2:
        return None
    name = props.get("name") or ""
    bits = [b for b in (name, props.get("city") if props.get("city") != name else None,
                        props.get("state"), props.get("country")) if b]
    return {
        "lat": coords[1],
        "lng": coords[0],
        "label": ", ".join(bits) or name,
        "city": props.get("city") or name,
        "country": props.get("country"),
        "country_code": (props.get("countrycode") or "").upper(),
        "type": props.get("osm_value") or props.get("type"),
    }


def _photon_geocode(text: str, limit: int = 5, lang: str = "en", timeout: int = 12) -> list[dict]:
    q = (text or "").strip()
    if not q:
        return []
    cache_key = (q.lower(), limit, lang)
    cached = _PHOTON_CACHE.get(cache_key)
    if cached is not None:
        return cached
    params = {"q": q, "limit": str(limit)}
    if lang in ("en", "de", "fr"):     # Photon only supports a few response languages
        params["lang"] = lang
    url = PHOTON_BASE + "?" + urllib.parse.urlencode(params)
    out = net.fetch_json(url, headers={"User-Agent": UA, "Accept": "application/json"},
                         timeout=timeout)
    rows = out.get("features") or []
    result = [c for c in (_photon_clean(f) for f in rows) if c]
    _PHOTON_CACHE.set(cache_key, result)
    return result


def geocode(text: str, limit: int = 5, lang: str = "en", timeout: int = 15) -> list[dict]:
    """Forward geocode -> ranked candidates. Geoapify when keyed, else Photon (keyless)."""
    if geoapify.have_keys():
        return geoapify.geocode(text, limit=limit, lang=lang, timeout=timeout)
    return _photon_geocode(text, limit=limit, lang=lang, timeout=timeout)


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(description="Place search (Photon keyless; Geoapify when keyed).")
    p.add_argument("query", nargs="*", help="place to search")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
    q = " ".join(args.query).strip()
    if not q:
        p.error("give a place to search")
    try:
        res = geocode(q, limit=args.limit)
    except net.FetchError as e:
        print(f"lookup failed: {e}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(f"(provider: {provider()})")
        for r in res:
            print(f"  {r['label']}  ({r['lat']:.4f}, {r['lng']:.4f})")
    return 0


# --------------------------------------------------------------------------- self-test (offline)
def selftest() -> int:
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    check("available() is always True (keyless Photon default)", available() is True)
    check("provider() names a real provider", provider() in ("photon", "geoapify"))

    feat = {"geometry": {"coordinates": [2.3522, 48.8566]},
            "properties": {"name": "Paris", "country": "France", "countrycode": "fr",
                           "state": "Ile-de-France", "osm_value": "city"}}
    c = _photon_clean(feat)
    check("photon feature normalizes to the shared shape (lng/lat swap handled)",
          c["lat"] == 48.8566 and c["lng"] == 2.3522 and c["city"] == "Paris"
          and c["country_code"] == "FR" and "France" in c["label"])
    check("photon label joins name/state/country without duplicating the city",
          c["label"].startswith("Paris") and c["label"].count("Paris") == 1)
    check("a feature with no coordinates is dropped, not crashed on",
          _photon_clean({"geometry": {}, "properties": {"name": "x"}}) is None)
    check("empty query short-circuits to [] without a network call",
          _photon_geocode("") == [] and _photon_geocode("   ") == [])
    check("UA identifies the project (Photon policy asks who's calling)",
          "hopandhaul" in UA and "github.com" in UA)

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (offline checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
