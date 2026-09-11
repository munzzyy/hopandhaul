#!/usr/bin/env python3
"""
go.py - the one-shot trip plan: `hopandhaul go JFK "Tallinn" --date 2027-06-15`.

Everything the map click does, in a terminal, with ZERO keys: resolves the origin and
destination (IATA code, or a city/airport name looked up in the bundled 4,175-airport DB),
runs the full planning pipeline (direct vs fly-into-a-cheaper-hub splits, real ferry
corridors, BTS-anchored US fares, the $200 rule), and prints the ranked options with a
leg-by-leg itinerary and per-leg price provenance. Live extras turn on by themselves when
available: real Transitous ground schedules (keyless) and real Duffel fares (with a key).

Examples:
  hopandhaul go JFK TLL --date 2027-06-15
  hopandhaul go "New York" "Santorini" --date 2027-06-15 --travelers 2
  hopandhaul go LAX "Victoria BC" --offline        (no network calls at all)
  python -m hopandhaul.go --selftest               (offline, no network)
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import functools
import io
import json
import sys

from . import duffel, geo, itinerary, server, trip

# A trailing qualifier the airport DB doesn't carry as a searchable word ("Victoria BC",
# "Portland OR") used to just get dropped and the remaining city name tie-broken by hub tier -
# which silently prefers whichever same-named city has the bigger airport, regardless of which
# country the qualifier actually named ("Victoria BC" resolving to Victoria, Texas). These are
# the qualifiers explicit enough to safely narrow the search to one country before that
# tie-break ever runs.
_CA_PROVINCES = {"BC", "ON", "QC", "AB", "MB", "SK", "NS", "NB", "NL", "PE"}
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WI", "WY",
}
_AU_STATES = {"NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"}
_ISO_COUNTRY_CODES = None


def _iso_country_codes() -> set:
    """Every ISO-3166 country code the bundled airport DB actually uses - computed from the
    data itself (a["country"]) rather than hand-maintaining a second full country-code table
    that could drift from what airports.json ships."""
    global _ISO_COUNTRY_CODES
    if _ISO_COUNTRY_CODES is None:
        _ISO_COUNTRY_CODES = {a["country"] for a in geo.airports() if a.get("country")}
    return _ISO_COUNTRY_CODES


def _qualifier_country(word: str) -> str | None:
    """A dropped trailing word -> the ISO2 country it names, or None. Region tables (checked
    first - a US state code should mean the US state, not accidentally the one ISO country code
    that happens to share the same two letters) before a bare ISO-3166 country code."""
    w = word.upper()
    if w in _CA_PROVINCES:
        return "CA"
    if w in _US_STATES:
        return "US"
    if w in _AU_STATES:
        return "AU"
    if len(w) == 2 and w in _iso_country_codes():
        return w
    return None


def resolve_airport(query: str) -> tuple[dict | None, list[dict]]:
    """(airport, candidates). A 3-letter code resolves exactly; otherwise the bundled DB is
    searched by city/name. One confident hit -> (airport, []); several plausible ones ->
    (best, others) so the CLI can say what it picked and what else matched. A query with a
    trailing qualifier the DB doesn't carry ("Victoria BC", "Springfield Missouri") retries
    with trailing words dropped - and when a dropped word names a country/province/state
    (_qualifier_country), the retry is filtered to that country before the hub tie-break, so
    "Victoria BC" can't resolve to Victoria, Texas just because it's the bigger airport."""
    q = (query or "").strip()
    if not q:
        return None, []
    if len(q) == 3 and q.isalpha():
        a = geo.by_iata(q)
        if a:
            return a, []
    best, others = _search_airports(q)
    words = q.split()
    country = None
    while best is None and len(words) > 1:
        qc = _qualifier_country(words[-1])
        if qc:
            country = qc
        words = words[:-1]
        best, others = _search_airports(" ".join(words), country=country)
    return best, others


def _search_airports(q: str, country: str | None = None) -> tuple[dict | None, list[dict]]:
    ql = q.lower()
    scored = []
    for a in geo.airports():
        if country and a.get("country") != country:
            continue
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
    # other DISTINCT places that matched about as well - distance is the disambiguator, not
    # name equality: "Springfield" hits both Springfield IL (exact) and Springfield MO
    # (prefix), while a same-metro second airport (LGA next to JFK) must stay suppressed.
    others = [a for sc, _hub, a in scored[1:8]
              if sc <= scored[0][0] + 1
              and geo.haversine_km(a["lat"], a["lng"], best["lat"], best["lng"]) > 150][:4]
    return best, others


def _suggest_airports(q: str, limit: int = 3) -> list[dict]:
    """Typo tolerance: when substring search misses entirely ('Aspin', 'Bostn', 'Pariss'),
    fall back to difflib.get_close_matches over every city/airport name instead of just
    failing - a CLI should say "did you mean" the same way a shell does. Several airports can
    share a city name ('Paris' -> CDG/ORY/BVA); pick the best-connected one (lowest hub tier)
    for that match instead of whichever happened to be inserted first."""
    names = {}
    for a in geo.airports():
        for key in (a.get("city"), a.get("name")):
            if not key:
                continue
            kl = key.lower()
            cur = names.get(kl)
            if cur is None or a["hub"] < cur["hub"]:
                names[kl] = a
    matches = difflib.get_close_matches((q or "").lower(), list(names.keys()), n=limit, cutoff=0.6)
    seen, out = set(), []
    for m in matches:
        a = names[m]
        if a["iata"] not in seen:
            seen.add(a["iata"])
            out.append(a)
    return out


def _with_private_rows(result: dict) -> dict:
    """trip.format_report needs the private rows plan() strips for the JSON API."""
    rec = next(o for o in result["options"] if o["name"] == result["recommended"])
    base = next(o for o in result["options"] if o.get("is_baseline"))
    return {**result, "_recommended_row": rec, "_baseline_row": base}


def main(argv=None) -> int:
    trip._force_utf8()
    p = argparse.ArgumentParser(
        prog="hopandhaul go",
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
    p.add_argument("--currency", default="USD",
                   help="display currency for the text report (e.g. EUR, GBP, JPY); "
                        "the $200 rule and --json output always stay USD (default USD)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
    if not (args.origin and args.dest):
        p.error("give an origin and a destination, e.g.  hopandhaul go JFK TLL")

    # Validate at the CLI boundary, the same way the HTTP surface does (server.parse_plan_params).
    # A malformed --date used to slip through and get silently ignored while the report still
    # claimed the fare was "date-adjusted"; travelers was silently clamped to 9; a return date
    # before the departure date planned anyway. Reject them here instead.
    try:
        if args.date:
            args.date = server._v_date(args.date, "date")
        if args.ret:
            args.ret = server._v_date(args.ret, "return date")
        if args.date and args.ret and args.ret < args.date:
            raise server.ValidationError("return date must be on or after the depart date")
        if not (1 <= args.travelers <= server.MAX_TRAVELERS):
            raise server.ValidationError(
                f"travelers must be between 1 and {server.MAX_TRAVELERS}")
    except server.ValidationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    def _no_match_error(query: str) -> None:
        sugg = _suggest_airports(query)
        if sugg:
            s = sugg[0]
            print(f"error: no airport matches {query!r}. Did you mean {s['iata']} "
                  f"({s.get('city') or s['name']})?", file=sys.stderr)
        else:
            print(f"error: no airport matches {query!r}, try an IATA code", file=sys.stderr)

    origin, o_others = resolve_airport(args.origin)
    if not origin:
        _no_match_error(args.origin)
        return 2
    dest, d_others = resolve_airport(args.dest)
    if not dest:
        _no_match_error(args.dest)
        return 2
    # Destination point: the airport is the fallback, but when the user typed a PLACE and
    # we're online, the town itself is the honest target - plan() resolves its own nearest
    # airport from the point, the last-mile note stays accurate, and live ground schedules
    # can route to somewhere people actually go (transit can't snap a runway coordinate).
    # When the geocoder flatly disagrees with the airport-DB guess ("Victoria BC" matching
    # Victoria, Texas), the geocoder wins - the user typed a place name, and plan() will
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
        # origin == destination is a bad-input error the same way a malformed --date is -
        # give it the same exit code as every other CLI validation failure, not the code a
        # runtime planning failure (no airport nearby, provider down) gets.
        return 2 if out.get("code") == "origin_is_destination" else 1
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    print(f"(pricing: {out['pricing_source']})")
    # labels come from the plan's own resolution - when the geocoder moved the point, the
    # plan's dest airport is the truth, not the pre-geocode name match
    o_lbl = f"{out['origin']['iata']} {out['origin'].get('city') or ''}".strip()
    d_lbl = f"{out['dest']['iata']} {out['dest'].get('city') or ''}".strip()
    cur = duffel.resolve_display_currency(args.currency)
    money_fmt = functools.partial(duffel.format_money, currency=cur) if cur != "USD" else None
    print(trip.format_report(_with_private_rows(out["result"]), o_lbl, d_lbl, money_fmt=money_fmt))
    itin = duffel.format_itineraries(out["result"], money_fmt=money_fmt)
    if itin:
        print()
        print(itin)
    wx = out.get("weather")
    if wx and wx.get("temp") is not None:
        line = f"\nWEATHER AT DESTINATION: {wx['emoji']} {wx['temp']}{wx['units']}, {wx['desc']}"
        fc = wx.get("forecast")
        if fc:
            line += f"  (on {fc['date']}: {fc['emoji']} {fc['temp']}{fc['units']}, {fc['desc']})"
        print(line)
    if out.get("notes"):
        print("\nNOTES:")
        for n in out["notes"]:
            print(f"  • {itinerary.render_note(n)}")
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
    check("'Victoria BC' resolves to the Canadian Victoria, not Victoria, Texas",
          a6 is not None and a6["iata"] in ("YYJ", "YWH") and a6["country"] == "CA")
    a7, _ = resolve_airport("Victoria TX")
    check("'Victoria TX' still resolves to the US Victoria (region filter isn't one-directional)",
          a7 is not None and a7["country"] == "US")

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

    # CLI-boundary validation: the go surface used to silently ignore a malformed date (while
    # still claiming a "date-adjusted" fare), silently clamp travelers to 9, and plan a
    # return-before-departure trip. All three must now exit 2 cleanly, matching the HTTP surface.
    check("a malformed --date is rejected (exit 2), not silently ignored",
          main(["JFK", "ASE", "--date", "2026-8-15", "--offline"]) == 2)
    check("--date banana is rejected",
          main(["JFK", "ASE", "--date", "banana", "--offline"]) == 2)
    check("--travelers above the max is rejected, not silently clamped to 9",
          main(["JFK", "ASE", "--travelers", "50", "--offline"]) == 2)
    check("--travelers below 1 is rejected",
          main(["JFK", "ASE", "--travelers", "0", "--offline"]) == 2)
    check("a return date before the departure date is rejected",
          main(["JFK", "ASE", "--date", "2026-08-15", "--return-date", "2026-08-01",
                "--offline"]) == 2)

    # --currency: a final-render conversion only - the $200 rule and every internal number
    # stay USD, but a known code renders the report in its own symbol, no stderr note.
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc_eur = main(["JFK", "ASE", "--offline", "--currency", "eur"])
    check("--currency accepts a lowercase code and renders the report in that currency",
          rc_eur == 0 and "€" in out_buf.getvalue())
    check("a known --currency prints no unknown-currency note",
          "no FX rate" not in err_buf.getvalue())

    out_buf2, err_buf2 = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf2), contextlib.redirect_stderr(err_buf2):
        rc_unk = main(["JFK", "ASE", "--offline", "--currency", "ZZZ"])
    check("an unrecognized --currency still succeeds, falling back to USD with a stderr note",
          rc_unk == 0 and "$" in out_buf2.getvalue() and "no FX rate" in err_buf2.getvalue())

    # typo tolerance: substring search misses entirely, difflib fallback should still suggest
    # the right airport instead of a flat "no match".
    sugg_a = _suggest_airports("Aspin")
    check("typo 'Aspin' suggests ASE (Aspen)", any(a["iata"] == "ASE" for a in sugg_a))
    sugg_b = _suggest_airports("Bostn")
    check("typo 'Bostn' suggests BOS (Boston)", any(a["iata"] == "BOS" for a in sugg_b))
    sugg_c = _suggest_airports("Pariss")
    check("typo 'Pariss' suggests a Paris airport (CDG/ORY)",
          any(a["iata"] in ("CDG", "ORY") for a in sugg_c))
    err_buf3 = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err_buf3):
        rc_typo = main(["Aspin", "JFK", "--offline"])
    check("a typo'd origin exits 2 with a 'did you mean' suggestion",
          rc_typo == 2 and "ASE" in err_buf3.getvalue())

    # origin == destination is a bad-input error - same exit code as every other CLI
    # validation failure (a malformed --date, travelers out of range), not the code a runtime
    # planning failure gets.
    err_buf4 = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err_buf4):
        rc_same = main(["JFK", "JFK", "--offline"])
    check("origin == destination exits 2, matching other validation errors", rc_same == 2)

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (offline checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
