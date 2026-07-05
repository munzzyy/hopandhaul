// Pure formatting/escaping helpers. No DOM access, no state — safe to import anywhere.

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

/** ~150 kg, ~2.3 t — CO2e is always an ESTIMATE, so this stays rounded/approximate on purpose;
 * switches to tonnes once the number gets big enough that kilograms stop being readable. */
export function fmtCo2(kg) {
  if (kg == null || Number.isNaN(kg)) return "—";
  const n = Number(kg);
  return n >= 1000 ? `≈ ${(n / 1000).toFixed(1)} t CO₂e` : `≈ ${Math.round(n)} kg CO₂e`;
}

/** 3h05, 3h, 0h45 — never "60m" at the boundary. */
export function fmtH(hours) {
  if (hours == null || Number.isNaN(hours)) return "—";
  const totalMin = Math.round(hours * 60);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return m ? `${h}h${String(m).padStart(2, "0")}` : `${h}h`;
}

// Every ground mode trip.py can emit, plus "fly" as a first-class key — no post-hoc special case.
const MODE_EMOJI = {
  fly: "✈️",
  train: "🚆", rail: "🚆",
  bus: "🚌", coach: "🚌", shuttle: "🚐",
  drive: "🚗", car: "🚗", rental: "🚗",
  ferry: "⛴️",
};

/** Emoji glyph for a ground/flight mode. Always paired with a text label in the DOM. */
export function modeEmoji(mode) {
  return MODE_EMOJI[mode] || "🚌";
}

/** Plain-English label for a mode — the text alternative that goes next to the emoji. */
export function modeLabel(mode) {
  const labels = {
    fly: "flight", train: "train", rail: "train", bus: "bus", coach: "bus",
    shuttle: "shuttle", drive: "drive", car: "drive", rental: "rental car",
    ferry: "ferry",
  };
  return labels[mode] || mode || "ground";
}

// Every status trip.py's evaluate() can emit — all 7, not just the 4 the old UI styled.
// Raw implementation vocabulary (e.g. "pricier_faster") never reaches the user.
const STATUS_LABEL = {
  baseline: { text: "direct", tone: "base" },
  dominant: { text: "cheaper & faster", tone: "ok" },
  split_qualifies: { text: "saves ≥ rule", tone: "ok" },
  alt_qualifies: { text: "saves ≥ rule", tone: "ok" },
  cheaper_below_threshold: { text: "under threshold", tone: "warn" },
  pricier_faster: { text: "faster, costs more", tone: "warn" },
  worse: { text: "costlier & slower", tone: "bad" },
};

/** { text, tone } for a trip.py option status — tone maps to a CSS class, never color alone. */
export function statusLabel(status) {
  return STATUS_LABEL[status] || { text: status, tone: "" };
}
