// Pure formatting/escaping helpers. No DOM access; the one piece of module state is the
// display currency (see setDisplayCurrency) - everything else stays a pure function of its args.
import { t, currentLangCode } from "./i18n.js";
import { convertFromUsd } from "./fx.js";

/** Escape a string for safe insertion into HTML markup (attribute or text position). */
export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// The engine's math and the visitor's typed $threshold are always USD - this only changes what
// fmtMoney() PRINTS. One module-level setting rather than threading a currency argument through
// every call site across results.js/map.js, matching how currentLangCode() works in i18n.js.
let _currency = "USD";

export function setDisplayCurrency(code) {
  _currency = String(code || "USD").toUpperCase();
}

export function currentCurrency() {
  return _currency;
}

/** $1,234 for a whole amount, $1,234.56 when there are real cents - now currency-aware: every
 * price is stored in USD by the engine and converted for display only (see fx.js). */
export function fmtMoney(x) {
  if (x == null || Number.isNaN(x)) return "—";
  const usd = Number(x);
  const { amount } = convertFromUsd(usd, _currency);
  const whole = Math.abs(amount - Math.round(amount)) < 0.005;
  try {
    return new Intl.NumberFormat(currentLangCode(), {
      style: "currency",
      currency: _currency,
      minimumFractionDigits: whole ? 0 : undefined,
      maximumFractionDigits: whole ? 0 : undefined,
    }).format(amount);
  } catch {
    // an unsupported/unknown ISO code (shouldn't happen - the picker only offers real ones)
    return "$" + Math.round(amount).toLocaleString();
  }
}

/** ~150 kg, ~2.3 t - CO2e is always an ESTIMATE, so this stays rounded/approximate on purpose;
 * switches to tonnes once the number gets big enough that kilograms stop being readable. */
export function fmtCo2(kg) {
  if (kg == null || Number.isNaN(kg)) return "—";
  const n = Number(kg);
  return n >= 1000 ? `≈ ${(n / 1000).toFixed(1)} t CO₂e` : `≈ ${Math.round(n)} kg CO₂e`;
}

/** 3h05, 3h, 0h45 - never "60m" at the boundary. */
export function fmtH(hours) {
  if (hours == null || Number.isNaN(hours)) return "—";
  const totalMin = Math.round(hours * 60);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return m ? `${h}h${String(m).padStart(2, "0")}` : `${h}h`;
}

// Every ground mode trip.py can emit, plus "fly" as a first-class key - no post-hoc special case.
const MODE_ICON = {
  fly: "i-plane",
  train: "i-train", rail: "i-train",
  bus: "i-bus", coach: "i-bus", shuttle: "i-van",
  drive: "i-car", car: "i-car", rental: "i-car",
  ferry: "i-ferry",
};

/** `<svg>` markup referencing the sprite symbol for a ground/flight mode - always paired
 * with a text label in the DOM (see modeLabel). aria-hidden since it's decorative. */
export function modeIcon(mode) {
  const id = MODE_ICON[mode] || "i-bus";
  return `<svg class="icon" aria-hidden="true"><use href="#${id}"/></svg>`;
}

/** Localized text label for a mode - the screen-reader-only text alternative for the
 * aria-hidden mode icon (see modeIcon). */
export function modeLabel(mode) {
  const keys = {
    fly: "mode.flight", train: "mode.train", rail: "mode.train", bus: "mode.bus", coach: "mode.bus",
    shuttle: "mode.shuttle", drive: "mode.drive", car: "mode.drive", rental: "mode.rentalCar",
    ferry: "mode.ferry",
  };
  const key = keys[mode];
  return key ? t(key) : (mode || t("mode.ground"));
}

// Every status trip.py's evaluate() can emit - all 7, not just the 4 the old UI styled.
// Raw implementation vocabulary (e.g. "pricier_faster") never reaches the user.
const STATUS_KEY = {
  baseline: { key: "status.direct", tone: "base" },
  dominant: { key: "status.cheaperFaster", tone: "ok" },
  split_qualifies: { key: "status.savesRule", tone: "ok" },
  // Elected because it clears the ≥$threshold rule outright (split_qualifies) vs. because it's
  // worth it at the visitor's own value of time (vot_qualifies) vs. a ground-only option that
  // cleared the threshold on its own with no flight leg at all (alt_qualifies) - three different
  // reasons an option won, so three different honest labels rather than one shared "saves" tag.
  vot_qualifies: { key: "status.votQualifies", tone: "ok" },
  alt_qualifies: { key: "status.altQualifies", tone: "ok" },
  cheaper_below_threshold: { key: "status.underThreshold", tone: "warn" },
  pricier_faster: { key: "status.fasterCostsMore", tone: "warn" },
  worse: { key: "status.worse", tone: "bad" },
};

/** { text, tone } for a trip.py option status - tone maps to a CSS class, never color alone. */
export function statusLabel(status) {
  const entry = STATUS_KEY[status];
  return entry ? { text: t(entry.key), tone: entry.tone } : { text: status, tone: "" };
}
