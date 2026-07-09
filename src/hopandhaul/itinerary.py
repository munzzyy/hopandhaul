#!/usr/bin/env python3
"""
itinerary.py — turns a priced trip.py option into a leg-by-leg, checkable schedule: real
airport/station endpoints (IATA + full name + city), a clock-time walk-through with realistic
buffers, per-leg price provenance ("where did this number come from"), and a one-click link to
check it against reality.

Why this exists: a dollar figure with no airports, no schedule, and no way to check it is
indistinguishable from a random number generator. This module is what turns "$284" into "MCI
-> DEN, ~2h05, route-band estimate for 2026-08-15, check it on Google Flights."

CRITICAL HONESTY RULE — read this before touching the clock math below. Nothing here is a real
booking. Clock times are an EXAMPLE schedule: a sane default departure (08:00) walked forward
leg by leg with an airport-arrival buffer and a connection buffer between legs — never an
invented airline, flight number, or "departs 9:47am" precision pulled from nothing. The one
exception: when a real live Duffel offer supplies a leg's actual segment times/carrier/flight
number (see duffel.py's segment parsing), that leg is marked live=True and its numbers are
real, not an example.

No cross-timezone conversion. data/airports.json carries no timezone data, and guessing one
from longitude would be its own kind of dishonesty — real UTC offsets don't track longitude/15;
political borders, DST, and half-hour/45-minute zones all break that approximation. Every
synthetic clock time is elapsed trip-time counted forward from the stated origin departure, and
is labelled as such — a long transatlantic leg will show an "arrival" that doesn't match the
clock on the terminal wall at the destination, and build_timeline's output says so via
`example_day`. A live Duffel segment's times ARE real per-airport local times (Duffel resolves
that server-side, this module doesn't need to) and are used as-is; the synthetic clock re-syncs
to a live leg's real arrival time before continuing into whatever estimate leg comes next.

Pure stdlib. Importable by duffel.py (CLI) and server.py (JSON API / browser UI). The browser
gets a hand-ported mirror at ui/engine/itinerary.js, checked against this file's output via
tests/web_parity/ (it rides along inside server.py's plan() output, no separate case type
needed — see gen_fixtures.py/check.mjs).

Run: python -m hopandhaul.itinerary --selftest
"""
from __future__ import annotations

import datetime
import urllib.parse

