// Connectivity chip: a small keyboard-accessible listbox popup (same interaction pattern as
// theme.js's picker) over the three modes in connectivity.js. This is a distinct concept from
// the pricing badge next to it - that one says where FARE numbers came from, this one says
// whether the app is even allowed to touch the network - so it's a second, separate control
// rather than a third state folded into the existing badge.
import { getMode, setMode, isOffline, onNetChange } from "./connectivity.js";
import { t } from "./i18n.js";
import { esc } from "./format.js";

let launcher, list, dot, label;
let isOpen = false;

const ROWS = ["auto", "online", "offline"];

function rowHtml(mode, activeMode) {
  const isActive = mode === activeMode;
  return `<li class="theme-item${isActive ? " theme-item--active" : ""}" role="option" `
    + `id="net-opt-${mode}" data-mode="${mode}" `
    + `aria-selected="${isActive}" tabindex="${isActive ? "0" : "-1"}">`
    + `<span class="theme-item-main">`
    + `<span class="theme-item-name">${esc(t(`net.name.${mode}`))}</span>`
    + `<span class="theme-item-desc net-item-desc">${esc(t(`net.desc.${mode}`))}</span>`
    + `</span>`
    + `${isActive ? `<span class="theme-item-check" aria-hidden="true">✓</span>` : ""}`
    + `</li>`;
}

function renderList() {
  const active = getMode();
  list.innerHTML = ROWS.map((m) => rowHtml(m, active)).join("");
}

function refreshChip() {
  const offline = isOffline();
  launcher.classList.toggle("net-chip--offline", offline);
  dot.setAttribute("aria-hidden", "true");
  label.textContent = offline ? t("net.chipOffline") : t("net.chipOnline");
  if (isOpen) renderList();
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

function choose(mode) {
  setMode(mode);
  closeMenu();
}

function onKeydown(e) {
  if (e.key === "Escape") { e.preventDefault(); closeMenu(); return; }
  if (e.key === "Tab") { closeMenu({ refocus: false }); return; }
  if (e.key === "ArrowDown") { e.preventDefault(); moveFocus(1); return; }
  if (e.key === "ArrowUp") { e.preventDefault(); moveFocus(-1); return; }
  if (e.key === "Enter" || e.key === " ") {
    const el = document.activeElement?.closest?.('li[role="option"]');
    if (el) { e.preventDefault(); choose(el.dataset.mode); }
  }
}

/** Re-run after a language switch re-renders static text - applyStatic() only walks
 * data-i18n(-attr) elements, not this dynamically-built chip/listbox. */
export function refreshNetLabel() {
  refreshChip();
}

export function initNetChip() {
  launcher = document.getElementById("net-toggle");
  list = document.getElementById("net-list");
  dot = document.getElementById("net-dot");
  label = document.getElementById("net-label");

  launcher.addEventListener("click", () => { (isOpen ? closeMenu : openMenu)(); });
  launcher.addEventListener("keydown", (e) => {
    if (isOpen) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openMenu();
    }
  });
  list.addEventListener("click", (e) => {
    const li = e.target.closest("li[data-mode]");
    if (li) choose(li.dataset.mode);
  });

  onNetChange(refreshChip);
  refreshChip();
}
