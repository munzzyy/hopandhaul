#!/usr/bin/env python3
"""
check_i18n.py - CI gate for the locale catalogs in src/hopandhaul/ui/i18n/.

en.json is the schema (see i18n.js). The runtime falls back to English silently for
any key a catalog is missing, which is right for a visitor mid-week but wrong as a
steady state: a locale quietly missing keys means "46 languages" is quietly untrue.
This makes catalog drift a red build instead:

  - every catalog must be valid JSON: a flat object of non-empty strings
  - every catalog must carry exactly en.json's key set (no missing, no unknown keys)
  - every value must keep the same {placeholder} tokens as its English string, so a
    translation can't lose or typo the {money}/{count} slot t() substitutes into

Stdlib only, no arguments, exits non-zero listing every problem it found.
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

I18N_DIR = Path(__file__).resolve().parent.parent / "src" / "hopandhaul" / "ui" / "i18n"
PLACEHOLDER = re.compile(r"\{[A-Za-z0-9_]+\}")


def load(path):
    cat = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cat, dict):
        raise ValueError("top level is not an object")
    for key, value in cat.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key!r} is not a non-empty string")
    return cat


def main():
    problems = []
    en = load(I18N_DIR / "en.json")
    en_slots = {k: Counter(PLACEHOLDER.findall(v)) for k, v in en.items()}

    catalogs = sorted(p for p in I18N_DIR.glob("*.json") if p.name != "en.json")
    for path in catalogs:
        name = path.name
        try:
            cat = load(path)
        except ValueError as exc:
            problems.append(f"{name}: {exc}")
            continue
        except json.JSONDecodeError as exc:
            problems.append(f"{name}: invalid JSON — {exc}")
            continue

        missing = sorted(set(en) - set(cat))
        unknown = sorted(set(cat) - set(en))
        if missing:
            problems.append(f"{name}: missing {len(missing)} keys: {', '.join(missing)}")
        if unknown:
            problems.append(f"{name}: keys not in en.json: {', '.join(unknown)}")
        for key, value in cat.items():
            if key in en_slots and Counter(PLACEHOLDER.findall(value)) != en_slots[key]:
                problems.append(
                    f"{name}: {key} placeholders {sorted(PLACEHOLDER.findall(value))} "
                    f"!= en.json's {sorted(en_slots[key].elements())}")

    if problems:
        print(f"i18n check FAILED ({len(problems)} problems):")
        for line in problems:
            print(f"  {line}")
        return 1
    print(f"i18n check OK: {len(catalogs) + 1} catalogs, {len(en)} keys each, "
          "placeholders intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
