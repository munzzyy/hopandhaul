#!/usr/bin/env python3
"""
go.py — the one-shot trip plan: `hopandhaul go JFK "Tallinn" --date 2026-08-15`.

Everything the map click does, in a terminal, with ZERO keys: resolves the origin and
destination (IATA code, or a city/airport name looked up in the bundled 4,175-airport DB),
runs the full planning pipeline (direct vs fly-into-a-cheaper-hub splits, real ferry
corridors, BTS-anchored US fares, the $200 rule), and prints the ranked options with a
leg-by-leg itinerary and per-leg price provenance. Live extras turn on by themselves when
available: real Transitous ground schedules (keyless) and real Duffel fares (with a key).

Examples:
  hopandhaul go JFK TLL --date 2026-08-15
  hopandhaul go "New York" "Santorini" --date 2026-08-15 --travelers 2
  hopandhaul go LAX "Victoria BC" --offline        (no network calls at all)
  python -m hopandhaul.go --selftest               (offline, no network)
"""
from __future__ import annotations

import argparse
import json
import sys

from . import duffel, geo, server, trip


def resolve_airport(query: str) -> tuple[dict | None, list[dict]]:
    """(airport, candidates). A 3-letter code resolves exactly; otherwise the bundled DB is
    searched by city/name. One confident hit -> (airport, []); several plausible ones ->
    (best, others) so the CLI can say what it picked and what else matched. A query with a
    trailing qualifier the DB doesn't carry ("Victoria BC", "Springfield Missouri") retries
    with trailing words dropped."""
    q = (query or "").strip()
    if not q:
        return None, []
    if len(q) == 3 and q.isalpha():
        a = geo.by_iata(q)
        if a:
            return a, []
    best, others = _search_airports(q)
    words = q.split()
    while best is None and len(words) > 1:
        words = words[:-1]
        best, others = _search_airports(" ".join(words))
    return best, others


def _search_airports(q: str) -> tuple[dict | None, list[dict]]:
    ql = q.lower()
    scored = []
    for a in geo.airports():
        city = (a.get("city") or "").lower()
        name = (a.get("name") or "").lower()
        if ql == city:
            score = 0
        elif city.startswith(ql):
            score = 1
        elif ql in city or ql in name:
            score = 2
        else:
            continue
        scored.append((score, a["hub"], a))
    if not scored:
        return None, []
    scored.sort(key=lambda s: (s[0], s[1]))
    best = scored[0][2]
    # other DISTINCT places that matched about as well — distance is the disambiguator, not
    # name equality: "Springfield" hits both Springfield IL (exact) and Springfield MO
    # (prefix), while a same-metro second airport (LGA next to JFK) must stay suppressed.
    others = [a for sc, _hub, a in scored[1:8]
              if sc <= scored[0][0] + 1
              and geo.haversine_km(a["lat"], a["lng"], best["lat"], best["lng"]) > 150][:4]
    return best, others


def _with_private_rows(result: dict) -> dict:
    """trip.format_report needs the private rows plan() strips for the JSON API."""
    rec = next(o for o in result["options"] if o["name"] == result["recommended"])
    base = next(o for o in result["options"] if o.get("is_baseline"))
    return {**result, "_recommended_row": rec, "_baseline_row": base}


