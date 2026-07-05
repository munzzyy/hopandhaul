// Light/dark toggle. Defaults to the OS preference, remembers an explicit override.
import { loadTheme, saveTheme } from "./state.js";

const prefersDark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? true;

function apply(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    btn.setAttribute("aria-pressed", String(theme === "dark"));
    btn.textContent = theme === "dark" ? "\u{1F319}" : "☀️";
    btn.setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
  }
}

export function initTheme() {
  const saved = loadTheme();
  const theme = saved || (prefersDark() ? "dark" : "light");
  apply(theme);

  const btn = document.getElementById("theme-toggle");
  btn?.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    apply(next);
    saveTheme(next);
  });

  // Follow the OS live only when the user hasn't picked an explicit override.
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
    if (loadTheme()) return;
    apply(e.matches ? "dark" : "light");
  });
}
