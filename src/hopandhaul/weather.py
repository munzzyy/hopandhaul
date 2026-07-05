#!/usr/bin/env python3
"""
weather.py — destination weather for travel-scout via the OpenWeather API (free tier).

Adds "what's it like there" to a plan: current conditions at the destination, plus a near-term
forecast for the travel date when it falls inside OpenWeather's free 5-day window. Non-blocking
by design — if the key is missing/not-yet-active or the call fails, callers get None and the
trip still plans.

Key: OPENWEATHER_API_KEY (env or secrets.local.json). Stdlib urllib only.
Note: a brand-new OpenWeather key can take up to ~2h to activate (401 until then).

Endpoints used (both on the free plan):
  /data/2.5/weather   — current conditions
  /data/2.5/forecast  — 5-day / 3-hour forecast

Examples:
  python -m hopandhaul.weather 39.19 -106.82
  python -m hopandhaul.weather 39.19 -106.82 --date 2026-07-05
  python -m hopandhaul.weather --selftest
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

from . import _secrets
from .integrations import net

BASE = "https://api.openweathermap.org/data/2.5"

# 10-minute TTL: weather doesn't change fast enough to justify a live call per gateway click
# within one planning session, and the free tier's quota is the tightest of the four providers.
_WEATHER_CACHE = net.TTLCache(ttl_seconds=600, max_size=256)


def have_keys() -> bool:
    return _secrets.has("OPENWEATHER_API_KEY")


def _emoji(weather_id: int, icon: str = "") -> str:
    """Map an OpenWeather condition id (+ day/night icon suffix) to an emoji."""
    night = icon.endswith("n")
    if 200 <= weather_id < 300:
        return "⛈️"
    if 300 <= weather_id < 400:
        return "🌦️"
    if 500 <= weather_id < 600:
        return "🌧️"
    if 600 <= weather_id < 700:
        return "🌨️"
    if 700 <= weather_id < 800:
        return "🌫️"
    if weather_id == 800:
        return "🌙" if night else "☀️"
    if weather_id == 801:
        return "🌤️"
    if 802 <= weather_id < 900:
        return "☁️"
    return "🌡️"


def _http_json(url: str, timeout: int = 12) -> dict:
    return net.fetch_json(url, headers={"Accept": "application/json"}, timeout=timeout)


def _units_symbol(units: str) -> str:
    return {"imperial": "°F", "metric": "°C"}.get(units, "K")


def current(lat: float, lng: float, units: str = "imperial", timeout: int = 12) -> dict | None:
    """Current conditions at a point, or None if unavailable."""
    if not have_keys():
        return None
    cache_key = ("cur", round(lat, 3), round(lng, 3), units)
    cached = _WEATHER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    params = {"lat": f"{lat}", "lon": f"{lng}", "units": units,
              "appid": _secrets.get("OPENWEATHER_API_KEY")}
    out = _http_json(f"{BASE}/weather?" + urllib.parse.urlencode(params), timeout=timeout)
    w = (out.get("weather") or [{}])[0]
    main = out.get("main") or {}
    wid = int(w.get("id", 0) or 0)
    result = {
        "temp": round(main.get("temp")) if main.get("temp") is not None else None,
        "feels": round(main.get("feels_like")) if main.get("feels_like") is not None else None,
        "humidity": main.get("humidity"),
        "desc": (w.get("description") or "").strip(),
        "emoji": _emoji(wid, w.get("icon", "")),
        "wind_mph": round(out.get("wind", {}).get("speed", 0)) if units == "imperial" else None,
        "units": _units_symbol(units),
        "place": out.get("name") or None,
        "source": "openweather",
    }
    _WEATHER_CACHE.set(cache_key, result)
    return result


def _local_dt(unix_utc: int, tz_offset_s: int):
    """UTC unix timestamp + a UTC-offset in seconds -> local datetime at the destination.
    dt_txt in OpenWeather's forecast rows is UTC text, not local time; string-slicing it
    directly (the previous approach) silently mispicks the day near midnight for any
    destination whose UTC offset crosses a date boundary from the query time."""
    return datetime.fromtimestamp(unix_utc, tz=timezone.utc) + timedelta(seconds=tz_offset_s)


def _forecast_for_date(lat: float, lng: float, date: str, units: str = "imperial",
                       timeout: int = 12) -> dict | None:
    """Nearest 3-hour forecast slot to <date> 12:00 **local time at the destination**, if
    within the 5-day window."""
    params = {"lat": f"{lat}", "lon": f"{lng}", "units": units,
              "appid": _secrets.get("OPENWEATHER_API_KEY")}
    out = _http_json(f"{BASE}/forecast?" + urllib.parse.urlencode(params), timeout=timeout)
    rows = out.get("list") or []
    tz_offset = int((out.get("city") or {}).get("timezone", 0) or 0)
    dated = []
    for r in rows:
        dt = r.get("dt")
        if dt is None:
            continue
        local = _local_dt(int(dt), tz_offset)
        if local.strftime("%Y-%m-%d") == date:
            dated.append((r, local))
    if not dated:
        return None  # date is outside the 5-day forecast horizon
    # pick the slot closest to local midday (12:00) for a representative daytime read
    r, local = min(dated, key=lambda pair: abs(pair[1].hour - 12))
    w = (r.get("weather") or [{}])[0]
    main = r.get("main") or {}
    wid = int(w.get("id", 0) or 0)
    return {
        "date": date,
        "temp": round(main.get("temp")) if main.get("temp") is not None else None,
        "desc": (w.get("description") or "").strip(),
        "emoji": _emoji(wid, w.get("icon", "")),
        "units": _units_symbol(units),
        "at": local.strftime("%Y-%m-%d %H:%M:%S") + " local",
    }


def for_point(lat: float, lng: float, date: str | None = None, units: str = "imperial",
              timeout: int = 12) -> dict | None:
    """Current conditions + (best-effort) forecast for the travel date. None if unavailable."""
    try:
        cur = current(lat, lng, units=units, timeout=timeout)
    except (net.FetchError, ValueError, KeyError):
        return None
    if not cur:
        return None
    out = dict(cur)
    if date:
        try:
            fc = _forecast_for_date(lat, lng, date, units=units, timeout=timeout)
        except (net.FetchError, ValueError, KeyError):
            fc = None
        out["forecast"] = fc
        if fc is None:
            out["forecast_note"] = "Beyond the 5-day forecast — showing current conditions."
    return out


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(description="OpenWeather conditions for travel-scout.")
    p.add_argument("lat", nargs="?", type=float)
    p.add_argument("lng", nargs="?", type=float)
    p.add_argument("--date", help="travel date YYYY-MM-DD (adds a forecast if within 5 days)")
    p.add_argument("--units", default="imperial", choices=["imperial", "metric"])
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
    if not have_keys():
        print("OPENWEATHER_API_KEY not set (env or secrets.local.json). Weather unavailable.")
        return 2
    if args.lat is None or args.lng is None:
        p.error("give LAT LNG")

    res = for_point(args.lat, args.lng, date=args.date, units=args.units)
    if res is None:
        print("Weather unavailable (key inactive or no data).")
        return 1
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"  {res['emoji']} {res['temp']}{res['units']} (feels {res['feels']}{res['units']}) "
              f"· {res['desc']} · humidity {res.get('humidity')}%")
        fc = res.get("forecast")
        if fc:
            print(f"  forecast {fc['date']}: {fc['emoji']} {fc['temp']}{fc['units']} · {fc['desc']}")
        elif res.get("forecast_note"):
            print(f"  {res['forecast_note']}")
    return 0


# --------------------------------------------------------------------------- self-test (offline)
def selftest():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    check("clear-day -> sun emoji", _emoji(800, "01d") == "☀️")
    check("clear-night -> moon emoji", _emoji(800, "01n") == "🌙")
    check("rain -> rain emoji", _emoji(500, "10d") == "🌧️")
    check("snow -> snow emoji", _emoji(600, "13d") == "🌨️")
    check("thunder -> storm emoji", _emoji(211, "11d") == "⛈️")
    check("units symbol F/C", _units_symbol("imperial") == "°F" and _units_symbol("metric") == "°C")
    check("have_keys() is a bool", isinstance(have_keys(), bool))

    # local-noon timezone fix: a UTC timestamp just after local midnight in a UTC+13 zone
    # (e.g. Auckland) must resolve to the NEXT calendar day locally, not the UTC day.
    # 2026-07-05 23:00 UTC + 13h offset = 2026-07-06 12:00 local.
    utc_ts = int(datetime(2026, 7, 5, 23, 0, 0, tzinfo=timezone.utc).timestamp())
    local = _local_dt(utc_ts, 13 * 3600)
    check("local_dt rolls the date forward across a UTC+13 offset",
          local.strftime("%Y-%m-%d") == "2026-07-06" and local.hour == 12)
    local_neg = _local_dt(utc_ts, -8 * 3600)
    check("local_dt rolls the date backward across a negative offset",
          local_neg.strftime("%Y-%m-%d") == "2026-07-05")

    _WEATHER_CACHE.set(("cur", 39.19, -106.82, "imperial"), {"temp": 70})
    check("weather cache stores under the (kind, lat, lng, units) key",
          _WEATHER_CACHE.get(("cur", 39.19, -106.82, "imperial")) == {"temp": 70})
    check("net.TTLCache is the backing cache type", isinstance(_WEATHER_CACHE, net.TTLCache))

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (offline checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
