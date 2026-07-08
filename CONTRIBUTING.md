# Contributing

Thanks for looking at this. It's a small, single-purpose tool and contributions are welcome.
Here's how to get set up and what gets a PR merged quickly.

## Setup

```
git clone https://github.com/munzzyy/hopandhaul
cd hopandhaul
pip install -e .
```

No other dependencies to install. If your change needs a new pip package, that's a red flag,
not a to-do. See "zero dependencies" below.

## Running the tests

Every module carries its own offline self-test, no keys or network required:

```
python -m hopandhaul.trip --selftest
python -m hopandhaul.geo --selftest
python -m hopandhaul.server --selftest
python -m hopandhaul.emissions --selftest
python -m hopandhaul.duffel --selftest
python -m hopandhaul.geoapify --selftest
python -m hopandhaul.weather --selftest
python -m hopandhaul.providers --selftest
python -m hopandhaul.integrations.net
```

All of these run in CI on every PR (see `.github/workflows/ci.yml`). If you touch a module,
run its self-test locally before pushing, and add a case to it if you fixed a bug or added
behavior. A fix with no test attached to it is a fix that can silently regress.

Lint with `ruff check .`. CI runs it too.

## The browser build

`src/hopandhaul/ui/engine/` is a hand-ported JS copy of the estimate path in `trip.py`/
`geo.py`/`emissions.py`/`server.py`'s `plan()` — it's what runs the whole app client-side on
GitHub Pages, no server, no API keys. If you touch the reasoning in any of those Python
modules, port the change to the matching file under `engine/` too (`trip.py` -> `trip.js`,
`geo.py` -> `geo.js`, etc.) — a divergence there means the Pages build silently disagrees with
the CLI.

`tests/web_parity/` is the gate that catches drift: `cases.json` is a shared list of inputs,
`gen_fixtures.py` runs them through the real Python engine, `check.mjs` runs the same inputs
through the JS port and deep-equals the two.

```
python tests/web_parity/gen_fixtures.py
node tests/web_parity/check.mjs
```

If a case fails, the JS is wrong — fix it to match Python, never loosen the check. The classic
trap is rounding: Python's `round()` is round-half-to-even computed off the float's exact
decimal expansion, not `Math.round()`/`toFixed()` — see `engine/pyround.js` for the port and
why a naive epsilon check gets `round(2.675, 2)` wrong.

To preview the Pages build locally, stage the data JSON the workflow copies in at deploy time,
then serve `ui/` statically:

```
mkdir src/hopandhaul/ui/data
cp src/hopandhaul/data/*.json src/hopandhaul/ui/data/
python -m http.server 8899 --directory src/hopandhaul/ui
```

`src/hopandhaul/ui/data/` is gitignored — don't commit it; `src/hopandhaul/data/` stays the one
source of truth.

## Code style

- **Zero runtime dependencies, always.** This project's whole pitch includes "clone it and
  run it with nothing but Python." A PR that adds an entry to `pyproject.toml`'s
  `dependencies` needs a very strong reason and will get pushed back on hard.
- Stdlib only for anything that runs at request time. `urllib`, not `requests`.
- Match the existing style in the file you're editing before introducing a new one.
- No secrets in code, ever. Read them through `_secrets.py` (env var first, then
  `secrets.local.json`, which is gitignored). See `secrets.local.example.json`.
- Every endpoint returns `{"ok": bool, ...}` and never a raw exception string or traceback to
  the client. Log the real error server-side, return a generic message. See `docs/api.md`.
- Keep the server's security invariants intact: 127.0.0.1-only bind, the Host-header
  allowlist (DNS-rebinding guard), and the static-file whitelist (no path built from request
  input). If your change touches `server.py`, re-read that part before you submit.

## Voice

Write comments, docstrings, commit messages, and PR descriptions the way you'd explain it to
a coworker: plain and direct. Skip hedging filler and restating what the code already says in
prose. A comment should explain a constraint or a "why" the code itself can't, not narrate the
"what." If you're not sure, read a few existing docstrings in this repo (`trip.py`'s module
docstring is a good example) and match that.

## Gateway suggestions

`gateways.json` is a curated list of "this cheaper hub + this ground leg is actually a good
idea" facts. If you know a real one that's missing, open an issue with the
`gateway_suggestion` template — it asks for exactly the fields needed to turn it into a
one-line PR.

## Airport data corrections

`airports.json` holds one row per airport: IATA code, name, city, country, coordinates, and a
`hub` tier (1 = major/cheap/frequent, 2 = medium, 3 = small/regional/resort — few flights,
pricey). The tier drives real behavior: it's what makes the planner prefer DEN over a tiny
strip when both are "near" a click, and what the $200 rule is comparing against. A wrong tier
or a stale/duplicate row is a real bug.

If you spot one — an airport tiered wrong for its actual size, a missing IATA code, a
coordinate that's off enough to affect nearest-airport resolution — open an issue with the
`bug` label and say which `iata` row and what it should be instead, with a source (an
airline's own route map, an airport's published stats, anything better than a hunch). Small,
single-row corrections are also welcome as a direct PR; `geo.py --selftest` has a block of
regression tests for exactly this class of bug (see the LBG/TEB/PDK/FTW/OPF/BED entries) —
add a case there if your fix could silently regress later.

There's currently no automated importer script in this repo (`airports.json`'s own
`_README` field references refreshing a floor import from
[OurAirports](https://github.com/davidmegginson/ourairports-data) — that step happens
outside this repo when a full refresh is due, not as a script contributors run). Day to day,
treat `airports.json` as a hand-curated file you edit directly.

## Translations

The UI catalogs live in `src/hopandhaul/ui/i18n/`, one flat JSON file per language;
`en.json` is the source of truth. To fix a string: edit the value (never the key), keep
`{placeholders}` exactly as they are, and check the file still parses. To add a language:
copy `en.json`, translate the values, add an entry to the `LANGS` table in `ui/i18n.js`,
and say in the PR whether you're a native speaker. Screen-reader strings (`legend.sr`,
the `announce.*` keys) deserve extra care — someone will hear them read aloud.

## Pull requests

Keep them focused: one fix or one feature per PR. Describe what changed and why, not just
what. If it touches behavior, say what you tested and paste the self-test output.
