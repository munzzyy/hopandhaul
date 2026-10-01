#!/usr/bin/env node
// ui/weather.js against a stubbed fetch, no network. Run: node tests/ui/weather.test.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PKG = path.resolve(HERE, "..", "..", "src", "hopandhaul");
const { fetchWeather, describe, WMO_GLYPHS, FORECAST_DAYS } = await import(
  pathToFileURL(path.join(PKG, "ui", "weather.js")).href);

const catalog = (code) => JSON.parse(readFileSync(path.join(PKG, "ui", "i18n", `${code}.json`), "utf8"));
const tFor = (cat) => (key, params) => {
  const raw = Object.prototype.hasOwnProperty.call(cat, key) ? cat[key] : key;
  return raw.replace(/\{(\w+)\}/g, (m, k) => (params && k in params ? String(params[k]) : m));
};

const CURRENT = { current: { temperature_2m: 70.4, apparent_temperature: 68.6, relative_humidity_2m: 40,
  weather_code: 61, wind_speed_10m: 5.2 } };
const DAILY = { daily: { time: ["2030-06-15"], weather_code: [95], temperature_2m_max: [80.1],
  temperature_2m_min: [60.2], precipitation_probability_max: [40] } };

function stubFetch({ dailyStatus = 200 } = {}) {
  const calls = [];
  const impl = async (url) => {
    calls.push(String(url));
    const daily = String(url).includes("daily=");
    const status = daily ? dailyStatus : 200;
    return { ok: status === 200, status, json: async () => (daily ? DAILY : CURRENT) };
  };
  return { calls, impl };
}

let fails = 0;
function check(name, cond) {
  console.log(`  [${cond ? "PASS" : "FAIL"}] ${name}`);
  if (!cond) fails++;
}

{
  const s = stubFetch();
  const wx = await fetchWeather(39.19, -106.82, "2030-06-15", { fetchImpl: s.impl, offline: true });
  check("offline: resolves null and makes zero network calls", wx === null && s.calls.length === 0);
}
{
  const s = stubFetch();
  const wx = await fetchWeather(39.19, -106.82, null, { fetchImpl: s.impl });
  check("online, no date: exactly one call, to api.open-meteo.com",
    s.calls.length === 1 && new URL(s.calls[0]).host === "api.open-meteo.com");
  check("current conditions carry temp, code and glyph",
    wx && wx.temp === 70 && wx.feels === 69 && wx.code === 61 && wx.emoji === WMO_GLYPHS[61]);
}
{
  const s = stubFetch();
  const wx = await fetchWeather(39.19, -106.82, "2030-06-15", { fetchImpl: s.impl });
  check("online with a date: one current call and one forecast call, both to Open-Meteo",
    s.calls.length === 2 && s.calls.every((u) => new URL(u).host === "api.open-meteo.com"));
  check("the forecast row carries its code and precip chance",
    wx && wx.forecast && wx.forecast.code === 95 && wx.forecast.precip === 40 && wx.forecast.temp === 80);
}
{
  const s = stubFetch({ dailyStatus: 400 });
  const wx = await fetchWeather(39.19, -106.82, "2031-06-15", { fetchImpl: s.impl });
  check("a date past the horizon keeps current conditions and says how far the forecast reaches",
    wx && wx.forecast === null && wx.forecast_days === FORECAST_DAYS && wx.temp === 70);
}
{
  const wx = await fetchWeather(39.19, -106.82, null, { fetchImpl: async () => { throw new Error("down"); } });
  check("a failed fetch resolves null instead of throwing", wx === null);
}

const ja = catalog("ja");
check("describe(61) in Japanese is the ja catalog's string", describe(61, tFor(ja)) === ja["wx.code.61"]);
check("describe() of an unknown code is empty, so the caller falls back", describe(42, tFor(ja)) === "");

const en = catalog("en");
const enCodes = Object.keys(en).filter((k) => k.startsWith("wx.code.")).map((k) => Number(k.slice(8)));
const jsCodes = Object.keys(WMO_GLYPHS).map(Number);
check("weather.js glyph codes match en.json's wx.code.<n> keys",
  JSON.stringify([...enCodes].sort((a, b) => a - b)) === JSON.stringify(jsCodes.sort((a, b) => a - b)));

const py = readFileSync(path.join(PKG, "weather.py"), "utf8");
const pyGlyphs = {};
for (const m of py.matchAll(/(\d+): \("[^"]+", "([^"]+)"\)/g)) pyGlyphs[Number(m[1])] = m[2];
check("weather.js glyphs match weather.py's _WMO table",
  Object.keys(pyGlyphs).length === jsCodes.length
  && jsCodes.every((c) => pyGlyphs[c] === WMO_GLYPHS[c]));

console.log(`\n${fails ? `${fails} FAILED` : "ALL PASS"}`);
process.exit(fails ? 1 : 0);
