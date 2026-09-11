#!/usr/bin/env python3
"""
itinerary.py - turns a priced trip.py option into a leg-by-leg, checkable schedule: real
airport/station endpoints (IATA + full name + city), a clock-time walk-through with realistic
buffers, per-leg price provenance ("where did this number come from"), and a one-click link to
check it against reality.

Why this exists: a dollar figure with no airports, no schedule, and no way to check it is
indistinguishable from a random number generator. This module is what turns "$284" into "MCI
-> DEN, ~2h05, route-band estimate for 2026-08-15, check it on Google Flights."

CRITICAL HONESTY RULE - read this before touching the clock math below. Nothing here is a real
booking. Clock times are an EXAMPLE schedule: a sane default departure (08:00) walked forward
leg by leg with an airport-arrival buffer and a connection buffer between legs - never an
invented airline, flight number, or "departs 9:47am" precision pulled from nothing. The one
exception: when a real live Duffel offer supplies a leg's actual segment times/carrier/flight
number (see duffel.py's segment parsing), that leg is marked live=True and its numbers are
real, not an example.

No cross-timezone conversion. data/airports.json carries no timezone data, and guessing one
from longitude would be its own kind of dishonesty - real UTC offsets don't track longitude/15;
political borders, DST, and half-hour/45-minute zones all break that approximation. Every
synthetic clock time is elapsed trip-time counted forward from the stated origin departure, and
is labelled as such - a long transatlantic leg will show an "arrival" that doesn't match the
clock on the terminal wall at the destination, and build_timeline's output says so via
`example_day`. A live Duffel segment's times ARE real per-airport local times (Duffel resolves
that server-side, this module doesn't need to) and are used as-is; the synthetic clock re-syncs
to a live leg's real arrival time before continuing into whatever estimate leg comes next.

Pure stdlib. Importable by duffel.py (CLI) and server.py (JSON API / browser UI). The browser
gets a hand-ported mirror at ui/engine/itinerary.js, checked against this file's output via
tests/web_parity/ (it rides along inside server.py's plan() output, no separate case type
needed - see gen_fixtures.py/check.mjs).

Run: python -m hopandhaul.itinerary --selftest
"""
from __future__ import annotations

import datetime
import importlib.resources
import json
import urllib.parse

AIRPORT_ARRIVAL_BUFFER_H = 2.0     # standard "be there early" buffer before a flight's departure
DEFAULT_DEPART_LOCAL = "08:00"     # sane default start-of-day for an example schedule
# Independent copy of trip.FLIGHT_MODES - this module takes plain leg dicts from its callers
# and shouldn't need an import-order dependency on trip.py to classify a leg as a flight.
FLIGHT_MODES = {"fly", "flight", "plane", "air"}


# --------------------------------------------------------------------------- structured notes
# plan()'s output notes used to be hardcoded English strings, spliced untranslated into every
# non-English locale's trust box (see ui/i18n/*.json's other 45 catalogs). A note is now
# {"key": "notes.xxx", "params": {...}} - both engines emit the same shape (fixture-pinned in
# tests/web_parity/), the browser looks the key up in its own language file, and the CLI
# (below) renders it in English from the one EN catalog everyone ships.
_I18N_PKG = "hopandhaul.ui.i18n"
_EN_NOTES = None


_EN_CATALOG = None


def _en_catalog() -> dict:
    """The full ui/i18n/en.json catalog (notes.*, mode.*, option.*, basis.*, everything) - read
    once from the packaged file so both note templates AND {"i18n": "..."} param lookups share
    one source of truth with what the browser ships."""
    global _EN_CATALOG
    if _EN_CATALOG is None:
        ref = importlib.resources.files(_I18N_PKG) / "en.json"
        _EN_CATALOG = json.loads(ref.read_text(encoding="utf-8"))
    return _EN_CATALOG


def _en_notes() -> dict:
    """The 'notes.*' subset of the catalog, keyed without the 'notes.' prefix."""
    global _EN_NOTES
    if _EN_NOTES is None:
        _EN_NOTES = {k[len("notes."):]: v for k, v in _en_catalog().items() if k.startswith("notes.")}
    return _EN_NOTES


# A note param may be a plain value, or {"i18n": "mode.train"} when the value is itself a word
# that needs translating (a raw English mode name spliced into every locale's note used to leak
# untranslated English into all 45 catalogs - see docs/api.md's param-i18n convention). BOTH
# renderers (this one, and the browser's results.js) resolve it through their language catalog
# instead of formatting it as a literal string.
MODE_I18N_KEY = {
    "fly": "mode.flight", "flight": "mode.flight", "plane": "mode.flight", "air": "mode.flight",
    "train": "mode.train", "rail": "mode.train",
    "bus": "mode.bus", "coach": "mode.bus",
    "shuttle": "mode.shuttle",
    "drive": "mode.drive", "car": "mode.drive", "taxi": "mode.drive", "uber": "mode.drive",
    "rideshare": "mode.drive",
    "rental": "mode.rentalCar",
    "ferry": "mode.ferry",
    "ground": "mode.ground",
}


