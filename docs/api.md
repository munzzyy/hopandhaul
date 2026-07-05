# API reference

`server.py` (console script `hopandhaul-serve`) runs a stdlib `http.server` app on
`127.0.0.1:8770` by default (override with `--port` or the `TRAVEL_PORT` env var). It serves
the UI and a small JSON API. There is no write path; every endpoint below is a `GET`.

## Error contract

Every JSON endpoint returns an object with an `"ok"` boolean.

- `{"ok": true, ...}`: the rest of the shape is documented per-endpoint below.
- `{"ok": false, "error": "<human-readable message>"}`: something went wrong. The message is
  always a short, generic description; the real exception (type and text) is logged
  server-side to stderr, never sent to the client. No endpoint returns a stack trace or raw
  exception string.

HTTP status codes follow normal REST conventions (`400` for a malformed request, `403` for a
rejected Host header, `404` for an unknown path or missing asset, `500` for a genuine server
fault). `/api/plan` is the one exception: it always answers `200` and puts success/failure in
the JSON body's `"ok"` field, because a "no route found" or "provider unavailable" result is a
normal, expected outcome for a planning request, not an HTTP-level error.

## Security notes relevant to callers

- The server only accepts requests whose `Host` header is `127.0.0.1`, `localhost`, or `::1`
  (a DNS-rebinding guard). Anything else gets `403 {"ok": false, "error": "forbidden host"}`.
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
  "has_weather": false,
  "default_origin": "JFK",
  "default_threshold": 200.0,
  "default_travelers": 1,
  "supports_return_date": true
}
```

- `flights_provider`: `"duffel"`, `"amadeus"`, or `null` if no flight-pricing key is set.
- `has_geocode` / `has_weather`: whether Geoapify / OpenWeather keys are configured.

## `GET /api/geocode?q=<text>&limit=<n>`

Type-ahead place search (Geoapify). Requires `q`; `limit` defaults to 6, clamped to 1-10.

- If `q` is missing or empty: `400 {"ok": false, "error": "q (query text) required"}`.
- If no Geoapify key is configured: `200 {"ok": false, "error": "geocoding not configured"}`.
- On a provider error: `200 {"ok": false, "error": "geocoding lookup failed"}` (the real
  exception is logged server-side, not returned).
- On success: `200 {"ok": true, "results": [...]}`, provider-shaped place results.

## `GET /api/nearest?lat=<f>&lng=<f>`

Nearest airport to a point, biased toward larger hubs.

- Missing/invalid `lat`/`lng`: `400 {"ok": false, "error": "lat/lng required"}`.
- No airport resolves (extremely rare): `200 {"ok": false, "error": "no airport found"}`.
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

- Missing/invalid `lat`/`lng`: `400 {"ok": false, "error": "lat and lng required"}`.
- Unknown `origin`: `200 {"ok": false, "error": "unknown origin airport '<code>'"}`.
- Any other internal failure: `200 {"ok": false, "error": "internal error planning that
  route"}` (logged server-side with the real exception type/message; never sent to the
  client).
- Success shape (trimmed):
  ```json
  {
    "ok": true,
    "pricing_source": "estimate | mixed | duffel-live | amadeus-live",
    "date": "2026-08-15", "return_date": null, "roundtrip": false,
    "travelers": 1, "threshold": 200.0, "vot": null,
    "origin": {"iata": "JFK", "lat": ..., "lng": ..., "name": "...", "city": "...", "hub": 1},
    "dest": {"iata": "ASE", "lat": ..., "lng": ..., "dist_km": 3.2, "click": {"lat": ..., "lng": ...}},
    "gateways": [{"iata": "DEN", "ground_mode": "bus", "ground_hours": 4.0, "ground_cost": 75, "...": "..."}],
    "direct": {"price": 620, "hours": 5.5, "source": "estimate", "rt": false},
    "result": {
      "recommended": "Fly direct to ASE",
      "options": [{"name": "...", "cost": 620.0, "legs": [...], "geo": [...], "...": "..."}]
    },
    "weather": null,
    "notes": ["Fares are distance-based ESTIMATES ... add a date for live fares."]
  }
  ```
- `pricing_source` is `"estimate"` whenever no live provider key/date combination was used,
  `"mixed"` when some legs were live and others fell back, and `"<provider>-live"` (e.g.
  `"duffel-live"`) when every leg priced live.
- `notes` is a plain-English list explaining anything a user should know about how the numbers
  were produced (estimate mode, FX conversion, group totals, round-trip approximation, a
  distant nearest-airport match, etc). Read it before trusting the number.

## `GET /favicon.ico`

Returns `204 No Content`.

## Anything else

`404 {"ok": false, "error": "not found"}`.
