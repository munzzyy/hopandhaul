# DESIGN.md — travelcheap (working name: Hop and Haul)

This is the single design doc the build follows. It reconciles six line-level code reads
(`docs/_design/read-*.md`) and six research briefs (`docs/_design/research-*.md`) into one set
of decisions. Where the reads/research disagreed, this doc picks a side and says why. Nothing
here should be re-litigated mid-build — if something turns out to be wrong once we're in the
code, come back and edit this file, don't quietly drift from it.

> **Since superseded in places (2026-07-09, v0.7.0):** the Amadeus fallback discussed below was
> removed outright (Amadeus decommissioned its self-service portal July 2026); OpenWeather and
> Geoapify-as-the-only-geocoder gave way to keyless Open-Meteo and Photon defaults; ground legs
> gained real ferry corridors (`data/ferries.json`), a land/water grid, BTS fare anchors, and
> live Transitous schedules. Where this doc and the code disagree, the code and README are
> current; this file stays as the record of the original build's reasoning.

---

## 1. Product vision

**One-sentence pitch:** it flies you into the airport that's actually cheap, then tells you
honestly whether the train ride from there is worth it.

That's the whole product. Everything else — the map, the weather chip, the emissions number,
the flexible dates — is in service of that one decision. The decision itself is Cole's rule:
compare the cheapest direct flight against flying into a nearby cheaper hub and covering the
rest by train/bus/ferry/drive, and only recommend the split if it saves **$200 or more**
(configurable), unless the split is flatly better on both cost and time (dominance) or the
extra hours are worth it at the user's own stated value of time.

Nobody else automates this. Google Flights and Kayak do nearby-airport search with no
split-vs-direct verdict. Rome2Rio and Omio do multimodal routing with no threshold decision.
Trainline's SplitSave does a threshold-gated split decision but only within rail fares, never
across modes. The research pass confirmed this by direct search, not assumption
(`research-features.md`) — this gap is real and it's the whole reason to ship this as its own
product instead of a feature request to an existing tool.

What we are not: not a booking site, not a price-prediction tool, not a points/miles optimizer,
not a hidden-city fare finder. We show honest math and point you at the actual flight/train/bus
booking pages. Saying what we deliberately don't do is part of the pitch, not an omission.

The zero-install property — `git clone && python server.py`, no `pip install`, it just runs —
is a real, rare differentiator among travel tools and a stated constraint from Cole. Every
architecture decision below defaults to preserving it and says explicitly on the rare occasions
it's worth spending.

---

## 2. Target architecture

### 2.1 Package layout

Move from the current flat repo root to a `src/` layout. This is additive — every module keeps
working exactly as `python trip.py --selftest` does today; nothing about running the scripts
directly changes.

```
travelcheap/
  pyproject.toml              # dependencies = [] for the core package, always
  README.md
  LICENSE                     # Prosperity Public License 3.0.0
  SECURITY.md
  CONTRIBUTING.md
  .gitignore
  Dockerfile                  # optional, secondary run path
  src/
    travelcheap/
      __init__.py             # __version__ only
      trip.py                 # engine — the $200-rule math
      geo.py                  # spatial + fare/ground estimation model
      emissions.py            # NEW — CO2e estimate module
      airports.json           # ships as package data
      gateways.json
      _secrets.py
      integrations/
        __init__.py
        net.py                 # NEW — shared retry/backoff/JSON-fetch helper
        duffel.py
        geoapify.py
        weather.py
      flights.py               # provider-selection facade
      cli.py                   # NEW — thin `travelcheap <verb>` dispatcher
      server/
        __init__.py
        app.py                 # current server.py, the stdlib http.server app
        validate.py            # NEW — shared query-param validators
        ttl_cache.py           # NEW — shared TTL/LRU cache, replaces 1+2 hand-rolled copies
        ratelimit.py           # NEW — token-bucket limiter in front of outbound Duffel calls
        asgi.py                # NEW, optional — only importable if starlette/uvicorn present
  ui/                          # stays at repo root — not Python, edited constantly, no reason
                                # to nest it under the import package
    index.html
    app.js / state.js / api.js / map.js / results.js / search.js / format.js / theme.js
    sw.js, manifest.webmanifest
    styles/{tokens,layout,components}.css
    vendor/{leaflet.js,leaflet.css}
    icons/
  tests/
    test_selftests.py          # thin pytest wrapper around the existing --selftest suites
    test_trip.py / test_geo.py / test_emissions.py / ...
  docs/
    DESIGN.md                  # this file
    api.md                     # hand-written endpoint reference
    _design/                   # the read-*/research-* inputs to this doc, kept for history
  .github/
    workflows/ci.yml
    ISSUE_TEMPLATE/{bug_report.md, gateway_suggestion.md}
    PULL_REQUEST_TEMPLATE.md
```