def mode_i18n_param(mode: str) -> dict:
    """A leg mode string -> {"i18n": "mode.xxx"} note param - see MODE_I18N_KEY above."""
    return {"i18n": MODE_I18N_KEY.get(mode, "mode.ground")}


def note(key: str, **params) -> dict:
    """Build a structured plan() note: {key, params}. Callers push this instead of a hardcoded
    English string - see the module docstring above."""
    return {"key": key, "params": params}


def _resolve_param(v):
    """{"i18n": "mode.train"} -> the EN catalog's word for that key; anything else passes
    through untouched. Used by render_note() (CLI, always English) - the browser's results.js
    does the equivalent lookup against whatever language catalog is active."""
    if isinstance(v, dict) and "i18n" in v:
        return _en_catalog().get(v["i18n"], v["i18n"])
    return v


def render_note(n: dict) -> str:
    """A structured note -> a plain English sentence, for the CLI (go.py/duffel.py), which
    always prints full English regardless of locale. Reads the same en.json the browser
    ships, so there is one source of truth for the wording, not two tables that can drift -
    a selftest below asserts every key this module can emit actually resolves."""
    key = n["key"]
    short = key[len("notes."):] if key.startswith("notes.") else key
    template = _en_notes().get(short)
    if template is None:
        return key   # a broken deploy (catalog missing a key) should be visible, not silent
    params = {k: _resolve_param(v) for k, v in (n.get("params") or {}).items()}
    return template.format(**params)


# --------------------------------------------------------------------------- clock math
def _hhmm_to_min(s: str) -> int:
    hh, mm = s.split(":")
    return int(hh) * 60 + int(mm)


def _min_to_hhmm(total_min: int) -> tuple[str, int]:
    """Minutes (may be negative or span multiple days) -> ('HH:MM', day_offset)."""
    day, rem = divmod(total_min, 1440)
    hh, mm = divmod(rem, 60)
    return f"{hh:02d}:{mm:02d}", day


def _day_label(day_offset: int, date: str | None) -> str:
    """A real calendar date when the caller gave us one to anchor to, else a relative
    'Day N' label - never fabricate a calendar date nobody asked for."""
    if date:
        try:
            d = datetime.date.fromisoformat(date) + datetime.timedelta(days=day_offset)
            return d.isoformat()
        except ValueError:
            pass
    return f"Day {day_offset + 1}"


def _airport_label(a: dict) -> dict:
    """iata + full name + city - the whole point of this module is never showing a bare code."""
    return {"iata": a.get("iata"), "name": a.get("name"), "city": a.get("city")}


# --------------------------------------------------------------------------- verify links
def google_flights_link(origin_iata: str, dest_iata: str, date: str | None = None,
                        return_date: str | None = None) -> str:
    """Deep link to check a flight leg's price against reality.
    Format: https://www.google.com/travel/flights?q=Flights+from+XXX+to+YYY+on+YYYY-MM-DD, or
    "...on YYYY-MM-DD through YYYY-MM-DD" for a round trip - a one-way query used to be built
    even when the priced fare covered a real round trip, which sent a reader to check half the
    price they were actually shown. `return_date` is ignored unless `date` is also given (a
    round trip with no outbound date has no honest range to print either).
    IATA codes are always short ASCII-letters-only strings by the time they reach here (see
    server.py's _v_iata / geo.by_iata), but the query text is still built through urlencode
    rather than hand-joined, so this stays correct even if a caller ever hands it a place name
    with spaces/punctuation instead."""
    q = f"Flights from {origin_iata} to {dest_iata}"
    if date and return_date:
        q += f" on {date} through {return_date}"
    elif date:
        q += f" on {date}"
    return "https://www.google.com/travel/flights?" + urllib.parse.urlencode({"q": q})


def _slug(text: str) -> str:
    """A readable, URL-safe path segment for rome2rio's /map/{from}/{to}: spaces -> '-', then
    percent-encode anything left (accents, punctuation, commas) - never hand-splice raw text
    into a URL path."""
    return urllib.parse.quote(text.strip().replace(" ", "-"), safe="-")


def rome2rio_link(from_place: str, to_place: str) -> str:
    """Deep link to check a ground leg's price/time against reality.
    Format: https://www.rome2rio.com/map/{from}/{to}"""
    return f"https://www.rome2rio.com/map/{_slug(from_place)}/{_slug(to_place)}"


