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

## Pull requests

Keep them focused: one fix or one feature per PR. Describe what changed and why, not just
what. If it touches behavior, say what you tested and paste the self-test output.
