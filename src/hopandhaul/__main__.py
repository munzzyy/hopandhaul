#!/usr/bin/env python3
"""hopandhaul CLI dispatcher - `hopandhaul <subcommand> ...` / `python -m hopandhaul ...`.

Thin routing layer only: every subcommand is a real module with its own argparse
parser and --selftest, reachable standalone via `python -m hopandhaul.trip` etc.
This dispatcher exists so `pip install` gives you one `hopandhaul` command instead
of making users remember five module paths.
"""
from __future__ import annotations

import sys

_SUBCOMMANDS = {
    "plan": "trip",       # trip.py owns the CLI historically named "plan a trip"
    "trip": "trip",
    "go": "go",           # one-shot zero-key trip plan: `hopandhaul go JFK TLL`
    "multicity": "multicity",   # order N cities into one trip: `hopandhaul multicity ...`
    "serve": "server",
    "geocode": "places",
    "weather": "weather",
    "duffel": "duffel",
    "dates": "dates",     # sweep a date window and report the cheapest one: `hopandhaul dates ...`
}

# One line each, in the order a new user should meet them rather than alphabetically.
_DESCRIPTIONS = [
    ("go", "plan one trip start to finish, origin and destination as codes or place names"),
    ("serve", "open the click-the-map UI on localhost"),
    ("dates", "sweep a window of departure dates and report the cheapest one"),
    ("multicity", "order N cities into one tour, cheapest hop by hop"),
    ("plan", "score routes you type in yourself (same command as `trip`)"),
    ("trip", "alias for `plan`"),
    ("geocode", "look up a place name and get coordinates"),
    ("weather", "destination forecast for a travel date"),
    ("duffel", "price real flights (needs a Duffel API key; everything else does not)"),
]


_UNDOCUMENTED = sorted(set(_SUBCOMMANDS) - {name for name, _ in _DESCRIPTIONS})
if _UNDOCUMENTED:  # adding a subcommand without a help line should break, not go quiet
    raise RuntimeError(f"_DESCRIPTIONS is missing a line for: {', '.join(_UNDOCUMENTED)}")


def _usage() -> str:
    width = max(len(name) for name, _ in _DESCRIPTIONS)
    lines = [f"  {name.ljust(width)}  {desc}" for name, desc in _DESCRIPTIONS]
    return "\n".join([
        "usage: hopandhaul <subcommand> [args...]",
        "",
        "Flies you into the cheap airport, then tells you honestly whether the ground leg",
        "is worth it. No API key needed for anything but `duffel`.",
        "",
        "subcommands:",
        *lines,
        "",
        "examples:",
        '  hopandhaul go JFK "Tallinn" --date 2027-06-15',
        "  hopandhaul-serve",
        "",
        "Every subcommand takes --help. Every module has an offline --selftest.",
    ])


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(_usage())
        return 0 if argv else 2

    cmd, rest = argv[0], argv[1:]
    module_name = _SUBCOMMANDS.get(cmd)
    if module_name is None:
        print(f"error: unknown subcommand {cmd!r}\n\n{_usage()}", file=sys.stderr)
        return 2

    if module_name == "trip":
        from . import trip
        return trip.main(rest, prog=f"hopandhaul {cmd}")
    if module_name == "go":
        from . import go
        return go.main(rest)
    if module_name == "multicity":
        from . import multicity
        return multicity.main(rest)
    if module_name == "server":
        from . import server
        return server.main(rest)
    if module_name == "places":
        from . import places
        return places.main(rest)
    if module_name == "weather":
        from . import weather
        return weather.main(rest)
    if module_name == "duffel":
        from . import duffel
        return duffel.main(rest)
    if module_name == "dates":
        from . import dates
        return dates.main(rest)
    return 2  # unreachable - every _SUBCOMMANDS value is handled above


if __name__ == "__main__":
    sys.exit(main())