def verify_link(mode: str, origin: dict, dest: dict, date: str | None = None,
                return_date: str | None = None) -> str:
    """Pick the right verify link for a leg's mode: Google Flights for anything that flies,
    Rome2Rio (city-to-city) for everything on the ground. `return_date` only ever matters for
    a flight leg - a ground leg has no round-trip query shape to build."""
    if mode in FLIGHT_MODES:
        return google_flights_link(origin["iata"], dest["iata"], date, return_date)
    from_place = origin.get("city") or origin.get("name") or origin["iata"]
    to_place = dest.get("city") or dest.get("name") or dest["iata"]
    return rome2rio_link(from_place, to_place)


# --------------------------------------------------------------------------- price provenance
def flight_provenance_estimate(detail: dict | None, date: str | None) -> str:
    """Human-readable 'where this number comes from' for an ESTIMATE flight leg. `detail` is
    geo.estimate_flight()'s own return dict (distance_km/route_mult/regions/date_mult/
    likely_connection) - never re-derived here, just narrated."""
    if not detail:
        return "route-band estimate"
    bits = [f"route-band estimate for {date}" if date else "route-band estimate (no date given)"]
    if detail.get("regions"):
        bits.append(f"{detail['regions']} market ×{detail.get('route_mult', 1.0):.2f}")
    an = detail.get("anchor")
    if an:
        # a REAL number rides along with the model: what this route's passengers actually paid.
        # Report where the FINAL fare landed, not where the clamp left it - the date multiplier
        # runs afterwards and regularly lifts the number back out of the band.
        if an.get("in_band") is False:
            # The date factor runs in BOTH directions: it clamps to [0.75, 1.75], so an advance
            # booking on a cheap month can push the fare under the band just as easily as a
            # last-minute one pushes it over. Saying "above" either way is wrong 58 route-days
            # out of the ones this repo's own airports can produce.
            way = "above" if detail["price"] > an["band_hi"] else "below"
            held = (f"the date factor put the estimate {way} that band "
                    f"(${an['band_lo']:g}-${an['band_hi']:g})")
        elif an.get("adjusted"):
            held = "estimate adjusted into that band"
        else:
            held = "estimate already inside that band"
        bits.append(f"real market check (BTS {an.get('asof', '')}): avg paid ${an['fare_avg']:g}"
                    f", lowest-fare carrier ${an['fare_low']:g}, {held}")
    if detail.get("date_mult"):
        bits.append(f"date factor ×{detail['date_mult']:.2f}")
    if detail.get("likely_connection"):
        bits.append("fare priced assuming a connecting flight (small/remote airport)")
    return "; ".join(bits)


def basis_parts_flight_estimate(detail: dict | None, date: str | None) -> list[dict]:
    """Structured twin of flight_provenance_estimate(): the SAME facts, as an ARRAY of
    {key, params} segments under basis.* instead of a hardcoded English sentence, so a locale
    can word each fact instead of inheriting English grammar wholesale. Both engines emit this
    (see ui/engine/itinerary.js); the English string stays too, for the CLI and backcompat -
    see docs/api.md for the full contract."""
    if not detail:
        return [{"key": "basis.routeBand", "params": {}}]
    parts = [{"key": "basis.routeBandDated", "params": {"date": date}} if date
             else {"key": "basis.routeBand", "params": {}}]
    if detail.get("regions"):
        parts.append({"key": "basis.marketMult",
                      "params": {"region": detail["regions"],
                                 "mult": round(detail.get("route_mult", 1.0), 2)}})
    an = detail.get("anchor")
    if an:
        parts.append({"key": "basis.anchoredBts",
                      "params": {"lo": an.get("fare_low"), "hi": an.get("fare_avg"),
                                 "asof": an.get("asof", "")}})
    if detail.get("date_mult"):
        parts.append({"key": "basis.dateAdjusted", "params": {"mult": round(detail["date_mult"], 2)}})
    if detail.get("likely_connection"):
        parts.append({"key": "basis.connectingAssumed", "params": {}})
    return parts


def basis_parts_flight_live(live: dict) -> list[dict]:
    """Structured twin of flight_provenance_live() - see basis_parts_flight_estimate()."""
    parts = [{"key": "basis.liveDuffel", "params": {"carrier": live.get("carrier") or ""}}]
    native = live.get("native_price")
    cur = live.get("currency")
    if native is not None and cur and cur != "USD":
        fx_key = ("basis.fxStaticPriced" if live.get("rate_source") == "static"
                 else "basis.fxLivePriced" if live.get("converted") else "basis.fxNativePriced")
        parts.append({"key": fx_key, "params": {"native": native, "currency": cur}})
    return parts


def basis_parts_ferry(ferry: dict) -> list[dict]:
    """Structured twin of ferry_provenance() - see basis_parts_flight_estimate()."""
    return [{"key": "basis.ferryBand",
            "params": {"lo": ferry.get("price_usd_lo"), "hi": ferry.get("price_usd_hi"),
                       "operators": ", ".join(ferry.get("operators") or []) or None,
                       "asof": ferry.get("price_asof") or ""}}]


