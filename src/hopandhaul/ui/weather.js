// Destination weather for the static build, from Open-Meteo (keyless, CORS-open). Same requests
// and payload shape as weather.py. No t() import, so tests/ui/weather.test.mjs can load it bare.
import { pyRound } from "./engine/pyround.js";

const BASE = "https://api.open-meteo.com/v1/forecast";
const TIMEOUT_MS = 5000;
export const FORECAST_DAYS = 16;

// Keys must match weather.py's _WMO and en.json's wx.code.<n>; weather.test.mjs checks all three.
export const WMO_GLYPHS = {
  0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️", 45: "🌫️", 48: "🌫️",
  51: "🌦️", 53: "🌦️", 55: "🌧️", 56: "🌧️", 57: "🌧️",
  61: "🌦️", 63: "🌧️", 65: "🌧️", 66: "🌧️", 67: "🌧️",
  71: "🌨️", 73: "🌨️", 75: "❄️", 77: "🌨️", 80: "🌦️", 81: "🌧️",
  82: "⛈️", 85: "🌨️", 86: "❄️", 95: "⛈️", 96: "⛈️", 99: "⛈️",
};
const NO_GLYPH = "🌡️";

function knownCode(code) {
  const n = Number(code);
  return code != null && Number.isInteger(n) && Object.prototype.hasOwnProperty.call(WMO_GLYPHS, n) ? n : null;
}

/** The localized description for a WMO code, or "" for a code this app has no words for. */
export function describe(code, t) {
  const n = knownCode(code);
  if (n == null) return "";
  const key = `wx.code.${n}`;
  const s = t(key);
  return s === key ? "" : s;
}

function glyph(code) {
  const n = knownCode(code);
  return n == null ? NO_GLYPH : WMO_GLYPHS[n];
}

// pyRound so 70.5°F reads 70 here as it does from the server.
const round = (v) => (v == null ? null : pyRound(v));

async function getJson(fetchImpl, params) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetchImpl(`${BASE}?${new URLSearchParams(params)}`, { signal: controller.signal });
    return { status: res.status, body: res.ok ? await res.json() : null };
  } finally {
    clearTimeout(timer);
  }
}

async function forecastFor(fetchImpl, lat, lng, date) {
  const { status, body } = await getJson(fetchImpl, {
    latitude: String(lat), longitude: String(lng),
    daily: "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
    start_date: date, end_date: date,
    timezone: "auto", temperature_unit: "fahrenheit",
  });
  // Open-Meteo answers a date past its horizon with a 400: no forecast yet, not an outage.
  if (status === 400 || !body) return null;
  const daily = body.daily || {};
  const times = daily.time || [];
  if (!times.length) return null;
  const code = (daily.weather_code || [null])[0];
  return {
    date: times[0],
    temp: round((daily.temperature_2m_max || [null])[0]),
    temp_lo: round((daily.temperature_2m_min || [null])[0]),
    code: code ?? null,
    precip: (daily.precipitation_probability_max || [null])[0] ?? null,
    emoji: glyph(code),
    units: "°F",
    at: `${times[0]} daily`,
  };
}

/** Current conditions plus, when `date` is given, that day's forecast. Resolves to null when
 * offline (without touching the network), on any failure, or on a malformed answer. */
export async function fetchWeather(lat, lng, date, { fetchImpl = globalThis.fetch, offline = false } = {}) {
  if (offline || typeof fetchImpl !== "function") return null;
  try {
    const { body } = await getJson(fetchImpl, {
      latitude: String(lat), longitude: String(lng),
      current: "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
      timezone: "auto", temperature_unit: "fahrenheit", wind_speed_unit: "mph",
    });
    const cur = body && body.current;
    if (!cur) return null;
    const out = {
      temp: round(cur.temperature_2m),
      feels: round(cur.apparent_temperature),
      humidity: cur.relative_humidity_2m ?? null,
      code: cur.weather_code ?? null,
      emoji: glyph(cur.weather_code),
      wind_mph: round(cur.wind_speed_10m),
      units: "°F",
      place: null,
      source: "open-meteo",
    };
    if (date) {
      let fc = null;
      try {
        fc = await forecastFor(fetchImpl, lat, lng, date);
      } catch {
        fc = null;
      }
      out.forecast = fc;
      if (!fc) {
        out.forecast_note = `Beyond the ${FORECAST_DAYS}-day forecast: showing current conditions.`;
        out.forecast_days = FORECAST_DAYS;
      }
    }
    return out;
  } catch {
    return null;
  }
}
