// Destination search: debounced Geoapify autocomplete wired up as a real ARIA combobox
// (role="combobox" + a listbox popup) instead of a div soup with no semantics.
import { esc } from "./format.js";
import { fetchGeocode } from "./api.js";

/**
 * @param {{onChoose:(r:{lat:number,lng:number,label:string})=>void}} handlers
 * @returns {{disable:(msg:string)=>void}}
 */
export function initSearch({ onChoose }) {
  const input = document.getElementById("place");
  const list = document.getElementById("aclist");
  let items = [];
  let activeIndex = -1;
  let timer = null;
  let requestId = 0;

  function optionId(i) {
    return `ac-opt-${i}`;
  }

  function close() {
    list.hidden = true;
    list.innerHTML = "";
    items = [];
    activeIndex = -1;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function paint(results) {
    items = results;
    activeIndex = -1;
    input.setAttribute("aria-expanded", "true");
    if (!results.length) {
      list.innerHTML = `<li class="aitem aitem--empty" role="presentation">No matches</li>`;
      list.hidden = false;
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
    input.setAttribute("aria-activedescendant", activeIndex >= 0 ? optionId(activeIndex) : "");
  }

  async function runSearch(q) {
    const myId = ++requestId;
    try {
      const d = await fetchGeocode(q);
      if (myId !== requestId) return; // a newer query already superseded this one
      if (d.ok) paint(d.results || []);
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
    if (q.length < 3) { close(); return; }
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

  list.addEventListener("click", (e) => {
    const li = e.target.closest("li[data-i]");
    if (li) choose(Number(li.dataset.i));
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search")) close();
  });

  return {
    disable(msg) {
      input.placeholder = msg;
      input.disabled = true;
    },
  };
}
