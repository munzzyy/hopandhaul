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
    "serve": "server",
    "geocode": "places",
    "weather": "weather",
    "duffel": "duffel",
}


def _usage() -> str:
    names = ", ".join(sorted(_SUBCOMMANDS))
    return f"usage: hopandhaul <subcommand> [args...]\nsubcommands: {names}"


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
        return trip.main(rest)
    if module_name == "go":
        from . import go
        return go.main(rest)
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
    return 2  # unreachable - every _SUBCOMMANDS value is handled above


if __name__ == "__main__":
    sys.exit(main())