AIRPORT_ARRIVAL_BUFFER_H = 2.0     # standard "be there early" buffer before a flight's departure
DEFAULT_DEPART_LOCAL = "08:00"     # sane default start-of-day for an example schedule
# Independent copy of trip.FLIGHT_MODES — this module takes plain leg dicts from its callers
# and shouldn't need an import-order dependency on trip.py to classify a leg as a flight.
FLIGHT_MODES = {"fly", "flight", "plane", "air"}


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
    'Day N' label — never fabricate a calendar date nobody asked for."""
    if date:
        try:
            d = datetime.date.fromisoformat(date) + datetime.timedelta(days=day_offset)
            return d.isoformat()
        except ValueError:
            pass
    return f"Day {day_offset + 1}"


def _airport_label(a: dict) -> dict:
    """iata + full name + city — the whole point of this module is never showing a bare code."""
    return {"iata": a.get("iata"), "name": a.get("name"), "city": a.get("city")}


# --------------------------------------------------------------------------- verify links
def google_flights_link(origin_iata: str, dest_iata: str, date: str | None = None) -> str:
    """Deep link to check a flight leg's price against reality.
    Format: https://www.google.com/travel/flights?q=Flights+from+XXX+to+YYY+on+YYYY-MM-DD
    IATA codes are always short ASCII-letters-only strings by the time they reach here (see
    server.py's _v_iata / geo.by_iata), but the query text is still built through urlencode
    rather than hand-joined, so this stays correct even if a caller ever hands it a place name
    with spaces/punctuation instead."""
    q = f"Flights from {origin_iata} to {dest_iata}"
    if date:
        q += f" on {date}"
    return "https://www.google.com/travel/flights?" + urllib.parse.urlencode({"q": q})


def _slug(text: str) -> str:
    """A readable, URL-safe path segment for rome2rio's /map/{from}/{to}: spaces -> '-', then
    percent-encode anything left (accents, punctuation, commas) — never hand-splice raw text
    into a URL path."""
    return urllib.parse.quote(text.strip().replace(" ", "-"), safe="-")


def rome2rio_link(from_place: str, to_place: str) -> str:
    """Deep link to check a ground leg's price/time against reality.
    Format: https://www.rome2rio.com/map/{from}/{to}"""
    return f"https://www.rome2rio.com/map/{_slug(from_place)}/{_slug(to_place)}"


def verify_link(mode: str, origin: dict, dest: dict, date: str | None = None) -> str:
    """Pick the right verify link for a leg's mode: Google Flights for anything that flies,
    Rome2Rio (city-to-city) for everything on the ground."""
    if mode in FLIGHT_MODES:
        return google_flights_link(origin["iata"], dest["iata"], date)
    from_place = origin.get("city") or origin.get("name") or origin["iata"]
    to_place = dest.get("city") or dest.get("name") or dest["iata"]
    return rome2rio_link(from_place, to_place)


# --------------------------------------------------------------------------- price provenance
def flight_provenance_estimate(detail: dict | None, date: str | None) -> str:
    """Human-readable 'where this number comes from' for an ESTIMATE flight leg. `detail` is
    geo.estimate_flight()'s own return dict (distance_km/route_mult/regions/date_mult/
    likely_connection) — never re-derived here, just narrated."""
    if not detail:
        return "route-band estimate"
    bits = [f"route-band estimate for {date}" if date else "route-band estimate (no date given)"]
    if detail.get("regions"):
        bits.append(f"{detail['regions']} market ×{detail.get('route_mult', 1.0):.2f}")
    if detail.get("date_mult"):
        bits.append(f"date factor ×{detail['date_mult']:.2f}")
    if detail.get("likely_connection"):
        bits.append("assumes one connection")
    return "; ".join(bits)


def flight_provenance_live(live: dict) -> str:
    """'where this number comes from' for a LIVE (Duffel) flight leg."""
    carrier = live.get("carrier") or "an airline"
    bits = [f"live fare — {carrier}"]
    native = live.get("native_price")
    cur = live.get("currency")
    if native is not None and cur and cur != "USD":
        conv = " (converted to USD, verify at booking)" if live.get("converted") else ""
        bits.append(f"priced {native} {cur}{conv}")
    return "; ".join(bits)


def ground_provenance(gw: dict, road_km: float | None) -> str:
    """'where this number comes from' for a ground leg — always an estimate; there's no free,
    open multimodal fares API worth calling here (see README)."""
    if gw.get("source") == "curated":
        note = gw.get("notes")
        return f"curated gateway estimate{(' — ' + note) if note else ''}"
    km = f"~{int(road_km)}km" if road_km is not None else "distance-based"
    return f"ground estimate ({km} road/rail distance, regional rate table)"


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

    Clock math walks forward from `depart_local` at the very first leg's departure (no
    timezone conversion — see the module docstring). Each leg's own `hours` advances the
    clock; a `transfer_buffer_h` gap is inserted BETWEEN legs, matching the same buffer
    trip.evaluate() already added to the option's hours_eff, so this timeline's total elapsed
    time always reconciles with the headline number a caller already computed and tested —
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
            checkin_by = {"clock": checkin_clock, "day": _day_label(checkin_day, date)}
        arrive_min = clock_min + round(leg["hours"] * 60)
        arrive_clock, arr_day = _min_to_hhmm(arrive_min)
        rows.append({
            "mode": leg["mode"],
            "from": _airport_label(leg["from"]),
            "to": _airport_label(leg["to"]),
            "depart_clock": depart_clock, "depart_day": _day_label(dep_day, date),
            "arrive_clock": arrive_clock, "arrive_day": _day_label(arr_day, date),
            "duration_h": round(leg["hours"], 2),
            "checkin_by": checkin_by,
            "cost": leg["cost"],
            "price_basis": leg["price_basis"],
            "verify_url": leg["verify_url"],
            "is_live": False,
            "carrier": None,
            "flight_number": None,
        })
        clock_min = arrive_min

    return {
        "legs": rows,
        "any_live": any_live,
        "example_day": not any_live,
        "depart_local": depart_local,
    }


