// Theme picker: a small keyboard-accessible listbox popup over a fixed THEMES registry,
// extending the original light/dark toggle rather than replacing its mechanism — still one
// `data-theme` attribute on <html>, still the same localStorage key (via state.js), still the
// same prefers-color-scheme fallback when nothing is stored. "Auto" isn't a real theme: it's
// what you get when THEME_KEY is simply absent, matching theme-boot.js's own pre-paint logic.
import { loadTheme, saveTheme, clearTheme } from "./state.js";
import { t } from "./i18n.js";
import { setMapTheme } from "./map.js";
import { esc } from "./format.js";

// Every theme rides one of map.js's two existing tile variants (dark_all / voyager) — no new
// tile style, no new external request. `base` picks which one via setMapTheme(base).
export const THEMES = [
  { code: "dark", base: "dark" },
  { code: "oled", base: "dark" },
  { code: "amber", base: "dark" },
  { code: "contrast", base: "dark" },
  { code: "light", base: "light" },
  { code: "vintage", base: "light" },
  { code: "sepia", base: "light" },
  { code: "coastal", base: "light" },
];
const THEME_CODES = new Set(THEMES.map((th) => th.code));

const prefersDark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? true;
const reducedMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
const osTheme = () => (prefersDark() ? "dark" : "light");

function baseOf(code) {
  return THEMES.find((th) => th.code === code)?.base || "dark";
}

/** The saved choice if it's still a real theme code, else null (meaning: Auto/follow-OS). A
 * stale/unknown value left over from an older build falls through to Auto rather than sticking
 * on a theme that no longer exists. */
function savedOrNull() {
  const saved = loadTheme();
  return saved && THEME_CODES.has(saved) ? saved : null;
}

/** The theme actually in effect right now. */
function effectiveTheme() {
  return savedOrNull() || osTheme();
}

let launcher, list;
let isOpen = false;

function apply(code) {
  // theme-boot.js already set data-theme pre-paint on first load — this keeps every
  // subsequent change (picker choice, OS-preference change while on Auto) in sync: the map
  // tiles/route colors, and the browser-chrome theme-color meta.
  document.documentElement.setAttribute("data-theme", code);
  setMapTheme(baseOf(code));
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    // Read --bg live off the just-applied data-theme rather than keeping a second copy of
    // 8 themes' colors in JS — the palette lives in styles.css; this is the one place a
    // theme's actual bg value is needed outside CSS.
    const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    if (bg) meta.setAttribute("content", bg);
  }
}

function rowHtml(code, nameKey, descKey, activeCode) {
  const isActive = code === activeCode;
  return `<li class="theme-item${isActive ? " theme-item--active" : ""}" role="option" `
    + `id="theme-opt-${esc(code)}" data-code="${esc(code)}" `
    + `aria-selected="${isActive}" tabindex="${isActive ? "0" : "-1"}">`
    + `<span class="theme-swatch theme-swatch--${esc(code)}" aria-hidden="true"></span>`
    + `<span class="theme-item-main">`
    + `<span class="theme-item-name">${esc(t(nameKey))}</span>`
    + `<span class="theme-item-desc">${esc(t(descKey))}</span>`
    + `</span>`
    + `${isActive ? `<span class="theme-item-check" aria-hidden="true">✓</span>` : ""}`
    + `</li>`;
}

function renderList() {
  const active = savedOrNull(); // null while on Auto — no real theme row should show as selected
  const rows = [rowHtml("auto", "theme.name.auto", "theme.desc.auto", active)];
  for (const th of THEMES) {
    rows.push(rowHtml(th.code, `theme.name.${th.code}`, `theme.desc.${th.code}`, active));
  }
  list.innerHTML = rows.join("");
}

function optionEls() {
  return [...list.querySelectorAll('li[role="option"]')];
}

function onDocClick(e) {
  if (list.contains(e.target) || launcher.contains(e.target)) return;
  closeMenu({ refocus: false });
}

function openMenu() {
  renderList();
  list.hidden = false;
  isOpen = true;
  launcher.setAttribute("aria-expanded", "true");
  const opts = optionEls();
  const current = opts.find((el) => el.getAttribute("aria-selected") === "true") || opts[0];
  current?.focus();
  document.addEventListener("keydown", onKeydown, true);
  document.addEventListener("click", onDocClick, true);
}

function closeMenu({ refocus = true } = {}) {
  if (!isOpen) return;
  isOpen = false;
  list.hidden = true;
  launcher.setAttribute("aria-expanded", "false");
  document.removeEventListener("keydown", onKeydown, true);
  document.removeEventListener("click", onDocClick, true);
  if (refocus) launcher.focus();
}

function moveFocus(delta) {
  const opts = optionEls();
  if (!opts.length) return;
  const i = opts.indexOf(document.activeElement);
  const next = opts[(((i < 0 ? 0 : i) + delta) % opts.length + opts.length) % opts.length];
  opts.forEach((el) => { el.tabIndex = -1; });
  next.tabIndex = 0;
  next.focus();
}

function focusEdge(first) {
  const opts = optionEls();
  if (!opts.length) return;
  opts.forEach((el) => { el.tabIndex = -1; });
  const el = first ? opts[0] : opts[opts.length - 1];
  el.tabIndex = 0;
  el.focus();
}

/** Apply a picked code (or clear back to Auto), swapping under a view-transition when the
 * browser supports one and the visitor hasn't asked for reduced motion — same polish the old
 * binary toggle had, now shared by every theme-to-theme change, not just light<->dark. */
function choose(code) {
  const run = () => {
    if (code === "auto") {
      clearTheme();
      apply(osTheme());
    } else {
      saveTheme(code);
      apply(code);
    }
  };
  if (document.startViewTransition && !reducedMotion()) {
    document.startViewTransition(run);
  } else {
    run();
  }
  closeMenu();
}

function onKeydown(e) {
  if (e.key === "Escape") { e.preventDefault(); closeMenu(); return; }
  if (e.key === "Tab") { closeMenu({ refocus: false }); return; } // let focus continue past the widget
  if (e.key === "ArrowDown") { e.preventDefault(); moveFocus(1); return; }
  if (e.key === "ArrowUp") { e.preventDefault(); moveFocus(-1); return; }
  if (e.key === "Home") { e.preventDefault(); focusEdge(true); return; }
  if (e.key === "End") { e.preventDefault(); focusEdge(false); return; }
  if (e.key === "Enter" || e.key === " ") {
    const el = document.activeElement?.closest?.('li[role="option"]');
    if (el) { e.preventDefault(); choose(el.dataset.code); }
  }
}

// Re-render the launcher/menu strings after a language change re-renders static text —
// applyStatic() only walks data-i18n(-attr) elements, not this dynamically-built listbox.
export function refreshThemeLabel() {
  if (isOpen) renderList();
}

export function initTheme() {
  launcher = document.getElementById("theme-toggle");
  list = document.getElementById("theme-list");

  launcher.addEventListener("click", () => { (isOpen ? closeMenu : openMenu)(); });
  launcher.addEventListener("keydown", (e) => {
    if (isOpen) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openMenu();
    }
  });
  list.addEventListener("click", (e) => {
    const li = e.target.closest("li[data-code]");
    if (li) choose(li.dataset.code);
  });

  // theme-boot.js already set data-theme before first paint — just sync the map/theme-color
  // meta to whatever it landed on, no re-decision needed here.
  apply(effectiveTheme());

  // Follow the OS live only when on Auto (nothing explicitly saved).
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
    if (savedOrNull()) return;
    apply(e.matches ? "dark" : "light");
  });
}
