// Search: two ARIA comboboxes (role="combobox" + a listbox popup, not a div soup with no
// semantics) sharing one keyboard/highlight engine - the destination field (debounced, backed
// by fetchGeocode: local airport DB + Photon when online) and the origin field (backed by the
// same local airport DB directly - an origin has to resolve to a real IATA code, so there is no
// "extended" geocoder tier for it the way there is for a free-text destination).
import { esc } from "./format.js";
import { fetchGeocode } from "./api.js";
import { searchAirports } from "./engine/search.js";
import { t } from "./i18n.js";

/**
 * @param {{
 *   inputId: string, listId: string,
 *   run: (q:string) => Promise<{ok:boolean, results?:object[], extendedSkipped?:boolean}>,
 *   onChoose: (r:object) => void,
 *   minLength?: number,
 * }} opts
 * @returns {{disable:(msg:string)=>void}}
 */
function initCombobox({ inputId, listId, run, onChoose, minLength = 3 }) {
  const input = document.getElementById(inputId);
  const list = document.getElementById(listId);
  let items = [];
  let activeIndex = -1;
  let timer = null;
  let requestId = 0;

  function optionId(i) {
    return `${listId}-opt-${i}`;
  }

  function close() {
    list.hidden = true;
    list.innerHTML = "";
    items = [];
    activeIndex = -1;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function paintEmpty(extendedSkipped) {
    const msg = extendedSkipped ? t("search.offlineNoMatches") : t("search.noMatches");
    list.innerHTML = `<li class="aitem aitem--empty" role="option" aria-disabled="true">${esc(msg)}</li>`;
    list.hidden = false;
  }

  function paint(results, extendedSkipped) {
    items = results;
    activeIndex = -1;
    input.setAttribute("aria-expanded", "true");
    if (!results.length) {
      paintEmpty(extendedSkipped);
      return;
    }
    list.innerHTML = results.map((r, i) => (
      `<li class="aitem" id="${optionId(i)}" role="option" data-i="${i}" aria-selected="false">`
      + `<span class="aitem-label">${esc(r.label)}</span>`
      + `<span class="aitem-type">${esc(r.type || "")}${r.country_code ? " · " + esc(r.country_code) : ""}</span>`
      + `</li>`
    )).join("");
    list.hidden = false;
  }

  function paintActive() {
    [...list.children].forEach((el, i) => {
      const on = i === activeIndex;
      el.classList.toggle("aitem--active", on);
      el.setAttribute("aria-selected", String(on));
    });
    if (activeIndex >= 0) {
      input.setAttribute("aria-activedescendant", optionId(activeIndex));
      list.children[activeIndex]?.scrollIntoView({ block: "nearest" });
    } else {
      // an empty aria-activedescendant is an invalid IDREF - remove rather than set ""
      input.removeAttribute("aria-activedescendant");
    }
  }

  async function runSearch(q) {
    const myId = ++requestId;
    try {
      const d = await run(q);
      if (myId !== requestId) return; // a newer query already superseded this one
      if (d.ok) paint(d.results || [], d.extendedSkipped === true);
      else close();
    } catch {
      if (myId === requestId) close();
    }
  }

  function choose(i) {
    const r = items[i];
    if (!r) return;
    close();
    input.value = r.label;
    onChoose(r);
  }

  input.addEventListener("input", () => {
    const q = input.value.trim();
    clearTimeout(timer);
    if (q.length < minLength) { close(); return; }
    timer = setTimeout(() => runSearch(q), 250);
  });

  input.addEventListener("keydown", (e) => {
    if (list.hidden || !items.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIndex = Math.min(items.length - 1, activeIndex + 1);
      paintActive();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex = Math.max(0, activeIndex - 1);
      paintActive();
    } else if (e.key === "Enter") {
      if (activeIndex >= 0 || items.length) {
        e.preventDefault();
        choose(activeIndex >= 0 ? activeIndex : 0);
      }
    } else if (e.key === "Escape") {
      close();
    }
  });

  // Prevent the input from blurring on mousedown-to-select - a <li> isn't natively
  // focusable, so without this the focusout handler below (or the document click-away
  // handler) can race the click handler and close the list before choose() ever runs.
  list.addEventListener("mousedown", (e) => { e.preventDefault(); });

  list.addEventListener("click", (e) => {
    const li = e.target.closest("li[data-i]");
    if (li) choose(Number(li.dataset.i));
  });

  document.addEventListener("click", (e) => {
    if (!input.contains(e.target) && !list.contains(e.target)) close();
  });

  // Keyboard users tabbing away from the search should close the popup too, not just
  // mouse-click-away - relatedTarget is null for some browsers on blur-to-nowhere, hence
  // the optional chaining rather than assuming it's always an Element.
  input.addEventListener("focusout", (e) => {
    if (!e.relatedTarget || (!input.contains(e.relatedTarget) && !list.contains(e.relatedTarget))) close();
  });

  return {
    disable(msg) {
      input.placeholder = msg;
      input.disabled = true;
    },
  };
}

/** Destination search box - debounced, network-capable (see fetchGeocode). */
export function initSearch({ onChoose }) {
  return initCombobox({
    inputId: "place",
    listId: "aclist",
    run: (q) => fetchGeocode(q),
    onChoose,
  });
}

/** Origin airport combobox - local DB only, no debounce needed (it's a synchronous in-memory
 * search), and a shorter minLength since IATA codes are 3 characters. Selecting a row fills the
 * IATA code the same way typing one directly always has - onChoose gets the raw match, which
 * always carries `.iata` for an airport result. */
export function initOriginSearch({ onChoose }) {
  return initCombobox({
    inputId: "origin",
    listId: "origin-aclist",
    minLength: 2,
    run: async (q) => ({ ok: true, results: searchAirports(q, 6) }),
    onChoose,
  });
}
