// Pure formatting/escaping helpers. No DOM access, no state - safe to import anywhere.
import { t } from "./i18n.js";

/** Escape a string for safe insertion into HTML markup (attribute or text position). */
export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/** $1,234 for whole dollars, $1234.56 when there are real cents. */
export function fmtMoney(x) {
  if (x == null || Number.isNaN(x)) return "—";
  const n = Number(x);
  return Math.abs(n - Math.round(n)) < 0.01
    ? "$" + Math.round(n).toLocaleString()
    : "$" + n.toFixed(2);
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
  alt_qualifies: { key: "status.savesRule", tone: "ok" },
  cheaper_below_threshold: { key: "status.underThreshold", tone: "warn" },
  pricier_faster: { key: "status.fasterCostsMore", tone: "warn" },
  worse: { key: "status.worse", tone: "bad" },
};

/** { text, tone } for a trip.py option status - tone maps to a CSS class, never color alone. */
export function statusLabel(status) {
  const entry = STATUS_KEY[status];
  return entry ? { text: t(entry.key), tone: entry.tone } : { text: status, tone: "" };
}
