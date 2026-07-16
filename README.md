# Hop and Haul

**Flies you into the airport that's actually cheap, then tells you honestly whether the
train ride from there is worth it.**

[![CI](https://github.com/munzzyy/hopandhaul/actions/workflows/ci.yml/badge.svg)](https://github.com/munzzyy/hopandhaul/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/hopandhaul)](https://pypi.org/project/hopandhaul/)
[![License: Prosperity 3.0.0](https://img.shields.io/badge/license-Prosperity--3.0.0-blue.svg)](LICENSE)
![zero dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)

> **Try it live:** [munzzyy.github.io/hopandhaul](https://munzzyy.github.io/hopandhaul/).
> No install, no keys, runs entirely in your browser.

![Click a destination and the recommendation card answers with the math: cost, time, CO2 per option, the $200 rule applied](docs/media/app-dark.png)

Click anywhere on the map and a recommendation card slides in — cost, time, and a CO2
estimate for every option side by side. A copy-link button turns the plan into a URL you
can send someone. That's a live screenshot, not a mockup: click a destination in the app
above, or run it locally with no API keys (about 30 seconds, below), then open
`http://127.0.0.1:8770/?lat=39.1911&lng=-106.8175&place=Aspen,+CO&origin=JFK`
to reproduce a trip like it.

**20-second demo — plan a trip, switch the UI to French, then flip the whole layout to
Arabic:**

![Animated demo: planning a trip, then switching the UI language to French and Arabic with full RTL mirroring](docs/media/demo.gif)

## The idea

Sometimes the cheapest way to get somewhere isn't flying there directly. It's flying into a
nearby hub where flights are cheap and plentiful, then covering the last leg by train, bus,
ferry, or rental car.

Google Flights and Kayak will search nearby airports for you. None of them tell you whether
the split is actually worth it once you account for the extra hours. Rome2Rio will show you
every possible multimodal combination between two points, but it doesn't tell you which one
actually beats flying direct. Hop and Haul does exactly that one thing: prices the direct
flight, prices every reasonable fly-into-a-cheaper-hub-then-ground alternative, and applies
one rule.

**The $200 rule:** only recommend the split if it saves $200 or more (this is a flag, change
it), unless the split is flatly better on both cost and time, or the extra hours are worth it
at your own stated value of time (`--vot`, $/hour).

If a split doesn't clear that bar, it recommends flying direct, even if the split
"technically" saved money. Marginal savings for hours of your day isn't a deal, and the tool
says so instead of just showing you the cheapest number.

## What this isn't

Not a booking site. It points you at the real flight/train/bus booking pages and stops
there. Not a price-prediction or buy-or-wait tool. Not a points/miles optimizer. Not a
hidden-city fare finder. No AI in the runtime path: the recommendation is deterministic math
you can read in `trip.py`, not a model's guess.

## Try it in your browser

**[munzzyy.github.io/hopandhaul](https://munzzyy.github.io/hopandhaul/)** — the whole app,
running client-side on GitHub Pages. Nothing to install, no keys, no server. It's the same
estimate engine ported to JS, and CI holds the port to exact numeric agreement with the
Python one.

## Quick start

```
pip install hopandhaul
hopandhaul-serve
```

Then open `http://127.0.0.1:8770` and click anywhere on the map. Or skip the map entirely:

```
hopandhaul go JFK "Tallinn" --date 2026-08-15
```

No API keys needed for any of that. Weather, place search, real ferry routes, real US fare
data, and real ground-transport timetables all work out of the box — the free sources below.
A Duffel key adds live airfares; nothing else needs one.

Hacking on the code instead? Clone and dev-install:

```
git clone https://github.com/munzzyy/hopandhaul
cd hopandhaul
pip install -e .
```

What changed in each version is in the
[release notes](https://github.com/munzzyy/hopandhaul/releases).

## Multi-city trips

Visiting several cities on one trip? `hopandhaul multicity` works out a good order to hit
them in, pricing every leg with the same fly-cheaper-hub-then-ground logic and $200 rule as
everything else here, then prints the ordered itinerary and a total:

```
hopandhaul multicity --home JFK --visit "Aspen,Boston,Chicago" --threshold 50
```

```
MULTI-CITY TOUR: JFK -> ASE -> ORD -> BOS -> JFK
round trip, 4 stops, solved via held-karp (exact)

ITINERARY:
  1. JFK -> ASE     $215    9h54  multimodal (fly $140 + bus $75)
  2. ASE -> ORD     $170    3h06  direct     (fly $170)
  3. ORD -> BOS     $105    3h06  direct     (fly $105)
  4. BOS -> JFK      $70    1h30  direct     (fly $70)

TOTAL: $560 across 4 legs
```

That first leg is the point: at a $50 threshold, flying into Denver and taking a bus the
rest of the way to Aspen beats a direct flight, so the tour routes through DEN instead of
pricing every leg as a straight flight. `--open` ends the tour at the last stop instead of
looping back home, and `--travelers N` scales group costs the same way the rest of the
engine does. Up to about 9 cities it solves exactly (Held-Karp); past that it switches to a
nearest-neighbor-plus-2-opt heuristic and says so in the output.

## Which date is actually cheapest

Every other command here prices one `--date`. `hopandhaul dates` checks several at once and
tells you which one wins, pricing each candidate the exact same way `hopandhaul duffel`
prices a single day — nothing is reimplemented, it's the same engine called once per date:

```
hopandhaul dates --from JFK --to ASE --date 2026-08-15 --window 3 --auto-gateways
```

```
CHEAPEST DATE SWEEP: JFK -> ASE
anchor 2026-08-15 +/- 3 day(s) (7 date(s) checked; dates already past are skipped)

   2026-08-12       $610     6h12   live       Fly direct to ASE
   2026-08-13       $590     6h12   live       Fly direct to ASE
   2026-08-14       $605     6h12   live       Fly direct to ASE
 → 2026-08-15       $455     8h54   live       DEN + bus
   2026-08-16       $620     6h12   live       Fly direct to ASE
   2026-08-17       $640     6h12   live       Fly direct to ASE
   2026-08-18       $600     6h12   live       Fly direct to ASE

CHEAPEST: 2026-08-15 - $455 via DEN + bus (live-priced)
```

With a `DUFFEL_API_KEY` set, every date is a real fare lookup. With no key, every date runs
through the same calibrated distance estimate the rest of the tool falls back to. Either way
each row is tagged with its basis: `live`, `estimate`, or `mixed` for a date whose
recommended option has one flight leg priced live and another (a thin gateway route Duffel
has nothing for that day, say) fell back, so a model number never gets mistaken for a
checked fare. The window centers on `--date`, 3 days each side by default, `--window` caps
out at 7, so a plain run won't fire off dozens of live lookups by accident. Repeat or
overlapping sweeps ride `hopandhaul duffel`'s own per-date cache, so checking the same date
twice costs nothing extra. `--return-date` shifts along with each candidate departure, so a
round trip's length stays fixed while its placement in the window moves. Takes the same
`--gateway` / `--auto-gateways` / `--adults` / `--cabin` / `--nonstop` / `--threshold` /
`--vot` flags as `hopandhaul duffel`.

## What's real vs estimated

More of this tool is real data than you'd guess for something with zero keys:

- **Ferry legs are real routes.** The engine ships a researched database of 85 passenger-ferry
  corridors — actual ports, operators, crossing times, sailing frequencies, and fare bands
  checked against operator/aggregator pages (each entry cites its source and date). A boat
  only appears if it exists: there's no Helsinki–Tallinn train over the Baltic here, and no
  ferry to Maui, because there is no ferry to Maui.
- **Ground-transport schedules are real timetables** when [Transitous](https://transitous.org)
  (a community-run journey planner over worldwide GTFS, keyless) knows the route: real
  operators, real departures, real door-to-door times, labeled "live schedule" per leg. Fares
  on those legs are still estimates — schedules are open data, ticket prices mostly aren't.
- **US fares are anchored to what passengers actually paid.** The bundled
  [BTS Consumer Airfare Report](https://data.transportation.gov/d/yj5y-b2ir) extract (public
  domain) carries real average fares for ~4,100 US city-pair markets; the model is clamped
  into each route's real band, and the itinerary shows the real market numbers next to the
  estimate.
- **Live airfares (Duffel)**: actual priced itineraries when you set `DUFFEL_API_KEY` — real
  carrier, flight number, and clock times, labeled "live" instead of "example." No key falls
  back to the labeled estimate automatically. (The old Amadeus fallback is gone: Amadeus shut
  its self-service API down in July 2026.)
- **Everything else is a labeled ESTIMATE**: a deterministic formula (distance, route-market
  competition, airport size, booking date) calibrated against real fares. Every estimate says
  so — `"pricing_source": "estimate"` in the API, plain English in the UI, per-leg provenance
  in the itinerary. It's a model, not a promise; verify before booking.
- **Weather ([Open-Meteo](https://open-meteo.com))** and **place search
  ([Photon](https://photon.komoot.io))** are real, live, and keyless. A Geoapify key upgrades
  search to full address-level geocoding if you want it.

## Features

- Every priced option shows its work: a leg-by-leg itinerary with real airport names, an
  example clock schedule (or the real one, once a live fare is priced), what each leg's price
  is based on, and a one-click link to check it — Google Flights for a flight leg, Rome2Rio for
  ground. No number without a way to check it.
- Boats, honestly: real ferry corridors as first-class legs (fly to Athens, take the real
  Blue Star boat to Santorini), and a land/water grid that stops the engine from routing a
  train across open sea when no bridge or tunnel exists
- `hopandhaul go A B` — the whole pipeline in one terminal command, zero keys
- `hopandhaul multicity` — order N cities into one trip (exact for small N, a
  nearest-neighbor + 2-opt heuristic beyond that), reusing the same split-vs-direct pricing
  leg by leg
- `hopandhaul dates` — sweep a bounded window of dates and find the actually cheapest one to
  fly, live-priced when a Duffel key is set, labeled per date so you know which
- Deterministic split-vs-direct engine with the $200 rule (configurable threshold and value
  of time)
- Group-aware costs (per-person fares scale by travelers; a rental car doesn't)
- Round-trip aware (real return pricing when the provider supports it, a stated estimate
  otherwise)
- Gateway discovery — curated hub suggestions plus geometric fallback search, worldwide
- Click-anywhere map UI (Leaflet self-hosted; map tiles stream from CARTO's servers)
- UI in 46 languages, four of them fully right-to-left, behind a hand-rolled i18n runtime
  instead of a framework — pick yours from the globe button
- Eight themes plus Auto, picked from the header: Departure Board, Boarding Pass, Night
  Flight (OLED), a CRT-amber Terminal, High Contrast, Rail Poster, Old Map, and Coastal
- Destination weather for the date you're planning
- Cheapest vs greenest: a rough CO2 estimate per option, with the lowest-carbon one flagged
  separately from the recommendation — estimates, not a certified footprint, and never used to
  pick a winner for you
- Zero runtime dependencies — pure Python standard library, no `npm install`, no build step

## Speaks your language

The whole UI ships in 46 languages — the big ones, plus Catalan, Icelandic, Swahili,
Filipino, and both Chinese scripts. Arabic, Hebrew, Persian, and Urdu mirror the entire
layout right-to-left, map panels included. Detection follows your browser, your pick
sticks in localStorage, and a language whose catalog fails to load falls back to English
instead of breaking.

| | |
|---|---|
| ![The language picker: filterable list of 46 languages with native names](docs/media/language-modal.png) | ![The app in Arabic: fully mirrored right-to-left layout](docs/media/app-arabic-rtl.png) |

Native speaker and you spot something off? A translation fix in
`src/hopandhaul/ui/i18n/<code>.json` is about the friendliest PR there is.

## Architecture, briefly

- `trip.py`: the $200-rule math. Given a set of priced options, decides what to recommend and
  why.
- `geo.py`: the estimation model. Nearest airport, gateway discovery, and the distance-based
  fare/ground formulas.
- `itinerary.py`: turns a priced option into a leg-by-leg timeline — real airport names, an
  example (or, with a live fare, real) clock schedule, per-leg price provenance, and a verify
  link. No invented flight numbers, no fake departure-time precision, no pretending a
  longitude-based guess is a real timezone — see the module docstring for the honesty rules.
- `duffel.py`: live flight pricing (optional key). `flights.py` is the thin interface
  server.py talks to.
- `dates.py`: sweeps a bounded window of dates through `duffel.py`'s own
  `build_and_evaluate()` — one call per candidate date, no separate pricing logic — and
  reports whichever one is actually cheapest.
- `transit.py`: real ground schedules via Transitous (keyless). `places.py`: place search,
  Photon by default (keyless), Geoapify when keyed. `weather.py`: Open-Meteo (keyless).
- `go.py`: the one-shot CLI — resolve places, plan, print the report and itineraries.
- `multicity.py`: the multi-city tour optimizer — a plain TSP solver (Held-Karp, exact, for
  small city counts; nearest-neighbor + 2-opt above that) over a cost matrix built by pricing
  every leg through `geo.py`/`trip.py`, the same way `go.py`/`server.py` price one.
- `server.py`: the stdlib `http.server` app. Serves the UI and the JSON API, nothing else.
- `data/`: the bundled real-world datasets — 4,175 airports (OurAirports), 85 ferry corridors
  (researched, sourced per entry), a 0.25° land/water grid (Natural Earth), and real US
  market fares (BTS). `tools/` has the scripts that regenerate them.

Every one of these is a plain, readable module you can open and check the reasoning of, not a
black box. See `docs/api.md` for the exact HTTP contract.

## Self-tests

Every module ships an offline self-test — no keys, no network, under a second total:

```
python -m hopandhaul.trip --selftest
python -m hopandhaul.geo --selftest
python -m hopandhaul.server --selftest
python -m hopandhaul.emissions --selftest
python -m hopandhaul.itinerary --selftest
python -m hopandhaul.duffel --selftest
python -m hopandhaul.geoapify --selftest
python -m hopandhaul.places --selftest
python -m hopandhaul.transit --selftest
python -m hopandhaul.weather --selftest
python -m hopandhaul.go --selftest
python -m hopandhaul.multicity --selftest
python -m hopandhaul.dates --selftest
```

## Configuration (all optional)

Weather, place search, ferry data, US fare anchors, and live ground schedules need no
configuration at all. Two keys exist, both optional, both read from env vars (which work for
a repo checkout and a real `pip install` alike):

- **`DUFFEL_API_KEY`** — live airfares. [app.duffel.com/join](https://app.duffel.com/join),
  instant sandbox access, no card required. A test-mode key (`duffel_test_...`) exercises the
  live-pricing code path against Duffel's test airline; real fares need a live key.
- **`GEOAPIFY_API_KEY`** — upgrades place search from Photon to full address-level geocoding.
  [geoapify.com](https://www.geoapify.com/), free without a card, 3,000 requests/day.

If you're working from a repo checkout (not a wheel install), there's also a
`secrets.local.example.json` you can copy to `src/hopandhaul/secrets.local.json` and fill in
instead. It's a convenience for local dev only: it isn't packaged into the wheel.

## Data sources and attribution

The bundled datasets and keyless services this tool leans on, with licenses:

- **[OurAirports](https://ourairports.com/data/)** — the 4,175-airport database (public
  domain).
- **[Natural Earth](https://www.naturalearthdata.com/)** — the land/water grid is rasterized
  from their 1:50m land polygons (public domain).
- **[US DOT/BTS Consumer Airfare Report](https://data.transportation.gov/d/yj5y-b2ir)** —
  real US city-pair market fares (US government work, public domain).
- **Ferry corridors** — researched by hand from operator and aggregator pages; every entry in
  `data/ferries.json` carries its own source URL and as-of date.
- **[Transitous](https://transitous.org/sources/)** — community-run journey planning over
  worldwide GTFS feeds and OpenStreetMap data; free for non-commercial/open-source use.
- **[Photon](https://photon.komoot.io)** by komoot — keyless geocoding over OpenStreetMap
  data. Map data on both: © OpenStreetMap contributors,
  [ODbL](https://www.openstreetmap.org/copyright).
- **[Open-Meteo](https://open-meteo.com)** — weather, CC-BY 4.0, free for non-commercial use.
- **[frankfurter.dev](https://frankfurter.dev)** — daily ECB exchange rates for converting
  non-USD live fares; the bundled approximate table is the offline fallback.
- **[CARTO](https://carto.com/attributions)** basemap tiles © OpenStreetMap contributors.

## Contributing / License / Security

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to run tests and the code-style/voice
expectations, [LICENSE](LICENSE) (Prosperity Public License, free for noncommercial use), and [SECURITY.md](SECURITY.md) for the security
posture and how to report a vulnerability.

## Support

If hopandhaul found you a cheaper way there, [sponsoring](https://github.com/sponsors/munzzyy) is what keeps the airport data fresh.