def _live_segments_to_rows(leg: dict, segments: list[dict], date: str | None,
                           add_checkin: bool, airport_buffer_h: float):
    """Real Duffel segment times -> timeline rows for one live flight leg (possibly more than
    one hop if the cheapest offer connects). Returns (rows, resync_clock_min) — resync_clock_min
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
            checkin_by = {"clock": checkin_dt.strftime("%H:%M"), "day": _day_label(checkin_day, date)}
        rows.append({
            "mode": "fly",
            "from": _airport_label(seg["from"]),
            "to": _airport_label(seg["to"]),
            "depart_clock": dep_dt.strftime("%H:%M"), "depart_day": _day_label(dep_day, date),
            "arrive_clock": arr_dt.strftime("%H:%M"), "arrive_day": _day_label(arr_day, date),
            "duration_h": round((arr_dt - dep_dt).total_seconds() / 3600.0, 2),
            "checkin_by": checkin_by,
            "cost": leg["cost"] if idx == 0 else 0.0,   # the fare covers the whole leg; shown once
            "price_basis": leg["price_basis"],
            "verify_url": leg["verify_url"],
            "is_live": True,
            "carrier": seg.get("carrier"),
            "flight_number": seg.get("flight_number"),
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

    jfk = {"iata": "JFK", "name": "John F Kennedy International Airport", "city": "New York"}
    den = {"iata": "DEN", "name": "Denver International Airport", "city": "Denver"}
    ase = {"iata": "ASE", "name": "Aspen/Pitkin County Airport", "city": "Aspen"}

    # ---- link builders
    link = google_flights_link("JFK", "ASE", "2026-08-15")
    check("google flights link has the right host + path",
          link.startswith("https://www.google.com/travel/flights?q="))
    check("google flights link encodes spaces as + (urlencode, not raw text)", "+" in link and " " not in link)
    check("google flights link contains both IATA codes and the date",
          "JFK" in link and "ASE" in link and "2026-08-15" in link)
    link_no_date = google_flights_link("JFK", "ASE")
    check("google flights link omits 'on ...' when no date is given", "+on+" not in link_no_date)

    r2r = rome2rio_link("New York", "Denver, CO")
    check("rome2rio link has the right host + path shape",
          r2r == "https://www.rome2rio.com/map/New-York/Denver%2C-CO")
    weird = rome2rio_link("São Paulo", "Ciudad de México")
    check("rome2rio link percent-encodes non-ASCII place names, no raw unicode leaks into the URL",
          all(ord(c) < 128 for c in weird))
    check("rome2rio link never contains a literal space (URL-encoding safety)",
          " " not in r2r and " " not in weird)

    check("verify_link picks Google Flights for a fly leg",
          verify_link("fly", jfk, den, "2026-08-15").startswith("https://www.google.com/travel/flights"))
    check("verify_link picks Rome2Rio for a ground leg",
          verify_link("train", den, ase).startswith("https://www.rome2rio.com/map/"))

    # a place name containing URL-meaningful characters must not corrupt the path or escape it
    injected = rome2rio_link("Denver/../../etc", "Aspen?x=1&y=2")
    check("rome2rio link neutralizes path-traversal-shaped input (no raw '/' or '..' segment survives)",
          "/../" not in injected and injected.count("/map/") == 1)

    # ---- provenance strings
    est_detail = {"regions": "NA-NA", "route_mult": 1.0, "date_mult": 1.08, "likely_connection": False}
    prov = flight_provenance_estimate(est_detail, "2026-08-15")
    check("flight provenance names the date and the route-market multiplier",
          "2026-08-15" in prov and "NA-NA" in prov and "1.00" in prov and "1.08" in prov)
    check("flight provenance with no detail still returns something honest",
          flight_provenance_estimate(None, None) == "route-band estimate")

    live_detail = {"carrier": "United Airlines", "native_price": 199.0, "currency": "GBP", "converted": True}
    live_prov = flight_provenance_live(live_detail)
    check("live provenance names the carrier and flags a currency conversion",
          "United Airlines" in live_prov and "GBP" in live_prov and "converted" in live_prov)

    check("curated ground provenance says so and carries the note",
          "curated" in ground_provenance({"source": "curated", "notes": "well-known Amtrak run"}, None))
    check("auto ground provenance shows the road distance",
          "~186km" in ground_provenance({"source": "auto"}, 186.4))

    # ---- timeline: single direct flight leg
    direct_legs = [{
        "mode": "fly", "cost": 284.0, "hours": 2.5, "from": jfk, "to": den,
        "price_basis": "route-band estimate for 2026-08-15", "verify_url": "https://x",
        "is_live": False, "segments": None,
    }]
    tl = build_timeline(direct_legs, date="2026-08-15")
    check("direct timeline has exactly one leg", len(tl["legs"]) == 1)
    row = tl["legs"][0]
    check("direct flight departs at the default 08:00 anchor", row["depart_clock"] == "08:00")
    check("direct flight arrives 2h30 later at 10:30", row["arrive_clock"] == "10:30")
    check("direct flight's depart/arrive land on the same given date",
          row["depart_day"] == row["arrive_day"] == "2026-08-15")
    check("a fly leg carries a checkin_by ~2h before departure", row["checkin_by"]["clock"] == "06:00")
    check("build_timeline with no date falls back to relative 'Day N' labels",
          build_timeline(direct_legs)["legs"][0]["depart_day"] == "Day 1")
    check("an estimate-only timeline is flagged example_day", tl["example_day"] is True and tl["any_live"] is False)

    # ---- timeline: fly + ground split, connection buffer must land between legs, and the
    # summed elapsed time must equal each leg's own hours plus exactly one transfer buffer —
    # the same total trip.evaluate() already computes as hours_eff, so the two can never disagree.
    split_legs = [
        {"mode": "fly", "cost": 210.0, "hours": 3.0, "from": jfk, "to": den,
         "price_basis": "route-band estimate", "verify_url": "https://x", "is_live": False, "segments": None},
        {"mode": "train", "cost": 75.0, "hours": 6.0, "from": den, "to": ase,
         "price_basis": "ground estimate", "verify_url": "https://y", "is_live": False, "segments": None},
    ]
    tl2 = build_timeline(split_legs, date="2026-08-15", transfer_buffer_h=1.0)
    check("split timeline has both legs", len(tl2["legs"]) == 2)
    fly_row, ground_row = tl2["legs"]
    check("fly leg departs 08:00, arrives 11:00 (3h)", fly_row["depart_clock"] == "08:00" and fly_row["arrive_clock"] == "11:00")
    check("ground leg departs after a 1h transfer buffer (12:00, not 11:00)", ground_row["depart_clock"] == "12:00")
    check("ground leg has no checkin_by (only flights get one)", ground_row["checkin_by"] is None)
    check("ground leg arrives 6h after its own departure (18:00)", ground_row["arrive_clock"] == "18:00")
    total_elapsed_min = _hhmm_to_min(ground_row["arrive_clock"]) - _hhmm_to_min(fly_row["depart_clock"])
    expected_elapsed_min = round((3.0 + 6.0) * 60) + round(1.0 * 60)   # both legs' hours + 1 transfer buffer
    check("total elapsed time matches leg hours + transfer buffer (reconciles with trip.py's hours_eff)",
          total_elapsed_min == expected_elapsed_min)

    # ---- timeline: day rollover past midnight is labelled, not silently wrapped to 00:00
    long_leg = [{"mode": "fly", "cost": 900.0, "hours": 18.0, "from": jfk, "to": den,
                "price_basis": "x", "verify_url": "https://x", "is_live": False, "segments": None}]
    tl3 = build_timeline(long_leg, date="2026-08-15", depart_local="20:00")
    r3 = tl3["legs"][0]
    check("an overnight leg rolls its arrival to the next calendar day",
          r3["depart_day"] == "2026-08-15" and r3["arrive_day"] == "2026-08-16")
    check("the rolled-over arrival clock is correct (20:00 + 18h = 14:00)", r3["arrive_clock"] == "14:00")

    # ---- timeline: a live leg's real segment times are used as-is, and a later estimate leg
    # re-anchors to the live leg's real arrival instead of the synthetic 08:00-based clock.
    live_leg = [{
        "mode": "fly", "cost": 241.5, "hours": 5.5, "from": jfk, "to": den, "is_live": True,
        "price_basis": "live fare — United Airlines", "verify_url": "https://x",
        "segments": [{
            "from": jfk, "to": den,
            "depart_at": datetime.datetime(2026, 8, 15, 14, 5),
            "arrive_at": datetime.datetime(2026, 8, 15, 16, 47),
            "carrier": "United Airlines", "flight_number": "UA1234",
        }],
    }, {
        "mode": "train", "cost": 75.0, "hours": 6.0, "from": den, "to": ase,
        "price_basis": "ground estimate", "verify_url": "https://y", "is_live": False, "segments": None,
    }]
    tl4 = build_timeline(live_leg, date="2026-08-15", transfer_buffer_h=1.0)
    live_row, next_row = tl4["legs"]
    check("live leg uses the real Duffel segment departure/arrival times, not the 08:00 synthetic anchor",
          live_row["depart_clock"] == "14:05" and live_row["arrive_clock"] == "16:47")
    check("live leg carries the real carrier + flight number", live_row["carrier"] == "United Airlines"
          and live_row["flight_number"] == "UA1234")
    check("live leg is flagged is_live=True, and the overall timeline is not example_day",
          live_row["is_live"] is True and tl4["example_day"] is False and tl4["any_live"] is True)
    check("the live leg's own checkin_by is ~2h before its REAL departure (12:05), not the synthetic anchor",
          live_row["checkin_by"]["clock"] == "12:05")
    check("the following ground leg re-anchors to the live leg's real arrival + transfer buffer (17:47)",
          next_row["depart_clock"] == "17:47")

    # ---- honesty: an estimate leg never claims to be live, and vice versa
    check("a synthetic leg's price_basis is whatever the caller supplied (this module narrates, doesn't invent)",
          fly_row["price_basis"] == "route-band estimate")
    check("empty legs list returns an empty, honestly-labelled timeline",
          build_timeline([]) == {"legs": [], "any_live": False, "example_day": True, "depart_local": "08:00"})

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (itinerary checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print("itinerary.py — import me, or run with --selftest")