`providers.py` (the legacy Amadeus adapter) gets collapsed, not carried forward as-is: route its
key-reading through `_secrets` like every sibling module, strip its ~100 lines of standalone-CLI
scaffolding, and fold what's left into `integrations/` as a thin fallback — or drop Amadeus
entirely if Duffel alone is judged sufficient. Right now it's in an awkward "is this a supported
CLI or an internal fallback" limbo (`read-server-api.md`); pick one.

`airports.json`/`gateways.json` move inside the package and get read via
`importlib.resources.files("travelcheap") / "airports.json"` instead of `__file__`-relative
paths, so a real `pip install` (inside a zipped wheel) resolves them correctly, not just a
repo checkout. Stdlib since Python 3.9 — no new dependency.

### 2.2 Backend/API decision: keep stdlib `http.server` as the only default; ASGI is opt-in, not built until wanted

**The call: `http.server` stays. Do not add Starlette/uvicorn/Flask/FastAPI as a runtime
dependency.** Three independent passes over this codebase (`read-server-api.md`,
`research-backend-arch.md`) converged on the same answer from different angles, so this isn't
a close call:

- The workload is I/O-bound (waiting on Duffel/Geoapify/OpenWeather), and the existing
  `ThreadPoolExecutor` fan-out inside `plan()` already captures the concurrency win async would
  otherwise sell. There's no throughput problem an ASGI server would solve here.
- The two hard security problems for "serve this safely on localhost" — DNS-rebinding via a
  Host-header allowlist, and path-traversal-free static file serving — are **already solved
  correctly** in `server.py`. Swapping frameworks means re-deriving and re-testing both from
  scratch for zero functional gain.
