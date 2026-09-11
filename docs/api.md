# API reference

`server.py` (console script `hopandhaul-serve`) runs a stdlib `http.server` app on
`127.0.0.1:8770` by default (override with `--port` or the `TRAVEL_PORT` env var). It serves
the UI and a small JSON API. There is no write path; every endpoint below is a `GET`.

## Error contract

Every JSON endpoint returns an object with an `"ok"` boolean.

- `{"ok": true, ...}`: the rest of the shape is documented per-endpoint below.
- `{"ok": false, "error": "<human-readable message>", "code": "<machine-readable code>"}`:
  something went wrong. `error` is always a short, generic description meant for a human;
  `code` is a stable string meant for a caller to branch on. The real exception (type and
  text) is logged server-side to stderr, never sent to the client. No endpoint returns a
  stack trace or raw exception string.

Codes you'll actually see: `invalid_param` (a query param failed validation; see the
message for which one and why), `forbidden_host` (the `Host` header wasn't localhost),
`not_found` (unknown path or missing static asset), `unknown_origin` (the `origin` IATA
code isn't in the airport database), `no_airport_near_point` (nothing within the
nearest-airport search radius), `origin_is_destination` (the clicked point resolves to the
same airport as `origin`, so there is nothing to plan), `origin_suspended` (`origin` is a
closed or restricted airport - no civil flights, or no bookable Western service),
`airport_suspended` (the clicked point's only nearby airport(s) are closed/restricted, so
there is honestly nothing to plan there), `geocoding_not_configured` / `geocode_lookup_failed`
(no Geoapify key / the provider call failed), `no_airport_found` (`/api/nearest` found
nothing), `dates_all_past` / `dates_all_failed` / `date_lookup_failed` (`/api/dates`: the
whole window is in the past, nothing in it priced, or one individual day failed),
`internal_error` (an unexpected server-side failure, logged with the real
exception; never sent to the client).

HTTP status codes follow normal REST conventions (`400` for a malformed request, `403` for a
rejected Host header, `404` for an unknown path or missing asset, `500` for a genuine server
fault). `/api/plan` and `/api/dates` are the exceptions: a malformed query param still gets a
`400`, but once the request itself is valid they answer `200` and put success or failure in
the JSON body's `"ok"` field, because a "no route found" or "provider unavailable" result is a
normal, expected outcome for a planning request, not an HTTP-level error.

## Security notes relevant to callers

- The server only accepts requests whose `Host` header is `127.0.0.1`, `localhost`, or `::1`
  (a DNS-rebinding guard). Anything else gets
  `403 {"ok": false, "error": "forbidden host", "code": "forbidden_host"}`.
- No endpoint ever returns an API key, token, or secret. `/api/config` reports only booleans
  and provider *names*.
- Static assets are served from a fixed whitelist dict, not a path built from the request, so
  there is no path-traversal surface.

---

## `GET /`, `GET /index.html`

Returns the UI (`text/html`). Not a JSON endpoint.

## `GET /vendor/leaflet.js`, `GET /vendor/leaflet.css`

Self-hosted Leaflet assets (no CDN). Not JSON endpoints.

## `GET /healthz`

Liveness check. No params, no keys, no work done. This is the one to point a container
`HEALTHCHECK` or a systemd/supervisor watchdog at.

```json
{
  "ok": true,
  "version": "0.9.0"
}
```

`version` is the installed package version, so the same call also tells you what a running
instance actually is. Like every other endpoint it enforces the localhost `Host` allowlist,
so a health probe has to reach it as `127.0.0.1` or `localhost`.

## `GET /api/config`

Tells the frontend what's configured, with no secrets attached.

```json
{
  "ok": true,
  "has_live_keys": true,
  "flights_provider": "duffel",
  "has_geocode": true,
  "geocode_provider": "photon",
  "has_weather": true,
  "has_transit": true,
  "default_origin": "JFK",
  "default_threshold": 200.0,
  "default_travelers": 1,
  "supports_return_date": true
}
```

- `flights_provider`: `"duffel"` or `null` if no flight-pricing key is set.
- `has_geocode` is always true (Photon needs no key); `geocode_provider` says which backend
  answers: `"geoapify"` when that key is configured, else `"photon"`.
- `has_weather` (Open-Meteo) and `has_transit` (Transitous) are keyless and normally true.

## `GET /api/geocode?q=<text>&limit=<n>&lang=<code>`

Type-ahead place search, Photon by default and Geoapify when keyed. Requires `q`; `limit`
defaults to 6, clamped to 1-10. `lang` is optional, one of `en`/`de`/`fr` (what Photon actually
supports server-side); anything else, or omitted, falls back to `en` - it is never rejected, so
a UI that forgets to send it just gets English results instead of a 400. The map UI should send
`lang=<currentLangCode>` so a place search result is worded in whatever language the page is
already showing (see `ui/api.js`).

- If `q` is missing or empty: `400 {"ok": false, "error": "q is required", "code": "invalid_param"}`.
- On a provider error:
  `200 {"ok": false, "error": "geocoding lookup failed", "code": "geocode_lookup_failed"}`
  (the real exception is logged server-side, not returned).
- On success: `200 {"ok": true, "results": [...]}`, provider-shaped place results.

## `GET /api/nearest?lat=<f>&lng=<f>`

Nearest airport to a point, biased toward larger hubs.

- Missing/invalid `lat`/`lng`:
  `400 {"ok": false, "error": "lat is required", "code": "invalid_param"}` (or the equivalent
  message for `lng`, or `"lat must be a number"` / `"lat must be between -90 and 90"` for a
  malformed or out-of-range value).
- No airport resolves (extremely rare):
  `200 {"ok": false, "error": "no airport found", "code": "no_airport_found"}`.
- Success:
  ```json
  {"ok": true, "airport": {"iata": "DEN", "name": "...", "city": "Denver",
                            "lat": 39.86, "lng": -104.67, "hub": 1}}
  ```

## `GET /api/plan?lat=<f>&lng=<f>&...`

The core endpoint: prices a direct flight and every candidate hop-then-ground split, and
applies the $200 rule to recommend one.

**Required:** `lat`, `lng` (the clicked point).

**Optional query params:**

| Param | Type | Default | Meaning |
|---|---|---|---|
| `origin` | IATA string | `JFK` | Departure airport |
| `date` | `YYYY-MM-DD` | none | Outbound date; omit for estimate-only pricing |
| `ret` | `YYYY-MM-DD` | none | Return date; implies round-trip |
| `round` | `1`/`0` | `0` | Force round-trip pricing without a specific return date |
| `vot` | float | none | Value of time, $/hour; trades cash for hours saved |
| `threshold` | float | `200.0` | Minimum $ savings to recommend a split (Cole's rule) |
| `maxGroundH` | float | `6.0` | Cap on ground-leg hours a candidate gateway may require |
| `travelers` | int | `1` | Clamped to 1-9; scales per-person costs, not vehicle costs |
| `buffer` | float | `1.0` | Transfer/connection time buffer, hours |

- Missing/invalid `lat`/`lng` (or any other query param that fails validation: bad date,
  origin too long, threshold out of range, etc.):
  `400 {"ok": false, "error": "lat is required", "code": "invalid_param"}` (message varies by
  which param and how it failed; see `server.py`'s validators for the exact wording).
- Unknown `origin`:
  `200 {"ok": false, "error": "unknown origin airport '<code>'", "code": "unknown_origin"}`.
- The clicked point resolves to the same airport as `origin`, so there is nothing to plan:
  `200 {"ok": false, "error": "that point resolves to your origin airport, so there is no
  flight to plan",
  "code": "origin_is_destination"}`.
- No airport within range of the clicked point:
  `200 {"ok": false, "error": "no airport found near that point", "code": "no_airport_near_point"}`.
- Any other internal failure:
  `200 {"ok": false, "error": "internal error planning that route", "code": "internal_error"}`
  (logged server-side with the real exception type/message; never sent to the client).
- Success shape (trimmed):
  ```json
  {
    "ok": true,
    "pricing_source": "estimate | mixed | duffel-live",
    "date": "2027-06-15", "return_date": null, "roundtrip": false,
    "travelers": 1, "threshold": 200.0, "vot": null,
    "origin": {"iata": "JFK", "lat": ..., "lng": ..., "name": "...", "city": "...", "hub": 1},
    "dest": {"iata": "ASE", "lat": ..., "lng": ..., "dist_km": 3.2, "click": {"lat": ..., "lng": ...}},
    "gateways": [{"iata": "DEN", "ground_mode": "bus", "ground_hours": 4.0, "ground_cost": 34, "...": "..."}],
    "origin_gateways": [{"iata": "ATH", "ground_mode": "ferry", "ground_hours": 2.5, "ground_cost": 60, "...": "..."}],
    "direct": {"price": 620, "hours": 5.5, "source": "estimate", "rt": false},
    "result": {
      "recommended": "Fly direct to ASE",
      "greenest": "DEN + bus",
      "options": [{"name": "...", "cost": 620.0, "co2e_kg": 415.78, "legs": [...], "geo": [...],
                   "itinerary": {"legs": [{"...": "see 'Itinerary, price provenance, and verify links' below"}],
                                  "any_live": false, "example_day": true, "depart_local": "08:00"},
                   "...": "..."}]
    },
    "weather": null,
    "notes": [{"key": "notes.estimateAddDateForLive", "params": {}},
              {"key": "notes.co2eEstimate", "params": {}}]
  }
  ```
- `pricing_source` is `"estimate"` whenever no live provider key/date combination was used,
  `"mixed"` when some legs were live and others fell back, and `"<provider>-live"` (e.g.
  `"duffel-live"`) when every leg priced live.
- `notes` is a list of **structured, translatable notes**: `{"key": "notes.xxx", "params":
  {...}}`, never a hardcoded English string. `key` matches an entry in `src/hopandhaul/ui/i18n/
  en.json` (and every other locale catalog there) under the `notes.` namespace; `params` fills
  in that template's `{placeholder}` fields (e.g. `notes.groupTotals` carries `travelers` and
  `vehicles`; `notes.lastMileGap` carries `iata` and `km`). A caller that only speaks English
  can render the same text the CLI does via `itinerary.render_note(note)` (Python) - the CLI
  (`hopandhaul go`/`hopandhaul duffel`) always prints full English sentences this way, read
  from the same `en.json` the browser ships, not a second hand-maintained table. The full key
  catalog: `estimatePastDate`, `estimateDateApplied`, `estimateNeutralWindow`,
  `estimateAddDateForLive`, `estimateNoProvider`, `mixedLiveEstimate`, `liveLookupFailed`,
  `fxStatic`, `fxLive`, `fxUnknown`, `groupTotals`, `roundtripReal`,
  `roundtripEstimatedSeparate`, `roundtripEstimated2x`, `ferryRealCorridor`,
  `transitLiveSchedule`, `lastMileGap`, `finalLeg`, `co2eEstimate`, `originSuspended`,
  `airportSuspended`, `legLikelyConnecting`, `liveBaggageCaveat`, `groundCrossingRestricted`,
  `airportClosedNearby` (all under the `notes.` prefix). Read the notes before trusting the number.
  - `notes.finalLeg`, `notes.groundCrossingRestricted`, and `notes.lastMileGap` are mutually
    exclusive and cover every case where the resolved destination airport is more than 12km
    (`geo.FINAL_LEG_MIN_KM`) from the clicked point: `finalLeg` fires when an honest last-mile
    leg exists (real ferry corridor or plain overland - see "The last-mile leg" below);
    `groundCrossingRestricted` fires when the airport genuinely nearest the click is itself
    suspended and sits in a country whose land/rail crossings are closed (Russia, Belarus -
    Ukraine stays ground-open); `lastMileGap` is the fallback for every other refusal (only open
    sea, no corridor). Unlike earlier versions of this API, `lastMileGap` is NOT gated on a
    120km distance - it fires on any refused last-mile leg past 12km, so a refusal is never
    silent.
  - `notes.airportClosedNearby` fires when the airport nearest the clicked point (suspended
    fields included) is closed/restricted and a different, served airport was used instead -
    carries `closed_iata`, `closed_name`, `used_iata` - so a route via a farther field never
    looks like a coincidence.
  - `notes.legLikelyConnecting` never appears at the top level - it rides inside a flight leg's
    own `label` field (see "Itinerary..." below), not in the response's top-level `notes` array.
  - `notes.liveBaggageCaveat` is server-only: it appears whenever any leg in the plan priced off
    a real Duffel fare. The browser engine (`ui/engine/plan.js`) has no live-fare code path at
    all, so it can never emit this key - not a parity gap, just a branch that literally can't be
    reached client-side.

### The last-mile leg

An airport is a proxy for the place someone actually asked about, not the place itself:
"Interlaken" resolves to BRN, about 40km and an hour of train away. Every option in
`result.options` - including the direct-flight baseline - includes a final ground leg from the
resolved destination airport (`dest.iata`) onward to the actual clicked point (`dest.click`)
whenever that gap is more than 12km (`geo.FINAL_LEG_MIN_KM`). The leg is priced through the same
machinery as a gateway's ground leg: a real ferry corridor when one dominantly covers the gap
(`gateways[].ferry`'s sibling logic, reused symmetrically), otherwise plain overland, chosen by
the same region-aware mode/distance rules, with its own stricter water policy: no coast-hugging
offset-rescue (an endpoint the gateway logic would nudge to find a detour stays exactly where it
is), a much lower open-water blocking threshold, and an island-suspicious check on the
destination's own grid cell for when the sampled path misses a narrow strait entirely. It shows
up as the LAST entry in each option's `itinerary.legs` and `geo` arrays, and its cost/hours are
folded into that option's headline `cost`/`hours_eff` - since it's added uniformly to every
option, the split-vs-direct comparison stays fair (in the option-string encoding, this leg's
mode is prefixed `"final:"` - see `trip.FINAL_LEG_PREFIX` - so it can never be miscounted as part
of a multimodal split, even on a 2-leg option). When the gap is real but there is no honest way
to cross it (open sea, no corridor, or a closed ground crossing into the nearest airport) the leg
is omitted and one of `notes.finalLeg`'s siblings explains why instead - see the notes catalog
above.

### Origin-side splits: `origin_gateways`

`gateways` only ever searches near the DESTINATION. `origin_gateways` is the symmetric search
near the ORIGIN airport, for exactly the case `gateways` structurally cannot reach: a
remote/expensive origin (e.g. `origin=JTR`) whose return-home leg used to be priced direct-only
even when grounding to a real hub first and flying from there was cheaper. Each entry has the
same shape as a `gateways[]` entry; the corresponding option in `result.options` is named
`"<Mode> to <IATA> + fly"` (e.g. `"Ferry to ATH + fly"`) and its leg order is reversed from a
normal split: ground FIRST (origin -> the gateway), then fly (the gateway -> dest). Single-sided
only - an origin-side split is never combined with a dest-side split in the same option. A
well-connected origin (a major hub, and no curated gateway table for it) naturally yields an
empty `origin_gateways` list - there is no separate "is this origin remote enough" flag to check.

### Gateway extras: `gateways[].ferry` and `gateways[].transit`

A ferry gateway carries a `ferry` object, the REAL corridor behind the leg: `name`,
`operators`, the actual `port_a`/`port_b` terminal names, `duration_h` (published crossing
time), `frequency_per_day`, `seasonal`, the sourced fare band `price_usd_lo`/`price_usd_hi`
with `price_asof`, the `fare_usd` used in the leg's cost, `fare_is_real`, `crossing_km`, and
the airport-to-port transfer estimate (`access_cost`/`access_hours`). The engine never
invents a boat: no matching corridor in `data/ferries.json` means no ferry leg.

Any train/bus/ferry gateway may additionally carry `transit`, a REAL timetable from
Transitous: `duration_h` (real door-to-door, which replaces the formula duration in the leg
and the ranking), `legs` (each with `mode`, `agency`, `route`, `depart` clock), `depart`,
`transfers`, `n_options`, `date`, and a ready-made provenance sentence in `line`. Present
only when Transitous covered the route at planning time; fares on those legs remain
estimates either way.

### Emissions: `co2e_kg` and `greenest` (cheapest vs greenest)

Every option in `result.options` carries a `co2e_kg` field: an ESTIMATED kilograms-CO2e figure
for that whole option (all legs, all travelers), computed from each leg's flight/ground
distance against a small hardcoded factor table in `emissions.py`. Not a live API, not a
certified footprint calculator. `result.greenest` is the `name` of whichever option in the set
has the lowest `co2e_kg`.

This is informational only. The server never uses `co2e_kg` to choose `result.recommended`:
the $200 rule and the rest of `trip.py`'s ranking are completely unaware emissions data exists.
`greenest` is just a second, independent pointer alongside `recommended`, so the response lets
you compare "cheapest/recommended" against "lowest-carbon" side by side and decide for yourself;
it does not mean the greenest option is a better choice.

Factor basis (grams CO2e per passenger-km, well-to-wake): short-haul flight (<1500km) ~246
g/pkm, long-haul ~148 g/pkm (both roughly DEFRA/EEA-range; a `with_rf=True` call in
`emissions.py` applies a ~1.9x radiative-forcing uplift for aviation's non-CO2 warming effects,
not used in the API response by default but available to any caller of the module directly),
rail ~37 g/pkm (EU-average blend; a clean-grid electric line can be much lower, a diesel
regional line higher), coach/bus ~28 g/pkm, car ~170 g per VEHICLE-km (divided across
travelers only when a mode is priced per-person; a drive/rental leg is per-vehicle, same
distinction `trip.py` already makes for cost). Full citations and reasoning in
`src/hopandhaul/emissions.py`'s module docstring.

### Itinerary, price provenance, and verify links: `result.options[].itinerary`

A dollar figure with no airports, no schedule, and no way to check it isn't worth much. Every
option carries an `itinerary` (built by `itinerary.py`) turning its total into a leg-by-leg,
checkable schedule:

```json
{
  "legs": [{
    "mode": "fly",
    "from": {"iata": "JFK", "name": "New York JFK", "city": "New York"},
    "to": {"iata": "DEN", "name": "Denver", "city": "Denver"},
    "depart_clock": "08:00", "depart_day": "2027-06-15",
    "arrive_clock": "11:00", "arrive_day": "2027-06-15",
    "duration_h": 3.0,
    "checkin_by": {"clock": "06:00", "day": "2027-06-15"},
    "cost": 210.0,
    "price_basis": "route-band estimate for 2027-06-15; NA-NA market ×1.00; date factor ×1.08",
    "verify_url": "https://www.google.com/travel/flights?q=Flights+from+JFK+to+DEN+on+2027-06-15",
    "is_live": false, "carrier": null, "flight_number": null,
    "label": null
  }],
  "any_live": false, "example_day": true, "depart_local": "08:00"
}
```

- `from`/`to`: the real airport or station, with IATA code, full name, and city. Never a bare code.
- `depart_clock`/`arrive_clock`/`depart_day`/`arrive_day`: a clock schedule. A leg's times are
  real offer times only when that leg is `is_live: true` (a live Duffel fare priced it);
  otherwise they're synthetic. The block-level `example_day` stays `true` as long as ANY leg
  is still an estimate, and only flips to `false` when every leg came from a live offer, so a
  mixed itinerary is never presented as a fully real day.
  Synthetic times walk forward from a sane default departure (`depart_local`, `08:00`) with a
  connection buffer between legs (the same `buffer` query param that already lengthens
  `hours_eff`, so the itinerary's elapsed time always reconciles with the summary card next to
  it) and no timezone conversion: `airports.json` carries no timezone data, and a
  longitude-based guess would be its own kind of dishonesty. A live leg's times ARE real
  per-airport local times (Duffel resolves that server-side).
- `checkin_by`: present on a flight leg only, a generic 2-hour-early airport-arrival
  recommendation, not an airline-specific claim.
- `price_basis`: plain-English provenance for that leg's `cost`: which route-band multipliers
  applied (estimate) or which carrier/fare priced it (live). Free text, like `notes` elsewhere
  in this response, and not translated by the UI's i18n catalog.
- `verify_url`: a one-click way to check the number: a Google Flights search
  (`?q=Flights+from+XXX+to+YYY+on+YYYY-MM-DD`, or `...on+YYYY-MM-DD+through+YYYY-MM-DD` when
  the plan is a real round trip with a return date) for a flight leg, a Rome2Rio map link
  (`/map/{from}/{to}`) for a ground leg.
- `is_live` / `carrier` / `flight_number`: only real (not invented), and `null`/`false` on every
  estimate leg.
- `label`: `null` on almost every leg. On a flight leg whose fare was priced ASSUMING a
  connection (the small/remote-airport pricing path in `geo.estimate_flight` -
  `likely_connection`), this carries `{"key": "notes.legLikelyConnecting", "params": {}}` -
  render it through the same `notes.` catalog. The single-arc map geometry doesn't change; this
  is purely so the leg doesn't read as a bare nonstop when the price already isn't one. Never
  set on a live segment row (a real multi-segment connection already shows itself as more than
  one row).

The same live-vs-estimate split shows up one level up too: `direct` and each entry in
`gateways[].fly` are the raw pricing dict `itinerary` was built from, so a caller who wants the
provenance without the formatted timeline can read `estimate_detail` (estimate) or `segments`/
`carrier`/`native_price` (live) directly.

### Translatable option names and price-basis: `name_key`/`name_params`, `basis_parts`

Every option name (`"Fly direct to CVU"`, `"ZRH + train"`, ...) and every leg's price-basis
string used to be hardcoded English, spliced untranslated into every non-English locale. Both
are now ALSO available in a structured form; the plain English `name` / `price_basis` stay too,
for the CLI and backcompat - a caller that only speaks English needs no changes.

- Each option in `result.options` carries `name_key` and `name_params` alongside `name`:
  `name_key` is one of `option.flyDirect` `{iata}`, `option.gatewayGround` `{iata, mode}`,
  `option.groundToHubFly` `{iata, mode}`, `option.groundOnlyFrom` `{iata, mode}` (the dest-side
  self-gateway case: the gateway curated/found near the destination is the user's own origin
  airport, so there's no flight, just a ground leg from home), `option.groundOnlyTo` `{iata,
  mode}` (the symmetric origin-side case: the gateway found near the origin is the destination
  itself). `mode` is itself a param-i18n value (see below), never a raw English word. Every key
  lives under the `option.` namespace in `ui/i18n/en.json` (and every other locale catalog).
- Each leg in `result.options[].itinerary.legs` carries `basis_parts` alongside `price_basis`:
  an ARRAY of `{key, params}` segments under the `basis.` namespace, in the same order the
  English sentence would join them with `"; "`. Keys emitted: `basis.routeBand` / `basis.
  routeBandDated {date}` (no date vs a given date), `basis.marketMult {region, mult}`, `basis.
  anchoredBts {lo, hi, asof}` (a real BTS-anchored fare band backed the estimate),
  `basis.dateAdjusted {mult}`, `basis.connectingAssumed` (small/remote-airport pricing assumed a
  connection), `basis.liveDuffel {carrier}`, `basis.fxNativePriced`/`basis.fxLivePriced`/`basis.
  fxStaticPriced {native, currency}` (a live fare quoted in a non-USD currency, and whether the
  conversion was a live rate, a static table, or unconverted), `basis.ferryBand {lo, hi,
  operators, asof}` (a real ferry corridor's researched fare band), `basis.curatedGateway` (a
  hand-tuned gateway estimate), `basis.groundEstimate {km}` (a formula ground estimate), `basis.
  transitLive {line}` (a real Transitous schedule rides alongside whichever of the above priced
  the fare - `line` is the ready-made English description carrying real operator/route names,
  which can't be meaningfully translated, so it's passed through as an opaque value rather than
  further decomposed).
- **Param-i18n convention**: a note/name/basis param whose VALUE is itself a translatable word
  (so far, only leg modes) is `{"i18n": "mode.xxx"}` instead of a raw string - see `mode.flight`
  /`mode.train`/`mode.bus`/`mode.shuttle`/`mode.drive`/`mode.rentalCar`/`mode.ferry`/`mode.
  ground` in `ui/i18n/en.json`. A renderer resolves `{"i18n": "..."}` params by looking the key
  up in its own active-language catalog before formatting; every other param renders as-is.
  Python's `itinerary.render_note()`/`mode_i18n_param()` implement this for the CLI (always
  English); the browser's `results.js` (not part of `ui/engine/`) is the equivalent renderer for
  whatever language the page is showing, and must apply the same resolution to `name_params` and
  `basis_parts` params, not just `notes[].params`.

## `GET /api/dates?lat=<f>&lng=<f>&date=<YYYY-MM-DD>&...`

Prices `date` and the days either side of it, and reports which one comes out cheapest. This
is the web twin of the `hopandhaul dates` CLI sweep. Both build their window, label each day's
basis and break ties through the same three helpers in `dates.py`, so the two cannot disagree
about which day won.

**Required:** `lat`, `lng`, and `date`. Unlike `/api/plan`, `date` is not optional here: a
sweep with nothing to centre on has nothing to sweep.

**Optional query params:** every one `/api/plan` takes, with the same meanings and defaults,
plus:

| Param | Type | Default | Meaning |
|---|---|---|---|
| `window` | int | `3` | Days to price either side of `date`. Clamped to 0-7, so at most 15 dates |

`ret` behaves differently here than on `/api/plan`. Each candidate departure carries its own
return, shifted by the same number of days, so the trip LENGTH stays fixed while its placement
in the window moves. A 7-night trip stays a 7-night trip on every row.

```json
{
  "ok": true,
  "origin_iata": "JFK", "dest_iata": "ASE",
  "anchor_date": "2026-10-01", "window": 1,
  "comparable": true, "live_cut_off": false,
  "dates": [
    {"date": "2026-09-30", "return_date": null, "ok": true,
     "recommended": "Fly direct to ASE", "cost": 250.0, "hours": 6.2,
     "basis": "estimate", "pricing_source": "estimate", "savings_vs_anchor": 10.0},
    {"date": "2026-10-01", "...": "the anchor, savings_vs_anchor is always 0.0"},
    {"date": "2026-10-02", "...": "a negative savings means this day costs MORE"}
  ],
  "best": {"date": "2026-09-30", "cost": 250.0, "hours": 6.2,
           "basis": "estimate", "recommended": "Fly direct to ASE"}
}
```

- `dates` is in chronological order, one row per candidate day. A day that could not be priced
  has `ok: false` and its own `error`/`code` instead of a cost, and does not sink the sweep.
- `savings_vs_anchor` is `anchor cost - this day's cost`, so positive means cheaper than the
  day you asked for. It is `null` when the anchor itself never priced.
- `basis` is `live`, `estimate` or `mixed`, describing that day's flight legs.
- **`comparable` is the flag that matters.** A window is priced all-live or all-estimate,
  never a mix. Two rate limiters sit in series on the live path and running out partway
  through is the normal case, so if the live pass comes back on more than one basis the whole
  window is thrown away and re-priced offline, and `live_cut_off` goes true. `comparable` is
  then recomputed from the final rows rather than asserted. A false means the winner is a hint
  rather than a fact, and a caller should say so instead of naming a cheapest day. The bundled
  UI refuses to name one.
- Days already in the past are dropped from the window rather than priced. The fare model has
  no booking-lead-time curve to read backwards, so it would price them exactly like an undated
  request while the row claimed to be a comparison.

Failure codes, on top of everything `/api/plan` can return (the structural refusals, unknown
origin and no-airport-near-point, are probed once up front so they fail the whole sweep with
the real reason instead of emitting N identical error rows):

- `date` missing: `400 {"ok": false, "error": "date is required", "code": "invalid_param"}`.
- `window` outside 0-7 or not a whole number: `400 ... "code": "invalid_param"`.
- Every day in the window is already past:
  `200 {"ok": false, "error": "every date in that window is already in the past",
  "code": "dates_all_past"}`.
- No day in the window could be priced at all:
  `200 {"ok": false, "error": "none of the dates in that window could be priced",
  "code": "dates_all_failed"}`.
- A single day failing carries `date_lookup_failed` on its own row, not on the sweep.

Transit and weather are off for every candidate. Real ground schedules cost seconds per plan
and do not change which DATE is cheapest, only fares do. The day the caller actually picks
gets a full `/api/plan` with transit and all.

## `GET /favicon.ico`

Returns `204 No Content`.

## Anything else

`404 {"ok": false, "error": "not found", "code": "not_found"}`.
