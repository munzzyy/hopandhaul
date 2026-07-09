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

Codes you'll actually see: `invalid_param` (a query param failed validation — see the
message for which one and why), `forbidden_host` (the `Host` header wasn't localhost),
`not_found` (unknown path or missing static asset), `unknown_origin` (the `origin` IATA
code isn't in the airport database), `no_airport_near_point` (nothing within the
nearest-airport search radius), `origin_is_destination` (the clicked point resolves to the
same airport as `origin` — nothing to plan), `geocoding_not_configured` / `geocode_lookup_failed`
(no Geoapify key / the provider call failed), `no_airport_found` (`/api/nearest` found
nothing), `internal_error` (an unexpected server-side failure, logged with the real
exception; never sent to the client).

HTTP status codes follow normal REST conventions (`400` for a malformed request, `403` for a
rejected Host header, `404` for an unknown path or missing asset, `500` for a genuine server
fault). `/api/plan` is the one exception: it always answers `200` and puts success/failure in
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
  answers — `"geoapify"` when that key is configured, else `"photon"`.
- `has_weather` (Open-Meteo) and `has_transit` (Transitous) are keyless and normally true.

## `GET /api/geocode?q=<text>&limit=<n>`

Type-ahead place search — Photon by default, Geoapify when keyed. Requires `q`; `limit`
defaults to 6, clamped to 1-10.

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

- Missing/invalid `lat`/`lng` (or any other query param that fails validation — bad date,
  origin too long, threshold out of range, etc.):
  `400 {"ok": false, "error": "lat is required", "code": "invalid_param"}` (message varies by
  which param and how it failed; see `server.py`'s validators for the exact wording).
- Unknown `origin`:
  `200 {"ok": false, "error": "unknown origin airport '<code>'", "code": "unknown_origin"}`.
- The clicked point resolves to the same airport as `origin` — nothing to plan:
  `200 {"ok": false, "error": "that point resolves to your origin airport — no flight needed",
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
    "date": "2026-08-15", "return_date": null, "roundtrip": false,
    "travelers": 1, "threshold": 200.0, "vot": null,
    "origin": {"iata": "JFK", "lat": ..., "lng": ..., "name": "...", "city": "...", "hub": 1},
    "dest": {"iata": "ASE", "lat": ..., "lng": ..., "dist_km": 3.2, "click": {"lat": ..., "lng": ...}},
    "gateways": [{"iata": "DEN", "ground_mode": "bus", "ground_hours": 4.0, "ground_cost": 75, "...": "..."}],
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
    "notes": ["Fares are distance-based ESTIMATES ... add a date for live fares.",
              "co2e_kg per option is a rough ESTIMATE from flight/ground distance, not a certified footprint ..."]
  }
  ```
- `pricing_source` is `"estimate"` whenever no live provider key/date combination was used,
  `"mixed"` when some legs were live and others fell back, and `"<provider>-live"` (e.g.
  `"duffel-live"`) when every leg priced live.
- `notes` is a plain-English list explaining anything a user should know about how the numbers
  were produced (estimate mode, FX conversion, group totals, round-trip approximation, a
  distant nearest-airport match, etc). Read it before trusting the number.

### Gateway extras: `gateways[].ferry` and `gateways[].transit`

A ferry gateway carries a `ferry` object — the REAL corridor behind the leg: `name`,
`operators`, the actual `port_a`/`port_b` terminal names, `duration_h` (published crossing
time), `frequency_per_day`, `seasonal`, the sourced fare band `price_usd_lo`/`price_usd_hi`
with `price_asof`, the `fare_usd` used in the leg's cost, `fare_is_real`, `crossing_km`, and
the airport-to-port transfer estimate (`access_cost`/`access_hours`). The engine never
invents a boat: no matching corridor in `data/ferries.json` means no ferry leg.

Any train/bus/ferry gateway may additionally carry `transit` — a REAL timetable from
Transitous: `duration_h` (real door-to-door, which replaces the formula duration in the leg
and the ranking), `legs` (each with `mode`, `agency`, `route`, `depart` clock), `depart`,
`transfers`, `n_options`, `date`, and a ready-made provenance sentence in `line`. Present
only when Transitous covered the route at planning time; fares on those legs remain
estimates either way.

### Emissions: `co2e_kg` and `greenest` (cheapest vs greenest)

Every option in `result.options` carries a `co2e_kg` field: an ESTIMATED kilograms-CO2e figure
for that whole option (all legs, all travelers), computed from each leg's flight/ground
distance against a small hardcoded factor table in `emissions.py` — not a live API, not a
certified footprint calculator. `result.greenest` is the `name` of whichever option in the set
has the lowest `co2e_kg`.

This is informational only. The server never uses `co2e_kg` to choose `result.recommended` —
the $200 rule and the rest of `trip.py`'s ranking are completely unaware emissions data exists.
`greenest` is just a second, independent pointer alongside `recommended`, so the response lets
you compare "cheapest/recommended" against "lowest-carbon" side by side and decide for yourself;
it does not mean the greenest option is a better choice.

Factor basis (grams CO2e per passenger-km, well-to-wake): short-haul flight (<1500km) ~246
g/pkm, long-haul ~148 g/pkm (both roughly DEFRA/EEA-range; a `with_rf=True` call in
`emissions.py` applies a ~1.9x radiative-forcing uplift for aviation's non-CO2 warming effects,
not used in the API response by default but available to any caller of the module directly),
rail ~37 g/pkm (EU-average blend — a clean-grid electric line can be much lower, a diesel
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
    "depart_clock": "08:00", "depart_day": "2026-08-15",
    "arrive_clock": "11:00", "arrive_day": "2026-08-15",
    "duration_h": 3.0,
    "checkin_by": {"clock": "06:00", "day": "2026-08-15"},
    "cost": 210.0,
    "price_basis": "route-band estimate for 2026-08-15; NA-NA market ×1.00; date factor ×1.08",
    "verify_url": "https://www.google.com/travel/flights?q=Flights+from+JFK+to+DEN+on+2026-08-15",
    "is_live": false, "carrier": null, "flight_number": null
  }],
  "any_live": false, "example_day": true, "depart_local": "08:00"
}
```

- `from`/`to`: the real airport or station — IATA code, full name, and city — never a bare code.
- `depart_clock`/`arrive_clock`/`depart_day`/`arrive_day`: a clock schedule. A leg's times are
  real offer times only when that leg is `is_live: true` (a live Duffel fare priced it);
  otherwise they're synthetic. The block-level `example_day` stays `true` as long as ANY leg
  is still an estimate — it only flips to `false` when every leg came from a live offer, so a
  mixed itinerary is never presented as a fully real day.
  Synthetic times walk forward from a sane default departure (`depart_local`, `08:00`) with a
  connection buffer between legs (the same `buffer` query param that already lengthens
  `hours_eff` — the itinerary's elapsed time always reconciles with the summary card next to
  it) and no timezone conversion: `airports.json` carries no timezone data, and a
  longitude-based guess would be its own kind of dishonesty. A live leg's times ARE real
  per-airport local times (Duffel resolves that server-side).
- `checkin_by`: present on a flight leg only — a generic 2-hour-early airport-arrival
  recommendation, not an airline-specific claim.
- `price_basis`: plain-English provenance for that leg's `cost` — which route-band multipliers
  applied (estimate) or which carrier/fare priced it (live). Free text, like `notes` elsewhere
  in this response — not translated by the UI's i18n catalog.
- `verify_url`: a one-click way to check the number — a Google Flights search
  (`?q=Flights+from+XXX+to+YYY+on+YYYY-MM-DD`) for a flight leg, a Rome2Rio map link
  (`/map/{from}/{to}`) for a ground leg.
- `is_live` / `carrier` / `flight_number`: only real (not invented) — `null`/`false` on every
  estimate leg.

The same live-vs-estimate split shows up one level up too: `direct` and each entry in
`gateways[].fly` are the raw pricing dict `itinerary` was built from, so a caller who wants the
provenance without the formatted timeline can read `estimate_detail` (estimate) or `segments`/
`carrier`/`native_price` (live) directly.

## `GET /favicon.ico`

Returns `204 No Content`.

## Anything else

`404 {"ok": false, "error": "not found", "code": "not_found"}`.