- Every dependency added is something a user has to trust and something that has to be
  security-audited before it ships (per Cole's standing security posture). Zero runtime
  dependencies means zero of that.
- `http.server`'s own docs say plainly it's "not suitable for production" — no TLS story, no
  keep-alive tuning, no defense against a slow client trickling bytes to hold a thread open.
  That's a real, honest limitation the moment this needs to sit on a public IP instead of
  `127.0.0.1`. Pretending otherwise would be dishonest.

**The resolution:** keep `server/app.py` as the one documented, zero-dependency "quick start"
path — this is what the README points to, unconditionally. Separately, extract the parts of
`plan()`/the endpoint handlers that don't touch `Handler`/HTTP state into plain functions of
`(params) -> (status, body_dict)` — most of this work is nearly free since `plan()` already
takes plain args and returns a plain dict. Then add `server/asgi.py` as a genuinely optional
module, only importable with `pip install travelcheap[asgi]` (Starlette + uvicorn), documented
as "the hosted/production mode" with its own README section on what it buys (TLS via reverse
proxy, real request timeouts, rate-limiting middleware) and what it costs (two more packages).
**Don't build `asgi.py` speculatively** — write down that the lane exists and is available, but
only actually build it once Cole confirms he wants a real hosted-beyond-localhost deployment.
Building a second server implementation for a use case that may never materialize is wasted
maintenance surface.

### 2.3 Frontend decision: no build step, split into ES modules

**The call: keep vanilla JS, zero bundler, zero `npm install`. Split the current 392-line
`index.html` script block into ~8 small ES modules loaded via native `<script type="module">`
and relative `import`.** This mirrors the backend call exactly — same virtue, one layer up.

Why no Vite/webpack/esbuild: native ES module support is universal in evergreen browsers now.
Vite's real value (fast HMR across a large component tree, production minification/chunking) is
irrelevant here — this is one map view, a form, and a results panel; there's no component tree
to tree-shake. Adding a JS build step to a project whose stated differentiator is "no account,
no hosted backend, it just runs" is self-defeating — a user with Python but not Node hits a wall
before seeing the map (`research-frontend-arch.md`). If a genuinely complex second screen shows
up later (a drag-reorder multi-city builder, say), that's the moment to revisit — not now.

TypeScript-without-a-build-step: JSDoc annotations checked by `tsc --noEmit` as a **dev-only, CI-
only** gate (never shipped, never required to run the app). Catches real classes of bug already
found in the current file (the `modeEmoji()` missing a `fly` key, `draw()`/`render()`
independently recomputing the same value) without adding a runtime dependency.

Leaflet stays, MapLibre GL is not worth the switch — this app draws a handful of markers and 2-4
great-circle arcs, a workload where Leaflet's DOM/Canvas renderer is faster than MapLibre's WebGL
pipeline needs to be to pay for itself, and MapLibre would add a WebGL dependency (broken on some
locked-down corporate browsers) for zero user-facing benefit.

Module split (replacing the one file):

```
ui/app.js        entry point, wires modules, owns top-level state (rec computed once, shared)
ui/state.js      URL <-> state serialization, localStorage persistence
ui/api.js        fetch wrappers (config/geocode/nearest/plan/flexible), AbortController
ui/map.js        Leaflet init, draw(), arc(), gateway pins
ui/results.js    render(), option cards, recommendation card, aria-live updates
ui/search.js     autocomplete: debounce, keyboard nav, ARIA listbox
ui/format.js     fmtMoney, fmtH, modeEmoji — pure functions (fixes the dead line-369 ternary here)
ui/theme.js      light/dark toggle, prefers-color-scheme, view-transitions
ui/sw.js         service worker (PWA)
```

PWA (manifest + hand-rolled ~40-line service worker, no Workbox) is achievable with zero build
tooling and is a should-ship, not a stretch — see §3.

### 2.4 Deliverables

1. **Library** — `travelcheap` importable package (`trip.evaluate`, `geo.estimate_flight`,
   `emissions.estimate`, etc.) via `pip install -e .` or straight `pip install travelcheap` once
   published.
2. **CLI** — `travelcheap plan/serve/geocode/weather/selftest` subcommands via
   `[project.scripts]`, wrapping the existing per-module `main()`s. The standalone
   `python trip.py --selftest`-style invocations keep working unmodified — the CLI dispatcher is
   additive polish, not a replacement.
3. **API server** — `server/app.py`, stdlib `http.server`, the documented default.
4. **Web app** — `ui/`, served by the same server, no separate deploy step.
5. **Docker** — one `Dockerfile`, explicitly secondary in the README (`docker run` as "here's
   another way if you'd rather," never the primary quick-start).
6. **PWA** — manifest + service worker, installable, offline fallback banner (plans need live
   data; this isn't a note-taking app that's naturally offline-first).
7. **Optional ASGI hosted mode** — documented lane, built only on request (§2.2).

---

## 3. Feature roadmap — ranked

### MUST (ship before this can be called done)

| Feature | Why | Effort |
|---|---|---|
| Fix the two dead `ROUTE_MULT` keys (`EU,CN`→`CN,EU`; `EU,AF`→`AF,EU`) + load-time sort assertion | Currently mispricing real routes in the exact opposite direction their comments intend — a London→Marrakech query gets priced as thin-competition when the code means saturated-charter. Silent, wrong, cheap to fix. | XS |
| `main()`/CLI error handling (`trip.py`, `duffel.py`, others) — no raw tracebacks to a user-facing CLI | A negative-cost option, malformed JSON, or a bad leg string currently crashes with a full Python stack trace. This is about to sit behind a real frontend where garbage input is guaranteed. | S |
| Validate `--threshold >= 0` (the one numeric flag missing a guard) | Three of four numeric flags are validated; this one silently produces a nonsensical report line ("saves >= -$100"). One-line fix sitting right next to the existing checks. | XS |
| Enforce `nlegs >= 2` for anything routed through `sugar_split` | The product's entire identity is correctly telling a split from a direct. Right now a malformed `--split` call silently downgrades to a 1-leg "direct" labeled as if it were a split, with no error. This is a correctness bug in the core thesis, not a nit. | S |
| `net.py` shared HTTP-JSON-with-retry/backoff helper, used by all 4 provider modules | No retry logic exists anywhere today. A single transient 429/5xx from Duffel silently and permanently downgrades a plan to "estimate mode" with no visible reason. This is the single biggest reliability gap found across the whole integrations layer. | M |
| Narrow `_price_flight`'s bare `except Exception` to network/HTTP-shaped exceptions | Currently a real bug in the normalization code presents identically to "Duffel is down" — forever, silently, to every user. | S |
| Fix `/api/geocode`'s info-leak (`f"{type(e).__name__}: {e}"` returned to the client) — apply `/api/plan`'s log-server-return-generic pattern everywhere | One inconsistent endpoint in an otherwise-good pattern. Cheap, and worth fixing before this is public. | XS |
| Cap `nearest_airport()` with a real `max_km` + tiered warning (soft 120-400km, hard fail beyond ~600-700km) | Confirmed: a click in Mongolia currently silently resolves to Beijing, 1,163 km away, and the whole plan is built on that wrong premise with only a mild "last mile isn't included" note. This is the data layer's one real bug, not a coverage gap. | S |
| Import OurAirports as the floor for `airports.json`, keep the 731 curated rows as overrides | Closes the Central Asia / interior Africa / Pacific-islands gaps in one pass, and reduces how often the nearest-airport cap above actually triggers. One-time offline script, stdlib `csv`/`json`, no runtime dependency. | M |
| Responsive layout (bottom sheet on mobile, real grid on desktop) | Confirmed zero `@media` rules exist today; two 300-340px fixed panels don't fit on a 375px phone. This is the single biggest real-world-usage gap in the current UI — a travel tool that's unusable on the exact device most "check a flight price" traffic comes from. | M |
| Accessibility pass (`aria-live` on results/spinner, ARIA roles on autocomplete + mode toggle, real focus rings, text alternatives to emoji-only legend) | Zero ARIA anywhere today. This is a WCAG 2.2 AA gap, not a polish item, and it's cheap relative to its blast radius. | M |
| Shareable trip URLs (`history.replaceState` + a visible "Copy link" button) | Zero URL state today — reload the page, lose everything. This tool's best distribution channel is "look what I found, click this link," and it's a near-free reuse of `planTo()`'s existing param-building logic. | S |
| Fix the visual hierarchy of the savings number | The entire pitch is "you could save $400" and that number is currently buried in 12.5px body text. Biggest typographic element on the card, one CSS/markup change. | S |
| Security: SECURITY.md + document the existing good posture (Host-header guard, 127.0.0.1-only bind, no path traversal, no secrets to the browser) | This is genuinely already well-built; say so explicitly and give a real vulnerability-reporting channel before this goes public. | XS |
| `pyproject.toml`, `src/` layout, `[project.scripts]` entry points, `dependencies = []` | Makes this installable/forkable without touching runtime behavior or adding a dependency. Table stakes for "genuinely excellent public repo." | M |
| CI (GitHub Actions: ruff lint + run every existing `--selftest`) | Zero CI exists today on a clean-slate repo. Cheap, matches the existing self-test discipline, gives a real green badge. | S |
| README (hook, GIF, quick start, the actual $200 rule stated plainly, honest scope) + LICENSE (Prosperity) | Nothing exists yet. This is the single highest-leverage piece of the whole launch — see §6. | M |

### SHOULD (clear, high-value, do soon after MUST)

**Done:** Emissions ("cheapest vs greenest") — `emissions.py` + `co2e_kg` per option +
`result.greenest`. Wired through `/api/plan` and the results panel; see `docs/api.md`. Still
purely informational, per the original plan below — never auto-recommended over the cheapest.

| Feature | Why | Effort |
|---|---|---|
| Flexible-date sweep (`/api/plan/flexible`, ±2-3 days) | Fare timing routinely swings past the $200 threshold on its own. A single fixed-date check can hide a qualifying split that's one day-shift away — this is a correctness gap in the core rule, not just a nice-to-have. 100% reuse of the existing `plan()`/cache/concurrency path. | S-M |
| "Cheapest way to get anywhere" explore mode | Nearly free — `geo.py`'s gateway-discovery + estimate engine already computes everything needed; this is a frontend reframing ranked by post-Cole's-rule total cost instead of raw flight price, a framing nobody else can copy without building the split-decision engine underneath it. | M |
| Quantified self-transfer / connection-risk signal per split | Every split this tool recommends is, by construction, a self-managed connection — exactly the risk category Kiwi.com built an insurance product around. Currently just a static cautions paragraph; turning it into a real `refundable`/tight-vs-comfortable buffer signal is what makes "we recommend a split" trustworthy with real money. | M |
| Gateway compare view (2-3 hubs side by side) | Backend already computes multiple candidate gateways; today they're just flat map pins. Pure frontend reframing, pairs naturally with the explore mode above. | S |
| `net.py` retry work paired with: shared time budget across the concurrent gateway fan-out + a token-bucket rate limiter in front of outbound Duffel calls | Six threads each doing sequential create+poll calls can hold up to a minute combined if Duffel is slow; nothing throttles a burst of different-destination clicks against Duffel's real 120 req/60s limit. | S-M |
| Shared `TTLCache` class, applied to the existing offer cache (fix the O(n log n) evict-half + widen the key to include `cabin`/`nonstop`) plus new geocode and weather caches | Three independent hand-rolled dict+lock patterns today (one existing, two needed); one tested class instead. Also closes a real stale-cache landmine waiting for the first cabin-class UI toggle. | S |
| Baggage + fare-rules surfaced from Duffel offers | Duffel already returns this; `_parse_offer` discards it today. A "cheapest" fare needing a $60 bag add-on isn't actually cheapest — this undermines the tool's own honesty claim. | S |
| Fix `region_of`'s real geographic gaps (Iceland/Faroes/Greenland, Russia/Central Asia, Pacific islands, the Vladivostok-mis-tagged-as-Japan bug) | ~20-30% of world land area/population silently falls into an undifferentiated `OTHER` bucket that loses all region-aware pricing/ground logic. The Vladivostok case is worse than a gap — it actively applies wrong assumptions. | S-M |
| Smooth `pick_ground_mode`'s cliff-edge discontinuities | A 2km difference in destination can currently flip the recommended split (both cheaper AND slower, or the reverse) purely from which side of a hard distance boundary it lands on. Directly affects the reliability of the $200 rule at the margins. | S |
| PWA (manifest + hand-rolled service worker, installable) | Genuinely additive, zero build tooling needed, no risk to the zero-install property. | S |
| `docs/api.md` + formalized error contract (`{ok, error, code}` uniformly) | Document the API surface once as the source of truth instead of five slightly-different ad hoc implementations a contributor has to reverse-engineer. | S |
| Collapse `providers.py` (route through `_secrets`, strip standalone-CLI scaffolding, or drop Amadeus) | Currently in an awkward half-supported state; the fix is cheap and removes a real inconsistency (its `have_keys()` disagrees with every sibling module about where secrets live). | S |
| Repo polish: CONTRIBUTING.md, issue templates (bug report + a `gateway_suggestion.md` template specific to this repo's real contribution shape), PR template, topics | `gateways.json` is designed to grow via curated submissions — a structured template turns "I know a cheap hub trick" into a mergeable PR instead of a vague issue. | S |

### COULD (real ideas, correctly lower priority — don't block launch on these)

| Feature | Why lower priority | Effort |
|---|---|---|
| Multi-city / open-jaw itineraries | Needs real design work on what "baseline" means for 3+ cities — the $200-rule doesn't generalize cleanly without rethinking it. Worth its own future design doc, not a bolt-on. | L |
| Watched-route alerts on split savings | Needs background polling infra (scheduler, notification channel, persistence) that doesn't exist in a stdlib-only, no-build-step tool. Good v2 direction once the core is otherwise done. | L |
| Estimate-only static demo on GitHub Pages | Real value for launch credibility, but sequence after the core repo is live and stable — not a launch blocker. | M |
| Ground-leg split-ticketing lookup (SplitSave-style, applied to the ground leg itself) | Interesting, validated-by-analogy idea, but needs a segment-level rail fare data source that doesn't exist publicly. Park as a research item. | — |
| Timezone data (`timezonefinder`, offline-only, build-time) | The one place a pip dependency is justified (no credible stdlib alternative), but keep it out of the runtime path entirely — offline import-time only. | S |
| Self-hosted Valhalla / OSRM as an offline calibration job to regenerate `ROAD_WINDING`/ground tables | Real accuracy win, but explicitly an offline batch job against demo-server rate limits, never a runtime dependency. Do this as a periodic calibration pass, not a v1 feature. | M |
| Optional ASGI hosted mode (§2.2) | Written down as available; build only if Cole actually wants a beyond-localhost deployment. | M |
| A tiny serverless proxy for a genuinely live public demo | Only worth it once there's real interest (stars/issues/traffic) — introduces an ongoing hosting/cost/rate-limit surface that isn't justified pre-launch. | M |

**Deliberately not building, ever, unless this doc changes:** price-prediction/buy-or-wait
(needs a historical fare corpus this project doesn't have — faking it would be actively
dishonest), full booking/payments (this is a planner, not an OTA — booking is a liability, not
a feature), loyalty/points search (different audience, different currency, off-thesis),
hidden-city/Skiplagged-style ticketing (a fundamentally different, ToS-violating risk category
that would muddy this product's "honest, transparent, deterministic-math" identity).

---

## 4. Code-quality + de-AI plan, by file

This is what "less AI, more optimized" means concretely, file by file. Every item below was
confirmed by running the code, not just reading it.

**`trip.py`**
- Delete `GROUND_MODES` and `CONNECTION_BUFFER_H` (both genuinely dead), or better: actually
  wire `GROUND_MODES` into `scale_leg_cost` as a validation/warning check — right now a typo'd
  mode (`"flght"`, `"walk"`) silently falls into the per-person cost branch with zero warning.
- Rename the private-by-convention `trip._num` to a public `trip.num()` — `providers.py`
  already imports and calls it directly, so the "private" name is a lie the moment anything
  else in the repo depends on it.
- Replace `o is baseline` / `o is recommended` identity comparisons with a stable key (index or
  synthetic `_id`) — currently works only because `rows` are built from the same list objects
  `baseline` was selected from; any future refactor that copies option dicts (e.g. a Pareto pass)
  breaks every `is_baseline` tag silently, with no exception.
- Surface `baseline_kind` in `format_report()` when there was no real direct in the input — right
  now the printed report always says "Rule: ... vs the cheapest direct" even when there wasn't
  one, which is actively misleading copy about the product's own core claim.
- Replace `_fmt_hours`'s manual float rounding + `if mm == 60` patch with `datetime.timedelta`,
  which handles the boundary correctly by construction.
- Emit a `"why": [...]` narrative list in the JSON payload, generated once server-side from the
  same logic `format_report()` already has — right now a frontend consuming `--json` has to
  reimplement the "why was this recommended" sentence logic by hand in JS, and it will drift.

**`geo.py`**
- Fix the two dead `ROUTE_MULT` keys (see MUST list) and add
  `assert all(k == tuple(sorted(k)) for k in ROUTE_MULT)` at module load so this class of bug
  can never silently reappear.
- Close the `region_of` gaps (Iceland/Faroes/Greenland, Russia/Central Asia, Pacific islands)
  and fix the Vladivostok-classified-as-Japan bug (see SHOULD list).
- Smooth `pick_ground_mode`'s hard distance-threshold cliffs.
- Add a `by_iata` dict index alongside the existing airport cache — free performance headroom,
  currently an O(n) scan called in a loop from `curated_gateways`.
- Validate travel dates aren't in the past at the point they enter the system, rather than
  `fare_date_multiplier` silently returning a neutral multiplier for a meaningless past date.

**`server.py` / `server/app.py`**
- Narrow `_price_flight`'s bare `except Exception` (see MUST).
- Widen the offer-cache key to include `cabin`/`nonstop` before either becomes a UI toggle.
- Rename `_price_flight` vs the `_price` closure inside `plan()` — two very similarly named
  functions with different signatures, ~30 lines apart; pick distinct names.
- Fix `/api/geocode`'s info-leak (see MUST).
- Resolve the 25s-upstream-vs-15s-handler timeout asymmetry explicitly rather than leaving it
  implicit.
- Add `/healthz` returning `{"ok": true, "version": ...}` unconditionally, distinct from
  `/api/config`.

**`duffel.py` / `geoapify.py` / `weather.py` / `providers.py`**
- Route all key reads through `_secrets` (currently `providers.py` reads `os.environ` directly,
  the one real inconsistency among the four provider modules).
- Consolidate `_http_json`-shaped helpers into `integrations/net.py` (see MUST) — four
  near-identical private copies today.
- Lift `weather.py`'s exception-handling pattern (catch `HTTPError`/`URLError`/`ValueError`/
  `KeyError` around each independent network call, return partial/`None` rather than raising)
  into `duffel.py` and `geoapify.py`'s own functions, not just at the caller.
- Fix the local-noon timezone bug in `weather.py`'s forecast picker (string-slices UTC `dt_txt`
  assuming it's local time — subtly wrong for a destination many timezones from UTC).
- Date-stamp the FX table (`duffel.py`) with an "as of" constant so staleness is grep-able.

**`ui/index.html` (splitting into modules per §2.3)**
- Kill the dead, self-contradicting ternary at line 369 and add `fly: '✈️'` as a first-class key
  in the `modeEmoji` map instead of special-casing it after the fact.
- Stop `draw()` and `render()` independently recomputing `rec` — compute once in `app.js`, pass
  to both.
- Cache the seven `planTo()` input element references once at module init instead of re-querying
  the DOM on every call.
- Give all 7 status values from `trip.py` (not just 4) a styled, plain-English label —
  `pricier_faster`/`worse` currently leak raw implementation vocabulary to end users.
- Add `AbortController` so a new `planTo()` call cancels the previous in-flight request instead
  of racing it (last-resolved-wins today, should be last-requested-wins).

---

## 5. Security plan — must be done before this is public

Confirmed via direct code walkthrough (`read-server-api.md`), not assumption:

1. **State the "no SSRF" claim explicitly, in writing, in the security docs.** Every outbound
   network call in the codebase targets a hardcoded host literal (Geoapify, Duffel, Amadeus,
   OpenWeather) — no code path ever builds a request URL/host from client input. This is a real,
   checkable claim and saying it plainly reads far stronger to a security-conscious reviewer
   than silence. The one thing that would introduce SSRF — a future "point this at your own
   geocoder" option — needs the standard guards (block RFC1918/loopback/link-local, no blind
   redirect-following) revisited if it's ever built.
2. **Fix the `/api/geocode` info-leak** — stringified exception returned to the client instead
   of following `/api/plan`'s log-server-side-return-generic pattern. Apply that pattern
   uniformly across every endpoint before publish.
3. **Narrow the bare `except Exception` in `_price_flight`** so a real bug in the normalization
   code doesn't permanently and silently masquerade as "provider is down" (this is a
   reliability finding as much as a security one, but a maintainer's ability to trust their own
   error logs is a security property).
4. **Keep the Host-header/DNS-rebinding guard and the zero-traversal static-file dict exactly
   as-is** through any refactor — these are the two hardest, most-correctly-solved parts of the
   current server and the easiest to accidentally regress while restructuring into `src/`.
5. **Keep `127.0.0.1`-only binding as the default** for the stdlib server; the moment a hosted
   mode ships (§2.2's ASGI lane), the security tradeoffs of exposure beyond localhost need their
   own written section — TLS termination via reverse proxy, real rate limiting, no default
   `0.0.0.0` bind without an explicit opt-in flag.
6. **No secrets ever reach the browser** — `/api/config` already only returns booleans and
   provider names; preserve this invariant explicitly as a rule for any new endpoint.
7. **`_secrets.py`'s env-first-then-gitignored-file pattern stays unchanged** — it's already
   correct. Ship a `secrets.local.example.json` template (placeholder values only) so a new
   contributor can see the expected shape without reverse-engineering four call sites.
8. **Add basic rate limiting / a request-size-aware posture note** before any hosted mode is
   considered — fine as-is for single-user `127.0.0.1`, a real gap the moment this runs on a
   shared network or behind a reverse proxy (a legitimate "run this on my home server" ask).
9. **`SECURITY.md`**: private reporting channel (GitHub Security Advisory or email, never a
   public issue), explicit scope (this repo's server/key-handling/geocode/fare-fetch code —
   NOT upstream Duffel/Geoapify/OpenWeather vulnerabilities), a one-line "never paste a real key
   into an issue" reminder, and an honest solo-maintainer response-time expectation instead of a
   corporate SLA nobody's going to hold to.

---

## 6. Repo + publish plan

**Name — primary: `hopandhaul`.** Two words that literally describe the product's mechanic (hop
to a cheap hub by air, haul the rest by ground) — plain English, memorable, explains itself
faster than almost any travel-tool name checked in research. No exact GitHub collision found in
the research pass (verify with a live `gh api repos/munzzyy/hopandhaul` GET before locking it
in — a 404 confirms the slug is actually free).

**Alternates, in preference order:** `groundswitch` (plainer, more "serious infra tool," good
fallback if "Hop and Haul" tests as too cute), `farehop` (short, CLI-friendly, slight risk of
reading like a fare-alert tool instead of a routing tool), `waypoint200` (bakes the $200 rule
into the name — strong hook, but ties the brand to a number that's actually a configurable flag;
use only if the "$200 rule" framing becomes the core marketing angle), `layoverlogic` (weakest
fit — "layover" technically means a connection within one itinerary, not a deliberately split
pair of separate tickets, and this audience will notice).

**License: Prosperity Public License 3.0.0** (free for noncommercial use, commercial use by
paid license). The original launch shipped MIT for maximum adoption and zero legal friction;
it was later relicensed to Prosperity to keep hopandhaul noncommercial and owner-held. The
`LICENSE` file at the repo root is authoritative.

**README outline** (the single highest-leverage artifact in the whole launch):
1. One-line hook, above the fold, verbatim or close to: *"Flies you into the airport that's
   actually cheap, then tells you honestly whether the train ride is worth it."*
2. 3 badges max, each a real signal: CI status, a license badge, a "zero dependencies" badge (rare
   and legitimately differentiating).
3. A 10-15s GIF of the actual click-to-plan flow, immediately after the hook, before any prose —
   this is the single highest-conversion element. Capture it **after** the frontend's savings-
   number visual-hierarchy fix ships, not before (capturing against the current UI would undersell
   the product).
4. Quick start in 3 copy-pasteable steps, zero install friction: `git clone`, `python server.py`,
   open `localhost:8770`. No `pip install -r requirements.txt` in this block — if any of the
   three live integrations need a dependency at all, that has to be confirmed false before this
   line ships, because a promised zero-dependency quickstart that then needs `pip install`
   undermines the pitch on line one.
5. The actual rule, stated plainly with a worked number: "Recommend the split only when it saves
   $200 or more, unless it's strictly cheaper and not slower." This is the single most quotable
   paragraph in the whole project.
6. Honest features list — deterministic engine, live fares via Duffel, group-aware, round-trip-
   aware, gateway discovery, weather, emissions estimate. No "AI" claims anywhere — there's no
   LLM in the runtime path and claiming one would be dishonest and invite exactly the kind of
   skepticism that sinks trust with the technical (HN-adjacent) audience this needs.
7. Short architecture section — one paragraph + a short file list (`trip.py` the math, `geo.py`
   the estimation model, the three integration adapters, `server/app.py` the localhost app).
   Reviewers deciding whether to trust a fare-estimation tool want to see the reasoning is
   inspectable, not a black box.
8. Self-test section, marketed not buried: "every module has an offline self-test, no keys
   required, run them all in under a second." This is unusually strong for a project this size.
9. Contributing / License / Security footer — one line each, linking out to the dedicated files.

**CI:** one GitHub Actions workflow — `ruff check` for lint, then run every module's
`--selftest` (`trip.py`, `geo.py`, `duffel.py`, `geoapify.py`, `weather.py`, `server/app.py`),
matrixed across two Python versions (e.g. 3.11 and current stable). Green badge in the README
the moment it passes once.

**SECURITY.md:** per §5 item 9 above.

**Demo option:** an estimate-only static demo on GitHub Pages (the `geo.py` estimator is pure
stdlib math, zero external calls, genuinely portable to a small client-side or Pyodide build) —
labeled clearly as "estimates only, clone the repo for live fares." Sequence as a v1.1 addition
once the core repo is live and stable, not a launch blocker. Do not build a keys-holding
serverless proxy for a fully-live public demo unless real interest (stars/issues/traffic)
justifies the ongoing hosting/cost/rate-limit exposure.

**Topics:** `travel`, `travel-planner`, `trip-planner`, `flights`, `python`, `cli`, `duffel`,
`geoapify`. A PR to `sindresorhus/awesome` or a relevant sub-list, sequenced after initial
traction (brand-new zero-star repos get rejected on sight).

**Launch sequencing:** Show HN is the right-fit channel — a technically interesting,
deterministic, zero-dependency, real-math tool aimed exactly at an audience that will appreciate
"the numbers are real math." Product Hunt is a weaker fit (skews SaaS/B2B). Ship the repo
complete → let it sit a few days for organic issues/typo fixes → Show HN once the estimate-only
demo exists to click through to → awesome-list PR once there's traction.

---

## 7. Definition of done / can't-get-better checklist

The completeness-critic grades against this list. Every item must be true, not "mostly true."

**Correctness of the core rule**
- [ ] All `trip.py` self-tests pass, including the exact-$200-boundary case.
- [ ] `sugar_split` cannot silently produce a 1-leg option tagged as a split; malformed split
      input raises a clean error instead.
- [ ] `baseline_kind` is surfaced in the printed report whenever there was no real direct
      flight in the input — the rule text never implies a comparison that didn't happen.
- [ ] The two dead `ROUTE_MULT` keys are fixed and a load-time sort assertion guards against
      regression.
- [ ] `nearest_airport()` has a real `max_km` ceiling with a tiered warning; no click on Earth
      silently resolves to an airport 900+ km away with only a mild caveat.

**Reliability**
- [ ] Every CLI (`trip.py`, `duffel.py`, etc.) fails with a clean, specific error message on bad
      input — zero raw Python tracebacks reach a user-facing surface.
- [ ] `net.py`'s retry/backoff is live in all four provider adapters; a single transient 429/5xx
      no longer silently and permanently downgrades a plan to estimate-only.
- [ ] Every endpoint follows one error contract (`{"ok", "error", "code"}`) — no endpoint leaks
      a raw exception string to the client.
- [ ] A shared time budget bounds the concurrent gateway fan-out; a slow provider degrades
      gracefully instead of holding every thread for its full individual timeout.

**Frontend**
- [ ] The app is genuinely usable on a 375px-wide phone viewport — no fixed-width panel exceeds
      the screen width, and there's a real mobile layout (bottom sheet or equivalent), not zero
      `@media` rules.
- [ ] `aria-live` on the results panel and loading states; ARIA roles on the autocomplete and
      mode toggle; visible, WCAG 2.2-compliant focus rings; no meaning conveyed by color or
      emoji alone without a text equivalent.
- [ ] A plan's full state round-trips through the URL — reload the page or send the link to
      someone else and the same plan renders without re-entering anything.
- [ ] The dollar savings figure is the single largest typographic element on the recommendation
      card.
- [ ] Loading, empty, and error states are distinct and real: a race-free request lifecycle
      (`AbortController`), a clear map on error instead of stale markers lingering, and a
      malformed response never throws an uncaught exception into a blank panel.

**Data**
- [ ] `airports.json` coverage includes at minimum Central Asia, interior Africa, and the
      Pacific islands (via the OurAirports import), not just the original 731 hand-curated rows.
- [ ] `region_of`'s confirmed gaps (Iceland, Russia/Central Asia, Pacific islands, the
      Vladivostok mis-tag) are closed.