def basis_parts_ground(gw: dict, road_km: float | None) -> list[dict]:
    """Structured twin of ground_provenance() - see basis_parts_flight_estimate()."""
    if gw.get("ferry"):
        parts = basis_parts_ferry(gw["ferry"])
    elif gw.get("source") == "curated":
        parts = [{"key": "basis.curatedGateway", "params": {}}]
    else:
        parts = [{"key": "basis.groundEstimate",
                 "params": {"km": int(road_km) if road_km is not None else None}}]
    tr = gw.get("transit")
    if tr and tr.get("line"):
        parts.append({"key": "basis.transitLive", "params": {"line": tr["line"]}})
    return parts


def flight_provenance_live(live: dict) -> str:
    """'where this number comes from' for a LIVE (Duffel) flight leg. A STATIC-table FX
    conversion is called out by name (the bundled table is pinned to a date and can be
    months stale) rather than lumped in with a real live ECB rate under the same generic
    'converted' phrase - the two carry very different reasons to double check the number."""
    carrier = live.get("carrier") or "an airline"
    bits = [f"live fare from {carrier}"]
    native = live.get("native_price")
    cur = live.get("currency")
    if native is not None and cur and cur != "USD":
        if live.get("rate_source") == "static":
            conv = " (converted to USD via a static FX table, not live - verify at booking)"
        elif live.get("converted"):
            conv = " (converted to USD at today's live rate)"
        else:
            conv = ""
        bits.append(f"priced {native} {cur}{conv}")
    return "; ".join(bits)


def ferry_provenance(ferry: dict) -> str:
    """'where this number comes from' for a REAL ferry-corridor leg - the one ground mode whose
    price is a researched fare, not a formula. Names the ports, the operators, the fare band
    with its as-of date, and the sailing frequency, so the number is checkable against the
    operator directly."""
    ops = ", ".join(ferry.get("operators") or []) or "operator n/a"
    bits = []
    lo, hi = ferry.get("price_usd_lo"), ferry.get("price_usd_hi")
    asof = ferry.get("price_asof") or "n/a"
    if ferry.get("fare_is_real") and lo is not None:
        band = f"${lo:g} to ${hi:g}" if hi is not None and hi != lo else f"from ${lo:g}"
        bits.append(f"real ferry fare {band} ({ops}; as of {asof})")
    else:
        bits.append(f"ferry fare estimate ({ops})")
    bits.append(f"{ferry['port_a']} → {ferry['port_b']}, ~{ferry['duration_h']:g}h crossing")
    freq = ferry.get("frequency_per_day")
    if freq:
        bits.append(f"~{freq:g} sailings/day" + (", seasonal service" if ferry.get("seasonal") else ""))
    elif ferry.get("seasonal"):
        bits.append("seasonal service")
    if ferry.get("access_cost") is not None:
        bits.append(f"+ ~${ferry['access_cost']:g} airport-to-port transfer estimate")
    return "; ".join(bits)


def ground_provenance(gw: dict, road_km: float | None) -> str:
    """'where this number comes from' for a ground leg. Ferry legs built from a real corridor
    narrate their researched fare/schedule (ferry_provenance); everything else is an estimate - 
    schedules can be checked live (Transitous), but no free, open multimodal FARES API exists
    (see README)."""
    base = None
    if gw.get("ferry"):
        base = ferry_provenance(gw["ferry"])
    elif gw.get("source") == "curated":
        note = gw.get("notes")
        base = f"curated gateway estimate{(': ' + note) if note else ''}"
    else:
        km = f"~{int(road_km)}km" if road_km is not None else "distance-based"
        base = f"ground estimate ({km} road/rail distance, regional rate table)"
    tr = gw.get("transit")
    if tr and tr.get("line"):
        base = f"{base}; {tr['line']}"
    return base


