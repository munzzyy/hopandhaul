// Pre-paint theme boot: sets data-theme from the saved preference (or OS default) before
// styles.css even applies, so there's no flash of the wrong theme on load. Classic script
// (not a module) loaded synchronously in <head> before styles.css — same-origin, so it
// passes script-src 'self' with zero CSP loosening. Mirrors the storage key state.js uses
// (THEME_KEY = "hopandhaul:theme") and the same OS-preference fallback theme.js uses.
// CODES mirrors theme.js's THEMES registry — duplicated as a literal array since this classic
// script can't import theme.js's module. A stale/unknown value (old build, hand-edited
// storage) falls through to the OS default instead of being written to data-theme as-is.
(function () {
  try {
    var CODES = ["dark", "oled", "amber", "contrast", "light", "vintage", "sepia", "coastal"];
    var saved = localStorage.getItem("hopandhaul:theme");
    var theme = (saved && CODES.indexOf(saved) !== -1) ? saved
      : (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", theme);
  } catch (e) {
    // storage/matchMedia blocked (private browsing, older browser) — CSS default (dark) applies
  }
})();