**Security**
- [ ] SECURITY.md exists, states the "no SSRF possible" claim explicitly with the reasoning,
      and gives a real private-reporting channel.
- [ ] No endpoint leaks exception internals to the client; the Host-header guard and
      zero-traversal static serving survive the `src/` restructure unchanged.
- [ ] `secrets.local.json` stays gitignored; a `secrets.local.example.json` template exists with
      placeholder values only.

**Packaging / repo**
- [ ] `python server.py` (or the packaged equivalent) still runs with zero `pip install` for
      the core engine — the zero-dependency quick-start claim in the README is literally true,
      verified by testing on a clean environment, not assumed.
- [ ] `pip install -e .` also works and exposes `travelcheap`/`travelcheap-serve` on PATH.
- [ ] CI is green: lint passes, every module's `--selftest` passes, on at least two Python
      versions.
- [ ] README has the hook, the GIF, the 3-step quick start, the rule stated plainly, and an
      honest feature list with no AI-washing.
- [ ] LICENSE (Prosperity), SECURITY.md, CONTRIBUTING.md, issue templates, and a PR template all exist.

**Voice**
- [ ] Every user-facing string (README, UI copy, error messages, CLI help text) reads like a
      person wrote it — no AI-slop tells (buzzword bullet spam, "not just X but Y," filler
      hedges, flat uniform rhythm). Run external-facing text through the humanizer pass before
      it ships.

**The honest test:** could a skeptical, technically sharp stranger read this repo end to end —
code, README, security posture — and come away thinking "this person actually knows what they're
doing and isn't hiding anything"? If any checklist item above is unresolved, the answer is no,
and it isn't done yet.
