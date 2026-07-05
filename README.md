# Hop and Haul

**Flies you into the airport that's actually cheap, then tells you honestly whether the
train ride from there is worth it.**

[![CI](https://github.com/munzzyy/hopandhaul/actions/workflows/ci.yml/badge.svg)](https://github.com/munzzyy/hopandhaul/actions/workflows/ci.yml)
[![MIT license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![zero dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)

![screenshot placeholder](docs/screenshot.png)
*(click a spot on the map, get a fly-direct-vs-fly-then-train verdict — GIF coming once the
UI's savings-number redesign ships)*

## The idea

Sometimes the cheapest way to get somewhere isn't flying there directly. It's flying into a
nearby hub where flights are cheap and plentiful, then covering the last leg by train, bus,
ferry, or rental car.

Google Flights and Kayak will search nearby airports for you. None of them tell you whether
the split is actually worth it once you account for the extra hours. Hop and Haul does
exactly that one thing: prices the direct flight, prices every reasonable
fly-into-a-cheaper-hub-then-ground alternative, and applies one rule.

**The $200 rule:** only recommend the split if it saves $200 or more (this is a flag, change
it), unless the split is flatly better on both cost and time, or the extra hours are worth it
at your own stated value of time (`--vot`, $/hour).

If a split doesn't clear that bar, it recommends flying direct, even if the split
"technically" saved money. Marginal savings for hours of your day isn't a deal, and the tool
says so instead of just showing you the cheapest number.

## Quick start

```
git clone https://github.com/munzzyy/hopandhaul
cd hopandhaul
pip install -e .
hopandhaul-serve
```

Then open `http://127.0.0.1:8770` and click anywhere on the map.

No API keys needed to try it. Without them the app runs on transparent distance-based
estimates (see below). Add keys later for live fares; nothing else changes.

## What's real vs estimated

- **Live fares (Duffel, or Amadeus as a fallback)**: actual priced itineraries, when you set
  `DUFFEL_API_KEY` (or the Amadeus pair). No key set and no date entered falls back to
  estimates automatically, and the response says so.
- **Fare and ground-leg ESTIMATES**: a deterministic formula (distance, route-market
  competition, airport size, booking date) calibrated against real fares, not a live quote.
  Every estimate-based response is labeled `"pricing_source": "estimate"` and says so in
  plain English in the UI. It's a model, not a promise, so verify before booking.
  Ground-transport costs and times (train/bus/drive) are always estimates; there's no free,
  open multimodal fares API worth calling here.
- **Weather (OpenWeather)** and **geocoding/place search (Geoapify)** are both real, live,
  optional integrations. Off entirely if you don't set their keys.

## Features

- Deterministic split-vs-direct engine with the $200 rule (configurable threshold and value
  of time)
- Group-aware costs (per-person fares scale by travelers; a rental car doesn't)
- Round-trip aware (real return pricing when the provider supports it, a stated estimate
  otherwise)
- Gateway discovery — curated hub suggestions plus geometric fallback search, worldwide
- Click-anywhere map UI (Leaflet, self-hosted, no CDN)
- Destination weather for the date you're planning
- Cheapest vs greenest: a rough CO2 estimate per option, with the lowest-carbon one flagged
  separately from the recommendation — estimates, not a certified footprint, and never used to
  pick a winner for you
- Zero runtime dependencies — pure Python standard library, no `npm install`, no build step

## Architecture, briefly

- `trip.py`: the $200-rule math. Given a set of priced options, decides what to recommend and
  why.
- `geo.py`: the estimation model. Nearest airport, gateway discovery, and the distance-based
  fare/ground formulas.
- `duffel.py` / `providers.py`: live flight pricing (Duffel primary, Amadeus fallback).
  `flights.py` picks whichever is configured.
- `geoapify.py` / `weather.py`: geocoding and destination weather, both optional.
- `server.py`: the stdlib `http.server` app. Serves the UI and the JSON API, nothing else.

Every one of these is a plain, readable module you can open and check the reasoning of, not a
black box. See `docs/api.md` for the exact HTTP contract.

## Self-tests

Every module ships an offline self-test — no keys, no network, under a second total:

```
python -m hopandhaul.trip --selftest
python -m hopandhaul.geo --selftest
python -m hopandhaul.server --selftest
python -m hopandhaul.emissions --selftest
python -m hopandhaul.duffel --selftest
python -m hopandhaul.geoapify --selftest
python -m hopandhaul.weather --selftest
python -m hopandhaul.providers --selftest
```

## Configuration (all optional)

Copy `secrets.local.example.json` to `src/hopandhaul/secrets.local.json` and fill in real
keys, or set the equivalent environment variables (env always wins). See that file for the
full list. Nothing here is required to run the app in estimate mode.

## What this isn't

Not a booking site. It points you at the real flight/train/bus booking pages and stops
there. Not a price-prediction or buy-or-wait tool. Not a points/miles optimizer. Not a
hidden-city fare finder. No AI in the runtime path: the recommendation is deterministic math
you can read in `trip.py`, not a model's guess.

## Contributing / License / Security

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to run tests and the code-style/voice
expectations, [LICENSE](LICENSE) (MIT), and [SECURITY.md](SECURITY.md) for the security
posture and how to report a vulnerability.
