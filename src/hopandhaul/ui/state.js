// URL <-> form-state serialization, plus localStorage for the non-shareable prefs (theme).
// Shareable state is everything planTo() needs to reproduce a plan: click point, origin,
// dates, travelers, threshold, vot, max ground hours, round trip, and the place label.

const URL_KEYS = [
  "lat", "lng", "origin", "date", "ret", "travelers", "vot", "threshold", "maxg", "round", "place",
];

/** Read the current plan/search state out of `location.search`. Returns null fields when absent. */
export function readUrlState() {
  const q = new URLSearchParams(location.search);
  const has = (k) => q.has(k) && q.get(k) !== "";
  const num = (k) => (has(k) ? Number(q.get(k)) : null);
  return {
    lat: has("lat") ? Number(q.get("lat")) : null,
    lng: has("lng") ? Number(q.get("lng")) : null,
    origin: has("origin") ? q.get("origin").toUpperCase().slice(0, 4) : null,
    date: has("date") ? q.get("date") : null,
    ret: has("ret") ? q.get("ret") : null,
    travelers: num("travelers"),
    vot: num("vot"),
    threshold: num("threshold"),
    maxg: num("maxg"),
    round: has("round") ? q.get("round") === "1" : null,
    place: has("place") ? q.get("place") : null,
  };
}

/**
 * Push the given state into the URL without adding a history entry or reloading - 
 * this is what makes a plan link shareable (§ shareable trip URLs).
 */
export function writeUrlState(state) {
  const q = new URLSearchParams();
  for (const k of URL_KEYS) {
    const v = state[k];
    if (v === null || v === undefined || v === "") continue;
    q.set(k, typeof v === "boolean" ? (v ? "1" : "0") : String(v));
  }
  const qs = q.toString();
  const url = qs ? `${location.pathname}?${qs}` : location.pathname;
  history.replaceState(null, "", url);
}

/** Absolute, copyable URL for the current state (used by the Copy Link button). */
export function shareUrl(state) {
  const q = new URLSearchParams();
  for (const k of URL_KEYS) {
    const v = state[k];
    if (v === null || v === undefined || v === "") continue;
    q.set(k, typeof v === "boolean" ? (v ? "1" : "0") : String(v));
  }
  return `${location.origin}${location.pathname}?${q.toString()}`;
}

/** Shared localStorage read/write for the small set of non-shareable prefs (theme, language) - 
 * both silently no-op on failure (private browsing, storage disabled) since neither is fatal
 * to the app; the pref just won't persist. */
function readPref(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writePref(key, val) {
  try {
    localStorage.setItem(key, val);
  } catch {
    // storage disabled - pref just won't persist, not fatal
  }
}

const THEME_KEY = "hopandhaul:theme";

export function loadTheme() {
  return readPref(THEME_KEY);
}

export function saveTheme(theme) {
  writePref(THEME_KEY, theme);
}

/** Drop back to "Auto" - no stored choice means prefers-color-scheme decides (theme-boot.js
 * and theme.js both already treat an absent key this way; this just makes clearing it explicit
 * instead of every caller reaching for localStorage.removeItem directly). */
export function clearTheme() {
  try {
    localStorage.removeItem(THEME_KEY);
  } catch {
    // storage disabled - nothing was persisted in the first place
  }
}

const LANG_KEY = "hopandhaul:lang";

export function loadLangPref() {
  return readPref(LANG_KEY);
}

export function saveLangPref(code) {
  writePref(LANG_KEY, code);
}
