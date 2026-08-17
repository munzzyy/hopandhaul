#!/usr/bin/env python3
"""
check_cli_help.py - proves every `hopandhaul <sub> --help` prints a usage line you can copy.

Each subcommand builds its own argparse parser. Without an explicit prog= argparse infers one
from sys.argv[0], which for the installed console script is "hopandhaul" with the subcommand
name dropped, so `hopandhaul go --help` used to advertise

    usage: hopandhaul [-h] [--date DATE] ... [origin] [dest]

and anyone who copied that got `error: unknown subcommand '--date'`. This walks the real CLI
and fails if any usage line stops matching the command that produced it, and if the top-level
help ever stops listing a subcommand the dispatcher accepts.

Run:  python tools/check_cli_help.py
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from hopandhaul.__main__ import _SUBCOMMANDS  # noqa: E402


def run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, "-m", "hopandhaul", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    fails: list[str] = []

    code, top = run(["--help"])
    if code != 0:
        fails.append(f"`hopandhaul --help` exited {code}")
    for name in sorted(_SUBCOMMANDS):
        if f"\n  {name} " not in top:
            fails.append(f"top-level --help does not describe the {name!r} subcommand")

    for name in sorted(_SUBCOMMANDS):
        code, out = run([name, "--help"])
        if code != 0:
            fails.append(f"`hopandhaul {name} --help` exited {code}")
            continue
        first = out.splitlines()[0] if out.splitlines() else ""
        want = f"usage: hopandhaul {name} "
        if not first.startswith(want):
            fails.append(f"`hopandhaul {name} --help` prints {first!r}, expected it to start "
                         f"with {want!r} so the line can be copied and run")

    for f in fails:
        print(f"FAIL  {f}", file=sys.stderr)
    if fails:
        print(f"\n{len(fails)} CLI help problems", file=sys.stderr)
        return 1
    print(f"CLI help OK: {len(_SUBCOMMANDS)} subcommands, every usage line is copy-runnable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
