// Language picker modal: launcher button + accessible dialog (filter, keyboard nav, focus
// trap) over the LANGS registry from i18n.js. Mirrors theme.js's "small self-contained
// feature module wired from app.js" shape.
import { LANGS, loadLang, currentLangCode, isRtl, t } from "./i18n.js";
import { saveLangPref } from "./state.js";
import { esc } from "./format.js";

const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

let onChangeCb = null; // set once from initLangPicker(); called after a language is applied
let launcher, modal, panel, filterInput, list, closeBtn;
let lastFocused = null;
let activeIndex = -1;
let visibleCodes = [];

function applyDirAndHtmlLang(code) {
  document.documentElement.setAttribute("lang", code);
  document.documentElement.setAttribute("dir", isRtl(code) ? "rtl" : "ltr");
}

function updateLauncherCode(code) {
  const el = document.getElementById("lang-code");
  if (el) el.textContent = code.split("-")[0].toUpperCase();
}

function matches(lang, query) {
  if (!query) return true;
  const q = query.toLocaleLowerCase();
  return lang.native.toLocaleLowerCase().includes(q) || lang.en.toLocaleLowerCase().includes(q);
}

function renderList(query) {
  const active = currentLangCode();
  const filtered = LANGS.filter((l) => matches(l, query));
  visibleCodes = filtered.map((l) => l.code);
  activeIndex = -1;

  list.innerHTML = filtered.map((l) => {
    const isActive = l.code === active;
    return `<li class="lang-item${isActive ? " lang-item--active" : ""}" role="option" `
      + `id="lang-opt-${esc(l.code)}" data-code="${esc(l.code)}" `
      + `aria-selected="${isActive}" tabindex="-1">`
      + `<span class="lang-item-main">`
      + `<span class="lang-item-native">${esc(l.native)}</span>`
      + `<span class="lang-item-en">${esc(l.en)}</span>`
      + `</span>`
      + `${isActive ? `<span class="lang-item-check" aria-hidden="true">✓</span>` : ""}`
      + `</li>`;
  // A real (but non-interactive) option, not role="presentation" - a listbox announcing zero
  // options with no explanation is worse than one disabled row explaining why it's empty.
  }).join("") || `<li class="lang-item lang-item--empty" role="option" aria-disabled="true" `
    + `id="lang-opt-empty">${esc(t("search.noMatches"))}</li>`;
}

function paintActive() {
  if (!visibleCodes.length) return; // only the disabled "no matches" row exists - never focus it
  [...list.children].forEach((el, i) => {
    el.classList.toggle("lang-item--focused", i === activeIndex);
  });
  const focused = list.children[activeIndex];
  if (focused) {
    focused.scrollIntoView({ block: "nearest" });
    filterInput.setAttribute("aria-activedescendant", focused.id);
  } else {
    // an empty aria-activedescendant is an invalid IDREF - remove rather than set ""
    filterInput.removeAttribute("aria-activedescendant");
  }
}

// Monotonic token so a slow earlier choose() can't resolve after a newer one and stomp its
// result - without this, a slow catalog fetch for a language the user already changed away
// from would land last and leave the UI, <html lang>, and localStorage on the stale choice.
let chooseSeq = 0;

async function choose(code) {
  const mySeq = ++chooseSeq;
  await loadLang(code); // resolves silently to English internally if the catalog 404s - 
                        // dir/lang below still follow the user's *chosen* code, not the fallback
  if (mySeq !== chooseSeq) return; // superseded by a newer selection while this awaited
  saveLangPref(code);
  applyDirAndHtmlLang(code);
  updateLauncherCode(code);
  closeModal();
  if (onChangeCb) onChangeCb(code);
}

function openModal() {
  lastFocused = document.activeElement;
  modal.hidden = false;
  filterInput.value = "";
  renderList("");
  requestAnimationFrame(() => {
    modal.classList.add("lang-overlay--open");
    filterInput.focus();
  });
  document.addEventListener("keydown", onKeydown, true);
}

function closeModal() {
  modal.classList.remove("lang-overlay--open");
  modal.hidden = true;
  document.removeEventListener("keydown", onKeydown, true);
  (lastFocused || launcher)?.focus();
}

function moveActive(delta) {
  const count = visibleCodes.length; // real options only - never the disabled empty-state row
  if (!count) return;
  activeIndex = ((activeIndex + delta) % count + count) % count;
  paintActive();
}

function onKeydown(e) {
  if (e.key === "Escape") {
    e.preventDefault();
    closeModal();
    return;
  }
  if (e.key === "Tab") {
    // simple focus trap: cycle within the panel
    const focusables = [...panel.querySelectorAll(FOCUSABLE)].filter((el) => !el.disabled);
    if (!focusables.length) return;
    const first = focusables[0], last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
    return;
  }
  if (document.activeElement !== filterInput) return;
  if (e.key === "ArrowDown") { e.preventDefault(); moveActive(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); moveActive(-1); }
  else if (e.key === "Enter") {
    e.preventDefault();
    const code = activeIndex >= 0 ? visibleCodes[activeIndex] : visibleCodes[0];
    if (code) choose(code);
  }
}

/** Wire the launcher + modal. `onChange(code)` fires after a new language has been loaded,
 * dir/lang applied, and the modal closed - callers re-run applyStatic() + re-render there. */
export function initLangPicker(onChange) {
  onChangeCb = onChange;
  launcher = document.getElementById("lang-toggle");
  modal = document.getElementById("lang-modal");
  panel = modal.querySelector(".lang-card");
  filterInput = document.getElementById("lang-filter");
  list = document.getElementById("lang-list");
  closeBtn = document.getElementById("lang-close");

  launcher.addEventListener("click", openModal);
  closeBtn.addEventListener("click", closeModal);
  modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
  filterInput.addEventListener("input", () => renderList(filterInput.value.trim()));
  list.addEventListener("click", (e) => {
    const li = e.target.closest("li[data-code]");
    if (li) choose(li.dataset.code);
  });
}

export function updateLauncherAfterInit(code) {
  updateLauncherCode(code);
  applyDirAndHtmlLang(code);
}