# --------------------------------------------------------------------------- timeline builder
def build_timeline(legs: list[dict], *, date: str | None = None,
                   depart_local: str = DEFAULT_DEPART_LOCAL,
                   transfer_buffer_h: float = 1.0,
                   airport_buffer_h: float = AIRPORT_ARRIVAL_BUFFER_H) -> dict:
    """legs: ordered leg specs, one per trip.py leg, each:
      {"mode", "cost", "hours", "from": airport_dict, "to": airport_dict,
       "price_basis": str, "verify_url": str, "is_live": bool, "segments": [...] | None}
    `segments` (only meaningful when is_live) are real per-hop dicts from duffel.py:
      {"from": airport_dict, "to": airport_dict, "depart_at": datetime, "arrive_at": datetime,
       "carrier": str|None, "flight_number": str|None}

    Returns {"legs": [...], "any_live": bool, "example_day": bool, "depart_local": str}.
    `example_day` stays True while ANY leg is still an estimate - a single live-priced flight
    next to an estimated ground leg must not let the block claim the whole day is real; the
    per-leg `is_live` flags say which rows came from a real offer.

    Clock math walks forward from `depart_local` at the very first leg's departure (no
    timezone conversion - see the module docstring). Each leg's own `hours` advances the
    clock; a `transfer_buffer_h` gap is inserted BETWEEN legs, matching the same buffer
    trip.evaluate() already added to the option's hours_eff, so this timeline's total elapsed
    time always reconciles with the headline number a caller already computed and tested - 
    the itinerary can't tell a different story than the summary card next to it.
    """
    if not legs:
        return {"legs": [], "any_live": False, "example_day": True, "depart_local": depart_local}

    rows: list[dict] = []
    clock_min = _hhmm_to_min(depart_local)
    any_live = False

    for i, leg in enumerate(legs):
        if i > 0:
            clock_min += round(transfer_buffer_h * 60)

        segments = leg.get("segments") if leg.get("is_live") else None
        if segments:
            any_live = True
            leg_rows, clock_min = _live_segments_to_rows(
                leg, segments, date, add_checkin=(i == 0), airport_buffer_h=airport_buffer_h)
            rows.extend(leg_rows)
            continue

        is_flight = leg["mode"] in FLIGHT_MODES
        depart_clock, dep_day = _min_to_hhmm(clock_min)
        checkin_by = None
        if is_flight:
            checkin_clock, checkin_day = _min_to_hhmm(clock_min - round(airport_buffer_h * 60))
            # day_n rides along so the browser can localize "Day N" via its catalog - the
            # string form stays for the CLI. None when a real date made the label an ISO date.
            checkin_by = {"clock": checkin_clock, "day": _day_label(checkin_day, date),
                          "day_n": None if date else checkin_day + 1}
        arrive_min = clock_min + round(leg["hours"] * 60)
        arrive_clock, arr_day = _min_to_hhmm(arrive_min)
        rows.append({
            "mode": leg["mode"],
            "from": _airport_label(leg["from"]),
            "to": _airport_label(leg["to"]),
            "depart_clock": depart_clock, "depart_day": _day_label(dep_day, date),
            "depart_day_n": None if date else dep_day + 1,
            "arrive_clock": arrive_clock, "arrive_day": _day_label(arr_day, date),
            "arrive_day_n": None if date else arr_day + 1,
            "duration_h": round(leg["hours"], 2),
            "checkin_by": checkin_by,
            "cost": leg["cost"],
            "price_basis": leg["price_basis"],
            "basis_parts": leg.get("basis_parts", []),
            "verify_url": leg["verify_url"],
            "is_live": False,
            "carrier": None,
            "flight_number": None,
            # the option's own fare narrative already assumed a connection to price this leg
            # (a tiny/remote field far from the other end - see geo.estimate_flight's
            # likely_connection) - the leg must say so instead of implying nonstop just
            # because the geometry drawn on the map is a single arc.
            "label": note("notes.legLikelyConnecting") if leg.get("likely_connection") else None,
        })
        clock_min = arrive_min

    return {
        "legs": rows,
        "any_live": any_live,
        "example_day": any(not r["is_live"] for r in rows),
        "depart_local": depart_local,
    }


def _live_segments_to_rows(leg: dict, segments: list[dict], date: str | None,
                           add_checkin: bool, airport_buffer_h: float):
    """Real Duffel segment times -> timeline rows for one live flight leg (possibly more than
    one hop if the cheapest offer connects). Returns (rows, resync_clock_min) - resync_clock_min
    lets a later synthetic leg (e.g. the ground leg after a live-priced flight) continue from
    this leg's REAL arrival instead of the synthetic clock it would otherwise have reached."""
    rows = []
    anchor = datetime.date.fromisoformat(date) if date else None
    last_arrive_min = None
    for idx, seg in enumerate(segments):
        dep_dt, arr_dt = seg["depart_at"], seg["arrive_at"]
        dep_day = (dep_dt.date() - anchor).days if anchor else 0
        arr_day = (arr_dt.date() - anchor).days if anchor else 0
        checkin_by = None
        if add_checkin and idx == 0:
            checkin_dt = dep_dt - datetime.timedelta(hours=airport_buffer_h)
            checkin_day = (checkin_dt.date() - anchor).days if anchor else 0
            checkin_by = {"clock": checkin_dt.strftime("%H:%M"), "day": _day_label(checkin_day, date),
                          "day_n": None if date else checkin_day + 1}
        rows.append({
            "mode": "fly",
            "from": _airport_label(seg["from"]),
            "to": _airport_label(seg["to"]),
            "depart_clock": dep_dt.strftime("%H:%M"), "depart_day": _day_label(dep_day, date),
            "depart_day_n": None if date else dep_day + 1,
            "arrive_clock": arr_dt.strftime("%H:%M"), "arrive_day": _day_label(arr_day, date),
            "arrive_day_n": None if date else arr_day + 1,
            "duration_h": round((arr_dt - dep_dt).total_seconds() / 3600.0, 2),
            "checkin_by": checkin_by,
            "cost": leg["cost"] if idx == 0 else 0.0,   # the fare covers the whole leg; shown once
            "price_basis": leg["price_basis"],
            "basis_parts": leg.get("basis_parts", []),
            "verify_url": leg["verify_url"],
            "is_live": True,
            "carrier": seg.get("carrier"),
            "flight_number": seg.get("flight_number"),
            # a live leg's real segment count already shows a genuine connection (more than one
            # row); "likely connecting" is for the ESTIMATE path only, where nothing else says so.
            "label": None,
        })
        last_arrive_min = arr_day * 1440 + _hhmm_to_min(arr_dt.strftime("%H:%M"))
    return rows, last_arrive_min


