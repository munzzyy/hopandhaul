#!/usr/bin/env python3
"""
weather.py — destination weather via Open-Meteo (open-meteo.com). KEYLESS.

This used to be an OpenWeather integration behind an optional API key, which meant weather
was off for everyone who hadn't signed up for one. Open-Meteo serves the same "what's it like
there" need with no key at all (free for non-commercial use, CC-BY 4.0 — attribution lives in
the README), a 16-day daily forecast horizon instead of 5, and open CORS. Non-blocking by
design — if the call fails, callers get None and the trip still plans.

Data: current temperature/feels-like/humidity/wind + WMO weather code, and a daily forecast
row for the travel date when it's within the 16-day window. Same output shape the UI already
renders (temp/feels/desc/emoji/units/forecast) so nothing downstream changed.

Examples:
  python -m hopandhaul.weather 39.19 -106.82
  python -m hopandhaul.weather 39.19 -106.82 --date 2026-07-16
  python -m hopandhaul.weather --selftest     (offline, no network)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

from . import __version__
from .integrations import net

BASE = "https://api.open-meteo.com/v1/forecast"
UA = f"hopandhaul/{__version__} (https://github.com/munzzyy/hopandhaul)"
FORECAST_DAYS = 16

# 10-minute TTL: weather doesn't change fast enough to justify a live call per gateway click
# within one planning session.
_WEATHER_CACHE = net.TTLCache(ttl_seconds=600, max_size=256)

# WMO weather interpretation codes (the standard Open-Meteo returns) -> label + glyph.
_WMO = {
    0: ("clear sky", "☀️"), 1: ("mainly clear", "🌤️"), 2: ("partly cloudy", "⛅"),
    3: ("overcast", "☁️"), 45: ("fog", "🌫️"), 48: ("depositing rime fog", "🌫️"),
    51: ("light drizzle", "🌦️"), 53: ("drizzle", "🌦️"), 55: ("dense drizzle", "🌧️"),
    56: ("freezing drizzle", "🌧️"), 57: ("dense freezing drizzle", "🌧️"),
    61: ("light rain", "🌦️"), 63: ("rain", "🌧️"), 65: ("heavy rain", "🌧️"),
    66: ("freezing rain", "🌧️"), 67: ("heavy freezing rain", "🌧️"),
    71: ("light snow", "🌨️"), 73: ("snow", "🌨️"), 75: ("heavy snow", "❄️"),
    77: ("snow grains", "🌨️"), 80: ("light showers", "🌦️"), 81: ("showers", "🌧️"),
    82: ("violent showers", "⛈️"), 85: ("snow showers", "🌨️"), 86: ("heavy snow showers", "❄️"),
    95: ("thunderstorm", "⛈️"), 96: ("thunderstorm with hail", "⛈️"),
    99: ("thunderstorm with heavy hail", "⛈️"),
}


def available() -> bool:
    """Weather is always available now — Open-Meteo needs no key."""
    return True


def have_keys() -> bool:  # back-compat name; weather no longer needs a key at all
    return True


def _wmo(code) -> tuple[str, str]:
    try:
        return _WMO.get(int(code), ("", "🌡️"))
    except (TypeError, ValueError):
        return ("", "🌡️")


def _units_symbol(units: str) -> str:
    return {"imperial": "°F", "metric": "°C"}.get(units, "°")


def _http_json(url: str, timeout: int = 12) -> dict:
    return net.fetch_json(url, headers={"User-Agent": UA, "Accept": "application/json"},
                          timeout=timeout)


def current(lat: float, lng: float, units: str = "imperial", timeout: int = 12) -> dict | None:
    """Current conditions at a point, or None if unavailable."""
    cache_key = ("cur", round(lat, 3), round(lng, 3), units)
    cached = _WEATHER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    params = {
        "latitude": f"{lat}", "longitude": f"{lng}",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "weather_code,wind_speed_10m",
        "timezone": "auto",
    }
    if units == "imperial":
        params["temperature_unit"] = "fahrenheit"
        params["wind_speed_unit"] = "mph"
    out = _http_json(BASE + "?" + urllib.parse.urlencode(params), timeout=timeout)
    cur = out.get("current") or {}
    desc, emoji = _wmo(cur.get("weather_code"))
    result = {
        "temp": round(cur["temperature_2m"]) if cur.get("temperature_2m") is not None else None,
        "feels": (round(cur["apparent_temperature"])
                  if cur.get("apparent_temperature") is not None else None),
        "humidity": cur.get("relative_humidity_2m"),
        "desc": desc,
        "emoji": emoji,
        "wind_mph": (round(cur["wind_speed_10m"])
                     if units == "imperial" and cur.get("wind_speed_10m") is not None else None),
        "units": _units_symbol(units),
        "place": None,                  # Open-Meteo is coordinates-in, conditions-out
        "source": "open-meteo",
    }
    _WEATHER_CACHE.set(cache_key, result)
    return result


def _forecast_for_date(lat: float, lng: float, date: str, units: str = "imperial",
                       timeout: int = 12) -> dict | None:
    """Daily forecast row for a specific date, or None when outside the 16-day horizon."""
    cache_key = ("fc", round(lat, 3), round(lng, 3), date, units)
    cached = _WEATHER_CACHE.get(cache_key)
    if cached is not None:
        return cached or None
    params = {
        "latitude": f"{lat}", "longitude": f"{lng}",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max",
        "start_date": date, "end_date": date,
        "timezone": "auto",
    }
    if units == "imperial":
        params["temperature_unit"] = "fahrenheit"
    try:
        out = _http_json(BASE + "?" + urllib.parse.urlencode(params), timeout=timeout)
    except net.FetchError as e:
        # Open-Meteo answers a date beyond its horizon with a 400, not an empty row —
        # that's "no forecast yet", not an outage.
        if e.status == 400:
            _WEATHER_CACHE.set(cache_key, {})
            return None
        raise
    daily = out.get("daily") or {}
    times = daily.get("time") or []
    if not times:
        _WEATHER_CACHE.set(cache_key, {})
        return None
    hi = (daily.get("temperature_2m_max") or [None])[0]
    lo = (daily.get("temperature_2m_min") or [None])[0]
    desc, emoji = _wmo((daily.get("weather_code") or [None])[0])
    precip = (daily.get("precipitation_probability_max") or [None])[0]
    if precip is not None:
        desc = f"{desc}, {precip}% precip" if desc else f"{precip}% precip"
    result = {
        "date": times[0],
        "temp": round(hi) if hi is not None else None,
        "temp_lo": round(lo) if lo is not None else None,
        "desc": desc,
        "emoji": emoji,
        "units": _units_symbol(units),
        "at": f"{times[0]} daily",
    }
    _WEATHER_CACHE.set(cache_key, result)
    return result


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
            out["forecast_note"] = (f"Beyond the {FORECAST_DAYS}-day forecast — "
                                    "showing current conditions.")
    return out


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(description="Destination weather via Open-Meteo (keyless).")
    p.add_argument("coords", nargs="*", help="LAT LNG")
    p.add_argument("--date", default=None, help="travel date YYYY-MM-DD")
    p.add_argument("--metric", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
    if len(args.coords) != 2:
        p.error("give LAT LNG")
    units = "metric" if args.metric else "imperial"
    wx = for_point(float(args.coords[0]), float(args.coords[1]), date=args.date, units=units)
    if not wx:
        print("weather unavailable")
        return 1
    if args.json:
        print(json.dumps(wx, indent=2, ensure_ascii=False))
    else:
        print(f"{wx['emoji']}  {wx['temp']}{wx['units']} (feels {wx['feels']}{wx['units']}) "
              f"— {wx['desc']}")
        fc = wx.get("forecast")
        if fc:
            lo = f"/{fc['temp_lo']}{fc['units']}" if fc.get("temp_lo") is not None else ""
            print(f"{fc['emoji']}  {fc['date']}: {fc['temp']}{fc['units']}{lo} — {fc['desc']}")
        elif wx.get("forecast_note"):
            print(wx["forecast_note"])
    return 0


# --------------------------------------------------------------------------- self-test (offline)
def selftest():
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    check("weather is keyless now — available()/have_keys() are True with no env at all",
          available() is True and have_keys() is True)
    check("WMO code map: clear/overcast/rain/snow/thunder all resolve",
          _wmo(0)[0] == "clear sky" and _wmo(3)[0] == "overcast"
          and "rain" in _wmo(63)[0] and "snow" in _wmo(73)[0] and "thunder" in _wmo(95)[0])
    check("unknown/None WMO codes degrade to a neutral glyph, not a crash",
          _wmo(42)[1] == "🌡️" and _wmo(None)[1] == "🌡️" and _wmo("x")[1] == "🌡️")
    check("units symbol", _units_symbol("imperial") == "°F" and _units_symbol("metric") == "°C")
    check("UA identifies the project", "hopandhaul" in UA)

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (offline checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
