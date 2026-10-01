#!/usr/bin/env python3
"""
check_selftest_lists.py - fail CI when the self-test module lists drift apart.

ci.yml, README.md and CONTRIBUTING.md each spell out the modules to self-test, and the three
must stay identical. It also fails when a module defines selftest() and CI does not run it.

Run:  python tools/check_selftest_lists.py
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
MODULE_LINE = re.compile(r"^\s*python -m (hopandhaul(?:\.\w+)+)(?:\s+--selftest)?\s*$")


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def section(text: str, start: str, stop: str) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    j = text.find(stop, i + len(start))
    return text[i:j if j >= 0 else len(text)]


def modules(block: str) -> list[str]:
    return [m.group(1) for line in block.splitlines() if (m := MODULE_LINE.match(line))]


def modules_with_selftest() -> set[str]:
    found = set()
    pkg = os.path.join(SRC, "hopandhaul")
    for dirpath, _, files in os.walk(pkg):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as f:
                if not re.search(r"^def selftest\(", f.read(), re.M):
                    continue
            rel = os.path.relpath(path, SRC)[:-3]
            found.add(rel.replace(os.sep, "."))
    return found


def main() -> int:
    lists = {
        "ci.yml": modules(section(read(".github/workflows/ci.yml"), "Self-test every module", "\n  # ")),
        "README.md": modules(section(read("README.md"), "## Self-tests", "\n## ")),
        "CONTRIBUTING.md": modules(section(read("CONTRIBUTING.md"), "## Running the tests", "\n## ")),
    }
    fails = [f"{name}: no self-test list found" for name, mods in lists.items() if not mods]
    want_name, want = next(iter(lists.items()))
    for name, mods in lists.items():
        if mods and mods != want:
            missing = [m for m in want if m not in mods]
            extra = [m for m in mods if m not in want]
            detail = (f"missing {missing}" if missing else "") + (f" extra {extra}" if extra else "")
            detail = detail.strip() or "same modules, different order"
            fails.append(f"{name} differs from {want_name}: {detail}")
    for mod in sorted(modules_with_selftest() - set(want)):
        fails.append(f"{mod} defines selftest() but {want_name} does not run it")

    for f in fails:
        print(f"FAIL  {f}", file=sys.stderr)
    if fails:
        return 1
    print(f"self-test lists OK: {len(want)} modules, identical in {', '.join(lists)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