# --------------------------------------------------------------------------- self-test
def selftest() -> int:
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    # this module never touches geo.fare_date_multiplier() - `date` here only anchors clock/
    # day-rollover labels, so past-vs-future is never load-bearing for correctness. Anchored
    # to today anyway so these fixtures don't read as a stale, long-past example date.
    def _d(days):
        return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()

    _travel = datetime.date.fromisoformat(_d(70))

    jfk = {"iata": "JFK", "name": "John F Kennedy International Airport", "city": "New York"}
    den = {"iata": "DEN", "name": "Denver International Airport", "city": "Denver"}
    ase = {"iata": "ASE", "name": "Aspen/Pitkin County Airport", "city": "Aspen"}

    # ---- link builders
    link = google_flights_link("JFK", "ASE", _d(70))
    check("google flights link has the right host + path",
          link.startswith("https://www.google.com/travel/flights?q="))
    check("google flights link encodes spaces as + (urlencode, not raw text)",
          "+" in link and " " not in link)
    check("google flights link contains both IATA codes and the date",
          "JFK" in link and "ASE" in link and _d(70) in link)
    link_no_date = google_flights_link("JFK", "ASE")
    check("google flights link omits 'on ...' when no date is given", "+on+" not in link_no_date)

    rt_link = google_flights_link("JFK", "ASE", _d(70), _d(77))
    check("a round-trip google flights link carries BOTH dates ('through'), not just the outbound",
          _d(70) in rt_link and _d(77) in rt_link and "through" in rt_link)
    check("a return_date with no outbound date is dropped (no honest range to print)",
          "through" not in google_flights_link("JFK", "ASE", None, _d(77)))
    check("verify_link forwards return_date for a flight leg",
          "through" in verify_link("fly", jfk, den, _d(70), _d(77)))
    check("verify_link ignores return_date for a ground leg (no round-trip query shape)",
          "through" not in verify_link("train", den, ase, _d(70), _d(77)))

    r2r = rome2rio_link("New York", "Denver, CO")
    check("rome2rio link has the right host + path shape",
          r2r == "https://www.rome2rio.com/map/New-York/Denver%2C-CO")
    weird = rome2rio_link("São Paulo", "Ciudad de México")
    check("rome2rio link percent-encodes non-ASCII place names, no raw unicode leaks into the URL",
          all(ord(c) < 128 for c in weird))
    check("rome2rio link never contains a literal space (URL-encoding safety)",
          " " not in r2r and " " not in weird)

    check("verify_link picks Google Flights for a fly leg",
          verify_link("fly", jfk, den, _d(70)).startswith("https://www.google.com/travel/flights"))
    check("verify_link picks Rome2Rio for a ground leg",
          verify_link("train", den, ase).startswith("https://www.rome2rio.com/map/"))

    # a place name containing URL-meaningful characters must not corrupt the path or escape it
    injected = rome2rio_link("Denver/../../etc", "Aspen?x=1&y=2")
    check("rome2rio link neutralizes path-traversal-shaped input (no raw '/' or '..' segment survives)",
          "/../" not in injected and injected.count("/map/") == 1)

    # ---- provenance strings
    est_detail = {"regions": "NA-NA", "route_mult": 1.0, "date_mult": 1.08, "likely_connection": False}
    prov = flight_provenance_estimate(est_detail, _d(70))
    check("flight provenance names the date and the route-market multiplier",
          _d(70) in prov and "NA-NA" in prov and "1.00" in prov and "1.08" in prov)
    check("flight provenance with no detail still returns something honest",
          flight_provenance_estimate(None, None) == "route-band estimate")

    live_detail = {"carrier": "United Airlines", "native_price": 199.0, "currency": "GBP", "converted": True}
    live_prov = flight_provenance_live(live_detail)
    check("live provenance names the carrier and flags a currency conversion",
          "United Airlines" in live_prov and "GBP" in live_prov and "converted" in live_prov)

    live_detail_static = {**live_detail, "rate_source": "static"}
    static_prov = flight_provenance_live(live_detail_static)
    check("a STATIC-table conversion is named explicitly, not lumped in with a live rate",
          "static" in static_prov.lower() and "not live" in static_prov.lower())
    live_detail_live = {**live_detail, "rate_source": "live"}
    live_rate_prov = flight_provenance_live(live_detail_live)
    check("a live-rate conversion says so and never claims 'static'",
          "live rate" in live_rate_prov.lower() and "static" not in live_rate_prov.lower())

    check("curated ground provenance says so and carries the note",
          "curated" in ground_provenance({"source": "curated", "notes": "well-known Amtrak run"}, None))
    check("auto ground provenance shows the road distance",
          "~186km" in ground_provenance({"source": "auto"}, 186.4))

    # ---- timeline: single direct flight leg
    direct_legs = [{
        "mode": "fly", "cost": 284.0, "hours": 2.5, "from": jfk, "to": den,
        "price_basis": f"route-band estimate for {_d(70)}", "verify_url": "https://x",
        "is_live": False, "segments": None,
    }]
    tl = build_timeline(direct_legs, date=_d(70))
    check("direct timeline has exactly one leg", len(tl["legs"]) == 1)
    row = tl["legs"][0]
    check("direct flight departs at the default 08:00 anchor", row["depart_clock"] == "08:00")
    check("direct flight arrives 2h30 later at 10:30", row["arrive_clock"] == "10:30")
    check("direct flight's depart/arrive land on the same given date",
          row["depart_day"] == row["arrive_day"] == _d(70))
    check("a fly leg carries a checkin_by ~2h before departure", row["checkin_by"]["clock"] == "06:00")
    check("build_timeline with no date falls back to relative 'Day N' labels",
          build_timeline(direct_legs)["legs"][0]["depart_day"] == "Day 1")
    check("an estimate-only timeline is flagged example_day",
          tl["example_day"] is True and tl["any_live"] is False)
    check("a leg with no likely_connection flag renders with no label",
          row["label"] is None)

    # a leg whose fare narrative already assumed a connection (small/remote-airport pricing)
    # must say so, not read as a bare "fly" leg that looks nonstop.
    connecting_legs = [{
        "mode": "fly", "cost": 310.0, "hours": 5.1, "from": jfk, "to": ase,
        "price_basis": "route-band estimate; fare priced assuming a connecting flight",
        "verify_url": "https://x", "is_live": False, "segments": None, "likely_connection": True,
    }]
    tl_conn = build_timeline(connecting_legs, date=_d(70))
    conn_row = tl_conn["legs"][0]
    check("a leg priced assuming a connection carries a structured 'likely connecting' label, "
          "not a bare fly leg that reads as nonstop",
          conn_row["label"] == {"key": "notes.legLikelyConnecting", "params": {}})

    # ---- timeline: fly + ground split, connection buffer must land between legs, and the
    # summed elapsed time must equal each leg's own hours plus exactly one transfer buffer - 
    # the same total trip.evaluate() already computes as hours_eff, so the two can never disagree.
    split_legs = [
        {"mode": "fly", "cost": 210.0, "hours": 3.0, "from": jfk, "to": den,
         "price_basis": "route-band estimate", "verify_url": "https://x", "is_live": False, "segments": None},
        {"mode": "train", "cost": 75.0, "hours": 6.0, "from": den, "to": ase,
         "price_basis": "ground estimate", "verify_url": "https://y", "is_live": False, "segments": None},
    ]
    tl2 = build_timeline(split_legs, date=_d(70), transfer_buffer_h=1.0)
    check("split timeline has both legs", len(tl2["legs"]) == 2)
    fly_row, ground_row = tl2["legs"]
    check("fly leg departs 08:00, arrives 11:00 (3h)",
          fly_row["depart_clock"] == "08:00" and fly_row["arrive_clock"] == "11:00")
    check("ground leg departs after a 1h transfer buffer (12:00, not 11:00)",
          ground_row["depart_clock"] == "12:00")
    check("ground leg has no checkin_by (only flights get one)", ground_row["checkin_by"] is None)
    check("ground leg arrives 6h after its own departure (18:00)", ground_row["arrive_clock"] == "18:00")
    total_elapsed_min = _hhmm_to_min(ground_row["arrive_clock"]) - _hhmm_to_min(fly_row["depart_clock"])
    expected_elapsed_min = round((3.0 + 6.0) * 60) + round(1.0 * 60)   # both legs' hours + 1 transfer buffer
    check("total elapsed time matches leg hours + transfer buffer (reconciles with trip.py's hours_eff)",
          total_elapsed_min == expected_elapsed_min)

    # ---- timeline: day rollover past midnight is labelled, not silently wrapped to 00:00
    long_leg = [{"mode": "fly", "cost": 900.0, "hours": 18.0, "from": jfk, "to": den,
                "price_basis": "x", "verify_url": "https://x", "is_live": False, "segments": None}]
    tl3 = build_timeline(long_leg, date=_d(70), depart_local="20:00")
    r3 = tl3["legs"][0]
    check("an overnight leg rolls its arrival to the next calendar day",
          r3["depart_day"] == _d(70) and r3["arrive_day"] == _d(71))
    check("the rolled-over arrival clock is correct (20:00 + 18h = 14:00)", r3["arrive_clock"] == "14:00")

    # ---- timeline: a live leg's real segment times are used as-is, and a later estimate leg
    # re-anchors to the live leg's real arrival instead of the synthetic 08:00-based clock.
    live_leg = [{
        "mode": "fly", "cost": 241.5, "hours": 5.5, "from": jfk, "to": den, "is_live": True,
        "price_basis": "live fare from United Airlines", "verify_url": "https://x",
        "segments": [{
            "from": jfk, "to": den,
            "depart_at": datetime.datetime(_travel.year, _travel.month, _travel.day, 14, 5),
            "arrive_at": datetime.datetime(_travel.year, _travel.month, _travel.day, 16, 47),
            "carrier": "United Airlines", "flight_number": "UA1234",
        }],
    }, {
        "mode": "train", "cost": 75.0, "hours": 6.0, "from": den, "to": ase,
        "price_basis": "ground estimate", "verify_url": "https://y", "is_live": False, "segments": None,
    }]
    tl4 = build_timeline(live_leg, date=_d(70), transfer_buffer_h=1.0)
    live_row, next_row = tl4["legs"]
    check("live leg uses the real Duffel segment departure/arrival times, not the 08:00 synthetic anchor",
          live_row["depart_clock"] == "14:05" and live_row["arrive_clock"] == "16:47")
    check("live leg carries the real carrier + flight number", live_row["carrier"] == "United Airlines"
          and live_row["flight_number"] == "UA1234")
    check("live leg is flagged is_live=True and any_live is set",
          live_row["is_live"] is True and tl4["any_live"] is True)
    check("a mixed live+estimate timeline is STILL example_day: one real fare must not "
          "let the block claim the whole day is real while the ground leg is an estimate",
          tl4["example_day"] is True and next_row["is_live"] is False)
    check("the live leg's own checkin_by is ~2h before its REAL departure (12:05), not the synthetic anchor",
          live_row["checkin_by"]["clock"] == "12:05")
    check("the following ground leg re-anchors to the live leg's real arrival + transfer buffer (17:47)",
          next_row["depart_clock"] == "17:47")

    # ---- honesty: an estimate leg never claims to be live, and vice versa
    check("a synthetic leg's price_basis is whatever the caller supplied "
          "(this module narrates, doesn't invent)",
          fly_row["price_basis"] == "route-band estimate")
    check("empty legs list returns an empty, honestly-labelled timeline",
          build_timeline([]) == {"legs": [], "any_live": False, "example_day": True, "depart_local": "08:00"})

    # structured notes: {key, params} renders through en.json, not a hardcoded string.
    n1 = note("notes.groupTotals", travelers=4, vehicles=1)
    check("note() builds a structured {key, params} dict",
          n1 == {"key": "notes.groupTotals", "params": {"travelers": 4, "vehicles": 1}})
    check("render_note fills the template from en.json",
          render_note(n1) == "Costs are group totals for 4 travelers. Per-person fares are "
                             "multiplied by 4. Drive and rental legs are priced for 1 vehicle(s).")
    check("render_note handles a key with no params",
          "estimates" in render_note(note("notes.estimateNoProvider")).lower())
    check("an unknown key renders as itself rather than raising (fail visibly, not crash)",
          render_note({"key": "notes.doesNotExist", "params": {}}) == "notes.doesNotExist")
    # every key trip.py/server.py/plan.js can actually emit must resolve in en.json - this is
    # the "stays in sync" guarantee the task asked for, made structural (read the same file)
    # rather than a second hand-maintained table that could drift.
    emittable_keys = [
        "notes.estimatePastDate", "notes.estimateDateApplied", "notes.estimateNeutralWindow",
        "notes.estimateAddDateForLive", "notes.estimateNoProvider", "notes.mixedLiveEstimate",
        "notes.liveLookupFailed", "notes.fxStatic", "notes.fxLive", "notes.fxUnknown",
        "notes.groupTotals", "notes.roundtripReal", "notes.roundtripEstimatedSeparate",
        "notes.roundtripEstimated2x", "notes.ferryRealCorridor", "notes.transitLiveSchedule",
        "notes.lastMileGap", "notes.co2eEstimate", "notes.originSuspended",
        "notes.airportSuspended", "notes.finalLeg", "notes.legLikelyConnecting",
        "notes.liveBaggageCaveat",
    ]
    missing = [k for k in emittable_keys if k[len("notes."):] not in _en_notes()]
    check(f"every note key this module can emit resolves in en.json (missing: {missing})",
          not missing)

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (itinerary checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print("itinerary.py: import me, or run with --selftest")