def main(argv=None) -> int:
    trip._force_utf8()
    p = argparse.ArgumentParser(
        description="One-shot trip plan with the $200 fly-then-ground rule. Zero keys needed.")
    p.add_argument("origin", nargs="?", help="origin airport code or city (e.g. JFK, 'New York')")
    p.add_argument("dest", nargs="?", help="destination code or place (e.g. TLL, 'Santorini')")
    p.add_argument("--date", default=None, help="departure date YYYY-MM-DD")
    p.add_argument("--return-date", dest="ret", default=None, help="return date YYYY-MM-DD")
    p.add_argument("--travelers", type=int, default=1)
    p.add_argument("--vot", type=float, default=None, help="value of time $/hr")
    p.add_argument("--threshold", type=float, default=trip.DEFAULT_THRESHOLD,
                   help=f"min $ a split must save (default {trip.DEFAULT_THRESHOLD:g})")
    p.add_argument("--max-ground-hours", type=float, default=6.0, dest="max_ground_h")
    p.add_argument("--offline", action="store_true",
                   help="no network at all: bundled data + estimates only")
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
    if not (args.origin and args.dest):
        p.error("give an origin and a destination, e.g.  hopandhaul go JFK TLL")

    origin, o_others = resolve_airport(args.origin)
    if not origin:
        print(f"error: no airport matches {args.origin!r} — try an IATA code", file=sys.stderr)
        return 2
    dest, d_others = resolve_airport(args.dest)
    if not dest:
        print(f"error: no airport matches {args.dest!r} — try an IATA code", file=sys.stderr)
        return 2
    # Destination point: the airport is the fallback, but when the user typed a PLACE and
    # we're online, the town itself is the honest target — plan() resolves its own nearest
    # airport from the point, the last-mile note stays accurate, and live ground schedules
    # can route to somewhere people actually go (transit can't snap a runway coordinate).
    # When the geocoder flatly disagrees with the airport-DB guess ("Victoria BC" matching
    # Victoria, Texas), the geocoder wins — the user typed a place name, and plan() will
    # re-derive the right airport from the right point.
    dest_lat, dest_lng = dest["lat"], dest["lng"]
    if not args.offline and not (len(args.dest.strip()) == 3 and args.dest.strip().isalpha()):
        try:
            from . import places
            hits = places.geocode(args.dest, limit=5)
            if hits:
                near = [h for h in hits
                        if geo.haversine_km(h["lat"], h["lng"], dest["lat"], dest["lng"]) <= 200]
                pick = near[0] if near else hits[0]
                dest_lat, dest_lng = pick["lat"], pick["lng"]
                if not near:
                    print(f"note: going by the geocoder's read of {args.dest!r} "
                          f"({pick.get('label') or pick.get('city')}), not the airport-name "
                          f"match ({dest['iata']})", file=sys.stderr)
        except Exception:
            pass    # geocoding is a refinement, never a blocker
    for label, picked, others in (("origin", origin, o_others), ("destination", dest, d_others)):
        if others:
            alts = ", ".join(f"{a['iata']} ({a.get('city')}, {a.get('country')})" for a in others)
            print(f"note: {label} matched {picked['iata']} ({picked.get('city')}, "
                  f"{picked.get('country')}); other matches: {alts}", file=sys.stderr)

    out = server.plan(
        dest_lat, dest_lng, origin_iata=origin["iata"], date=args.date, ret=args.ret,
        vot=args.vot, threshold=args.threshold, max_ground_h=args.max_ground_h,
        travelers=args.travelers, fetch_weather=not args.offline,
        allow_live=not args.offline, allow_transit=not args.offline,
    )
    if not out.get("ok"):
        print(f"error: {out.get('error', 'could not plan that trip')}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    print(f"(pricing: {out['pricing_source']})")
    # labels come from the plan's own resolution — when the geocoder moved the point, the
    # plan's dest airport is the truth, not the pre-geocode name match
    o_lbl = f"{out['origin']['iata']} {out['origin'].get('city') or ''}".strip()
    d_lbl = f"{out['dest']['iata']} {out['dest'].get('city') or ''}".strip()
    print(trip.format_report(_with_private_rows(out["result"]), o_lbl, d_lbl))
    itin = duffel.format_itineraries(out["result"])
    if itin:
        print()
        print(itin)
    wx = out.get("weather")
    if wx and wx.get("temp") is not None:
        line = f"\nWEATHER AT DESTINATION: {wx['emoji']} {wx['temp']}{wx['units']} — {wx['desc']}"
        fc = wx.get("forecast")
        if fc:
            line += f"  (on {fc['date']}: {fc['emoji']} {fc['temp']}{fc['units']}, {fc['desc']})"
        print(line)
    if out.get("notes"):
        print("\nNOTES:")
        for n in out["notes"]:
            print(f"  • {n}")
    return 0


# --------------------------------------------------------------------------- self-test (offline)
def selftest() -> int:
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    a, others = resolve_airport("JFK")
    check("IATA code resolves exactly", a and a["iata"] == "JFK" and not others)
    a2, _ = resolve_airport("tallinn")
    check("city name resolves via the bundled DB", a2 and a2["iata"] == "TLL")
    a3, _ = resolve_airport("Santorini")
    check("island name resolves", a3 and a3["iata"] == "JTR")
    a4, others4 = resolve_airport("springfield")
    check("ambiguous city returns a pick plus the other Springfields",
          a4 is not None and len(others4) >= 1)
    a5, _ = resolve_airport("xyzzy-nowhere")
    check("nonsense resolves to nothing, not a guess", a5 is None)
    a6, _ = resolve_airport("Victoria BC")
    check("a trailing qualifier the DB doesn't carry is dropped ('Victoria BC' -> Victoria)",
          a6 is not None and (a6.get("city") or "").lower().startswith("victoria"))

    # end-to-end offline: the exact pipeline `hopandhaul go` runs, no network
    out = server.plan(a3["lat"], a3["lng"], origin_iata="LHR", fetch_weather=False,
                      allow_live=False, allow_transit=False)
    check("offline end-to-end plan works", out.get("ok") is True)
    rep = trip.format_report(_with_private_rows(out["result"]), "LHR London", "JTR Santorini")
    check("report renders with a recommendation", "RECOMMENDED" in rep)
    itin = duffel.format_itineraries(out["result"])
    check("itineraries render with provenance", "ITINERARIES" in itin and "estimate" in itin)
    check("Santorini plan carries a real ferry option",
          any(g.get("ferry") for g in out["gateways"]))

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (offline checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
