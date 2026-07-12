# Security

hopandhaul is a local, single-user tool: `hopandhaul-serve` binds to `127.0.0.1` and
talks to a fixed set of third-party APIs over hardcoded hosts: Duffel and Geoapify
(optional, keyed) plus four keyless ones, Transitous (`api.transitous.org`, ground
schedules), Photon (`photon.komoot.io`, place search), Open-Meteo (`api.open-meteo.com`,
weather), and frankfurter (`api.frankfurter.dev`, FX rates). The browser build's CSP
allows exactly two of those (`api.transitous.org`, `photon.komoot.io`) beyond `'self'`,
because the static GitHub Pages app calls them directly; everything else stays
server-side. This document says plainly what that means for security, what's already
handled, and what isn't built yet.

## No SSRF is possible

Every outbound HTTP call in this codebase targets a hardcoded host literal: the six
hosts named above, nothing else. No code path builds a request URL or hostname from client input,
a query parameter, or a map click. `geo.py` (nearest-airport lookup, gateway discovery)
is pure local JSON/math and never touches the network at all.

This is a checkable claim, not a promise: grep `src/hopandhaul/*.py` and
`src/hopandhaul/ui/*.js` for `http://` and `https://` and confirm every hit is a literal
in the source, never an f-string built from a request. If a future feature ever lets a user point the server at their own geocoder or
tile server, that's the point to add the standard SSRF guards (block RFC1918/loopback/
link-local ranges, no blind redirect-following). Don't ship that without them.

## Binding and the Host-header guard

- `serve()` binds `ThreadingHTTPServer` to `127.0.0.1` only. It never listens on `0.0.0.0`.
- Every request is checked against an `ALLOWED_HOSTS` allowlist
  (`127.0.0.1`, `localhost`, `::1`) before anything else runs, closing the DNS-rebinding
  attack where a malicious page's JS gets a browser to send a same-origin-looking request
  to `127.0.0.1` under a different `Host` header.

If you ever want to run this beyond your own machine (a home server, a shared network),
that is explicitly **not** what the default stdlib server is built for. `http.server`'s own
docs say it isn't hardened for that: it has no TLS, slow-loris defense, or real request-size
limits. Don't flip the bind to `0.0.0.0` without adding, at minimum: TLS termination (a
reverse proxy is the easy path), real authentication, and rate limiting in front of it.
That's a distinct, opt-in deployment mode this project doesn't build by default.

## Static file serving

The UI and its vendored assets (`ui/index.html`, `ui/vendor/leaflet.js`,
`ui/vendor/leaflet.css`) are served from an exact-path allowlist dict, never from
`os.path.join(root, request_path)`. There is no code path that turns a URL into a
filesystem path outside that fixed set: a `..` in the request path just doesn't match
anything in the dict and 404s.

## No secrets reach the browser

`/api/config` returns booleans and provider *names* only (`"duffel"` / `"photon"` /
`"geoapify"` / `null`), never a key, never a masked fragment of one. Every other endpoint follows the
same rule. If you add a new endpoint, keep this invariant: nothing that touches
`_secrets.get(...)` should ever appear in a response body.

Keys themselves resolve through `_secrets.py`: environment variable first, then a
gitignored `secrets.local.json` in the package directory. Never commit a real key —
`secrets.local.json` is in `.gitignore`, and a `secrets.local.example.json` with
placeholder values ships instead so you can see the expected shape.

## Error handling

Every endpoint returns one contract: `{"ok": true, ...}` on success, or
`{"ok": false, "error": "<human-readable message>", "code": "<short_code>"}` on failure.
Exception details (message, type name, traceback) are logged server-side
(`stderr`) and never included in the response body. An earlier version of `/api/geocode`
returned the raw `f"{type(e).__name__}: {e}"` string to the client; that's fixed, and the
selftest asserts the error shape stays generic so it can't quietly regress.

## Rate limiting and time budgets

A shared token bucket sits in front of outbound Duffel calls (2 req/s sustained, small
burst allowance) so a handful of fast map clicks can't blow through Duffel's own account
rate limit. A wall-clock time budget bounds the whole concurrent per-gateway pricing
fan-out inside `/api/plan`, so one slow upstream degrades that gateway to a distance
estimate instead of holding every request thread open for its full individual timeout.

Neither of these is a defense against a malicious high-volume client — this is a
single-user localhost tool with no auth, and that's an intentional scope limit (see
above). They exist for reliability against a slow/unstable third-party API, not as a
DoS control.

## Input validation

`/api/plan`, `/api/geocode`, and `/api/nearest` validate every query parameter before it
reaches any application logic: numeric fields are type- and range-checked (lat/lng within
real coordinate bounds, traveler count 1-9, threshold and time-budget fields non-negative
with a sane ceiling), string fields are length-capped, and dates are checked against a
real calendar rather than a regex shape. A malformed request gets a 400 with a specific,
safe message — never a stack trace, never a silent wrong answer.

## Reporting a vulnerability

Please don't open a public GitHub issue for a security problem. Use GitHub's private
[Security Advisory](../../security/advisories/new) reporting form on this repo, or email
the address listed on the maintainer's GitHub profile. Include what you found and how to
reproduce it — you don't need a full writeup or a patch.

**Never paste a real API key into an issue, PR, or advisory** — even a "just testing,
revoked already" key. If you think a key of yours leaked, rotate it at the provider first
and mention that it happened; don't paste the value anywhere on GitHub.

This is a solo-maintained project. Expect an initial response within a few days, not a
contractual SLA — but a real one, and a real fix, not a "thanks, wontfix."

## Scope

In scope: this repo's server (`src/hopandhaul/server.py`), key-handling
(`src/hopandhaul/_secrets.py`), and the fare/geocode/transit/weather-fetching code in
`src/hopandhaul/{duffel,geoapify,places,transit,weather,flights}.py`.

Out of scope: vulnerabilities in Duffel, Geoapify, Transitous, Photon, Open-Meteo,
frankfurter, or any other upstream provider's own API or infrastructure — report those to
the provider directly. Also out of
scope: the consequences of running `hopandhaul-serve` with a manually-flipped bind address
or behind your own reverse proxy without the TLS/auth/rate-limiting called out above —
that's a deployment choice this project explicitly doesn't make for you.
