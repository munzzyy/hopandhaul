// fx.js - display-currency conversion for the browser build. The engine's math and the
// $threshold the visitor typed stay USD always; this only changes what fmtMoney() PRINTS.
// Mirrors duffel.py's FX_USD table (same values, same as-of date - keep them in sync) as the
// offline fallback, and upgrades to frankfurter.dev's live ECB daily rate the same way
// duffel.py's _live_rates() does, once per session, only when the app is effectively online.
import { isOffline } from "./connectivity.js";

export const FX_AS_OF = "2026-07-04"; // bump alongside duffel.py's FX_AS_OF when that table refreshes

// Approximate USD value of 1 unit of each currency - copied from duffel.py's FX_USD.
export const FX_USD = {
  USD: 1.0, GBP: 1.27, EUR: 1.08, CAD: 0.73, AUD: 0.66, NZD: 0.61,
  CHF: 1.12, SEK: 0.094, NOK: 0.092, DKK: 0.145, PLN: 0.25, CZK: 0.043,
  HUF: 0.0027, RON: 0.22, MXN: 0.055, BRL: 0.18, JPY: 0.0064, CNY: 0.138,
  HKD: 0.128, SGD: 0.74, INR: 0.012, ZAR: 0.054, AED: 0.272, SAR: 0.267,
  TRY: 0.031, THB: 0.028, MYR: 0.21, IDR: 0.000062, PHP: 0.017, KRW: 0.00073,
  ILS: 0.27, CLP: 0.0011, COP: 0.00025, ARS: 0.0011,
  ISK: 0.0072, BGN: 0.55, RSD: 0.0092, MKD: 0.0175, BAM: 0.55, ALL: 0.0107,
  UAH: 0.024, GEL: 0.37, AMD: 0.0025, AZN: 0.59, GIP: 1.27,
  QAR: 0.275, OMR: 2.60, JOD: 1.41, KWD: 3.25, BHD: 2.65, IQD: 0.00076,
  PEN: 0.27, UYU: 0.024, PYG: 0.00013, BOB: 0.145, GTQ: 0.13, CRC: 0.0019,
  NIO: 0.027, HNL: 0.040, DOP: 0.0165, JMD: 0.0064, TTD: 0.148, BBD: 0.50,
  BSD: 1.0, XCD: 0.37, AWG: 0.56, ANG: 0.56, PAB: 1.0, KYD: 1.20,
  EGP: 0.020, MAD: 0.10, TND: 0.32, DZD: 0.0075, KES: 0.0077, NGN: 0.00065,
  GHS: 0.065, TZS: 0.00037, UGX: 0.00027, XOF: 0.0018, XAF: 0.0018,
  RWF: 0.00072, ETB: 0.008, MUR: 0.022, SCR: 0.068, NAD: 0.054, BWP: 0.073,
  ZMW: 0.039, MGA: 0.00021,
  TWD: 0.031, VND: 0.000039, LAK: 0.000046, KHR: 0.00025, MOP: 0.124,
  BND: 0.74, LKR: 0.0033, NPR: 0.0074, BDT: 0.0085, PKR: 0.0036,
  MVR: 0.065, BTN: 0.012, KZT: 0.0019, UZS: 0.000078, KGS: 0.0115,
  MNT: 0.00029, FJD: 0.44, XPF: 0.0090,
};

// A compact, curated list for the currency picker - every code in it must be a key in FX_USD
// (or USD) so it always resolves to a real rate offline, live or not.
export const CURRENCY_OPTIONS = [
  "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "NZD", "CHF", "MXN", "BRL",
  "CNY", "HKD", "SGD", "INR", "ZAR", "AED", "TRY", "THB", "KRW", "PLN",
  "SEK", "NOK", "DKK", "CZK", "ILS",
];

const FRANKFURTER = "https://api.frankfurter.dev/v1/latest?base=USD";

let _live = null; // { rates, date } once a fetch has resolved, else null
let _tried = false;

/** One-time-per-session fetch of live ECB rates, only while effectively online. Never throws;
 * any failure just leaves `_live` null so callers keep using the static table. */
async function ensureLiveRates() {
  if (_tried || isOffline()) return;
  _tried = true;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 4000);
    const res = await fetch(FRANKFURTER, { signal: controller.signal });
    clearTimeout(timer);
    if (!res.ok) return;
    const out = await res.json();
    const rates = out.rates || {};
    const inverted = {};
    for (const [c, r] of Object.entries(rates)) if (r) inverted[c] = 1 / r;
    _live = { rates: inverted, date: out.date || null };
  } catch {
    // offline, blocked, or slow - the static table below still answers every conversion
  }
}

/** Fire-and-forget kickoff, safe to call as soon as the app knows it's online - callers that
 * need the result to already be in before formatting should `await` this once at boot instead. */
export function warmLiveRates() {
  ensureLiveRates().catch(() => {});
}

/** USD -> `currency`, or the USD amount unchanged if the currency isn't priceable at all.
 * Returns { amount, rateSource } where rateSource is "native" | "live" | "static" | "unknown" -
 * same vocabulary duffel.py's to_usd()/from_usd() use, so a UI string can reuse the same idea. */
export function convertFromUsd(amountUsd, currency) {
  const cur = String(currency || "USD").toUpperCase();
  if (cur === "USD") return { amount: amountUsd, rateSource: "native" };
  const liveRate = _live?.rates?.[cur];
  if (liveRate) return { amount: amountUsd / liveRate, rateSource: "live" };
  const staticRate = FX_USD[cur];
  if (staticRate) return { amount: amountUsd / staticRate, rateSource: "static" };
  return { amount: amountUsd, rateSource: "unknown" };
}

export function liveRatesDate() {
  return _live?.date || null;
}

/** Which source WOULD price `currency` right now, without doing the conversion - lets a caller
 * (the fx note under the currency picker) say "live" vs "approximate table" without formatting
 * a throwaway amount just to read its rateSource back out. */
export function rateSourceFor(currency) {
  const cur = String(currency || "USD").toUpperCase();
  if (cur === "USD") return "native";
  if (_live?.rates?.[cur]) return "live";
  if (FX_USD[cur]) return "static";
  return "unknown";
}

export { ensureLiveRates };
