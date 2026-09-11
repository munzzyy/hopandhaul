// Explicit connectivity mode: auto / online / offline, persisted, distinct from the pricing
// badge (that one says where FARE numbers came from; this one says whether the app is even
// allowed to reach the network at all). "auto" tracks navigator.onLine live via the browser's
// own online/offline window events - a forced mode ignores those entirely, so a visitor who
// picks "Offline" on a flaky hotel wifi doesn't have the app silently retry behind their back.
const MODE_KEY = "hopandhaul:netmode";
const MODES = new Set(["auto", "online", "offline"]);
const EVENT = "hopandhaul:netchange";

function readPref() {
  try {
    const v = localStorage.getItem(MODE_KEY);
    return MODES.has(v) ? v : "auto";
  } catch {
    return "auto";
  }
}

function writePref(mode) {
  try {
    localStorage.setItem(MODE_KEY, mode);
  } catch {
    // storage disabled - the choice just won't persist across reloads, not fatal
  }
}

function browserOnline() {
  try {
    return navigator.onLine !== false;
  } catch {
    return true; // no navigator.onLine support - assume online rather than stranding everyone
  }
}

let _mode = readPref();
let _offline = _mode === "offline" || (_mode === "auto" && !browserOnline());

export function getMode() {
  return _mode;
}

/** True when the app should behave as if there is no network at all - api.js/transit.js gate
 * every external fetch on this, not on navigator.onLine directly, so "forced offline" and
 * "auto + actually offline" are one code path. */
export function isOffline() {
  return _offline;
}

function fire() {
  window.dispatchEvent(new CustomEvent(EVENT, { detail: { mode: _mode, offline: _offline } }));
}

/** Explicit mode change from the picker. Always re-fires (even a no-op-for-`_offline` switch,
 * e.g. auto -> online while already online) so the chip's own UI updates to show the new mode. */
export function setMode(mode) {
  if (!MODES.has(mode) || mode === _mode) return;
  _mode = mode;
  writePref(mode);
  _offline = _mode === "offline" || (_mode === "auto" && !browserOnline());
  fire();
}

/** Wire the real browser online/offline events - only meaningful in "auto"; a forced mode
 * intentionally ignores them. Call once at boot. */
export function initConnectivity() {
  const onBrowserEvent = () => {
    if (_mode !== "auto") return;
    const next = !browserOnline();
    if (next === _offline) return;
    _offline = next;
    fire();
  };
  window.addEventListener("online", onBrowserEvent);
  window.addEventListener("offline", onBrowserEvent);
}

/** Subscribe to effective-state changes; returns an unsubscribe function. */
export function onNetChange(fn) {
  const handler = (e) => fn(e.detail);
  window.addEventListener(EVENT, handler);
  return () => window.removeEventListener(EVENT, handler);
}
