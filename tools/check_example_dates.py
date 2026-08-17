#!/usr/bin/env python3
"""
check_example_dates.py - fail CI when a copy-pasteable example date has gone stale.

Why this exists: every `--date YYYY-MM-DD` in the README, the docs and the module help text is
something a user will paste verbatim. The fare engine has no booking-lead-time curve for a date
that has already passed (geo.fare_date_multiplier returns a neutral 1.0), so a stale example
quietly demonstrates the *undated* code path in a tool whose entire pitch is being honest about
dates. The first person to notice is the user, and what they notice is "it says my date has
already passed" on the very first command in the README.

The same rot bit the test suite harder: three web-parity cases were pinned to 2026 dates and,
once those days passed, `date_far_out_aspen` and `date_close_in_aspen` were exercising the same
neutral path as each other and agreeing about it, so the gate stayed green while covering
nothing. That layer now uses relative offsets (see tests/web_parity/gen_fixtures.py). Prose
can't do offsets, so it gets this check instead.

Run:  python tools/check_example_dates.py
Exit: 0 = every example date is still in the future, 1 = at least one has rotted.
"""
from __future__ import annotations

import datetime
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Only flag dates attached to a date-taking CLI flag. Bare YYYY-MM-DD strings elsewhere in the
# prose are provenance ("BTS 2025Q1-2025Q4"), changelog entries, or deliberately-invalid test
# input, and none of those should move just because the calendar did.
EXAMPLE_DATE = re.compile(r"--(?:date|return-date|ret)[= ](\d{4}-\d{2}-\d{2})")

SEARCH = [
    ("README.md", None),
    ("docs", ".md"),
    ("src/hopandhaul", ".py"),
]

# How much runway an example needs. An example that expires next week is already a bug waiting
# to happen: CI would go red on a day nobody touched the repo.
MIN_DAYS_AHEAD = 30


def iter_files():
    for rel, ext in SEARCH:
        path = os.path.join(ROOT, rel)
        if os.path.isfile(path):
            yield path
            continue
        for dirpath, _dirs, names in os.walk(path):
            for name in names:
                if ext is None or name.endswith(ext):
                    yield os.path.join(dirpath, name)


def main() -> int:
    today = datetime.date.today()
    floor = today + datetime.timedelta(days=MIN_DAYS_AHEAD)
    stale = []
    checked = 0

    for path in sorted(iter_files()):
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                for raw in EXAMPLE_DATE.findall(line):
                    checked += 1
                    try:
                        d = datetime.date.fromisoformat(raw)
                    except ValueError:
                        stale.append((path, lineno, raw, "not a real calendar date"))
                        continue
                    if d < floor:
                        why = ("already in the past" if d < today
                               else f"expires in {(d - today).days} days")
                        stale.append((path, lineno, raw, why))

    if not checked:
        print("error: found no example dates at all - has the pattern or the layout changed?",
              file=sys.stderr)
        return 1

    if stale:
        print(f"{len(stale)} stale example date(s) - a user pasting these gets the undated "
              f"code path:", file=sys.stderr)
        for path, lineno, raw, why in stale:
            print(f"  {os.path.relpath(path, ROOT)}:{lineno}: {raw} ({why})", file=sys.stderr)
        print(f"\nBump them to at least {floor.isoformat()}.", file=sys.stderr)
        return 1

    print(f"example dates OK: {checked} checked, all at least {MIN_DAYS_AHEAD} days out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
