// Light/dark toggle. Defaults to the OS preference, remembers an explicit override.
import { loadTheme, saveTheme } from "./state.js";
import { t } from "./i18n.js";
import { setMapTheme } from "./map.js";

const prefersDark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? true;
const reducedMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

function apply(theme) {
  // theme-boot.js already set data-theme pre-paint on first load — this just keeps every
  // subsequent toggle in sync (icon, label, map tiles/lines, browser chrome color).
  document.documentElement.setAttribute("data-theme", theme);
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    const icon = btn.querySelector(".icon use");
    if (icon) icon.setAttribute("href", theme === "dark" ? "#i-moon" : "#i-sun");
    // Dynamic aria-label only — no aria-pressed. The accessible NAME already swaps
    // ("Switch to light theme" <-> "Switch to dark theme"), so also carrying pressed state
    // would be a contradictory announcement; the WAI pattern for this control shape is a
    // dynamic label alone. (Toggles with a stable name, e.g. the mode-toggle segment,
    // keep aria-pressed.)
    btn.setAttribute("aria-label", theme === "dark" ? t("theme.toLight") : t("theme.toDark"));
  }
  setMapTheme(theme);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    // Read --bg live off the just-applied data-theme rather than keeping a second hardcoded
    // copy of the palette in JS — the palette already lives in styles.css; this was the one
    // place it was duplicated. data-theme is set above, so this reads the new theme's value.
    const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    if (bg) meta.setAttribute("content", bg);
  }
}

// Re-apply the current theme's label/icon after a language change re-renders static text —
// applyStatic() only knows the pre-baked HTML attribute, not the toggled runtime state.
export function refreshThemeLabel() {
  const theme = document.documentElement.getAttribute("data-theme") || (prefersDark() ? "dark" : "light");
  apply(theme);
}

export function initTheme() {
  // theme-boot.js already set data-theme before first paint — just sync the label/icon/map
  // to whatever it landed on, no re-decision needed here.
  refreshThemeLabel();

  const btn = document.getElementById("theme-toggle");
  btn?.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    const swap = () => { apply(next); saveTheme(next); };
    if (document.startViewTransition && !reducedMotion()) {
      document.startViewTransition(swap);
    } else {
      swap();
    }
  });

  // Follow the OS live only when the user hasn't picked an explicit override.
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
    if (loadTheme()) return;
    apply(e.matches ? "dark" : "light");
  });
}
