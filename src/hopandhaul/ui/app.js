// Entry point: wires state <-> URL, the form, search, map, and results together.
// `rec` (the recommended option) is computed once here and passed to both draw() and the
// results render — no more independent recomputation in two places.
import { fetchConfig, fetchNearest, fetchPlan } from "./api.js";
import { readUrlState, writeUrlState, shareUrl } from "./state.js";
import { initMap, markOrigin, draw, clearMap } from "./map.js";
import { initSearch } from "./search.js";
import { renderPlan, renderError, renderEmpty, renderLoading } from "./results.js";
import { initTheme } from "./theme.js";

const $ = (sel) => document.querySelector(sel);
const spinner = $("#spinner");
const liveRegion = $("#sr-status");
let search; // assigned in main(); referenced by applyConfig() after search is wired up

// Cache the field references once instead of re-querying the DOM on every planTo() call.
const fields = {
  place: $("#place"),
  origin: $("#origin"),
  travelers: $("#travelers"),
  date: $("#date"),
  ret: $("#ret"),
  vot: $("#vot"),
  threshold: $("#threshold"),
  maxg: $("#maxg"),
  round: $("#round"),
};

let originIata = "JFK";
let clickMode = "dest"; // "dest" | "origin"
let lastPlaceLabel = null;
let lastClick = null; // {lat, lng} of the most recent destination point, for share/reload

function announce(msg) {
  liveRegion.textContent = msg;
}

function currentShareState() {
  return {
    lat: lastClick?.lat ?? null,
    lng: lastClick?.lng ?? null,
    origin: originIata,
    date: fields.date.value || null,
    ret: fields.ret.value || null,
    travelers: Number(fields.travelers.value) || 1,
    vot: fields.vot.value ? Number(fields.vot.value) : null,
    threshold: fields.threshold.value ? Number(fields.threshold.value) : null,
    maxg: fields.maxg.value ? Number(fields.maxg.value) : null,
    round: fields.round.checked,
    place: lastPlaceLabel,
  };
}

function syncUrl() {
  if (!lastClick) return;
  writeUrlState(currentShareState());
}

async function planTo(lat, lng) {
  lastClick = { lat, lng };
  spinner.hidden = false;
  announce("Calculating your trip…");
  renderLoading();

  const params = {
    lat, lng, origin: originIata,
    threshold: fields.threshold.value || 200,
    maxGroundH: fields.maxg.value || 6,
    round: (fields.round.checked || fields.ret.value) ? "1" : "0",
  };
  if (fields.vot.value) params.vot = fields.vot.value;
  if (fields.date.value) params.date = fields.date.value;
  if (fields.ret.value && fields.date.value) params.ret = fields.ret.value;
  const travelers = parseInt(fields.travelers.value || "1", 10);
  if (travelers > 1) params.travelers = travelers;

  let data;
  try {
    data = await fetchPlan(params);
  } catch (err) {
    if (err?.name === "AbortError") return; // superseded by a newer click — not an error
    data = { ok: false, error: "Network error reaching the local server." };
  }

  spinner.hidden = true;
  if (!data.ok) {
    clearMap();
    renderError(data.error);
    announce(`Couldn't plan that route: ${data.error || "unknown error"}`);
    return;
  }

  const R = data.result;
  const rec = R.options.find((o) => o.name === R.recommended);
  draw(data, rec);
  renderPlan(data, lastPlaceLabel);
  syncUrl();

  const isDirect = rec.name === R.options.find((o) => o.status === "baseline").name;
  announce(isDirect
    ? "Recommendation ready: fly direct is your best option."
    : `Recommendation ready: ${rec.name} saves money vs flying direct.`);

  $("#copy-link")?.addEventListener("click", onCopyLink);
}

async function onCopyLink() {
  const btn = $("#copy-link");
  const url = shareUrl(currentShareState());
  try {
    await navigator.clipboard.writeText(url);
    if (btn) {
      const original = btn.innerHTML;
      btn.innerHTML = `<span aria-hidden="true">✅</span> Copied`;
      announce("Trip link copied to clipboard.");
      setTimeout(() => { btn.innerHTML = original; }, 1800);
    }
  } catch {
    announce("Couldn't copy the link automatically — copy it from the address bar instead.");
  }
}

function wireModeToggle() {
  const group = $("#mode-toggle");
  group.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    clickMode = btn.dataset.m;
    [...group.children].forEach((b) => {
      const on = b === btn;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    });
  });
}

function wireForm() {
  fields.origin.addEventListener("change", (e) => {
    originIata = (e.target.value || "JFK").toUpperCase().trim().slice(0, 4);
    e.target.value = originIata;
    if (lastClick) planTo(lastClick.lat, lastClick.lng);
  });
  // A return date implies round trip — flip the checkbox before the shared re-plan below runs.
  fields.ret.addEventListener("change", (e) => {
    if (e.target.value) fields.round.checked = true;
  });
  // Any of these changing should re-plan the existing point, so shared links stay live
  // as the visitor tweaks assumptions, and so URL state always reflects the form.
  for (const key of ["travelers", "date", "ret", "vot", "threshold", "maxg", "round"]) {
    fields[key].addEventListener("change", () => { if (lastClick) planTo(lastClick.lat, lastClick.lng); });
  }
}

function wireMap(map) {
  map.on("click", async (e) => {
    if (clickMode === "origin") {
      try {
        const a = await fetchNearest(e.latlng.lat, e.latlng.lng);
        if (a.ok) {
          originIata = a.airport.iata;
          fields.origin.value = originIata;
          markOrigin(a.airport);
          if (lastClick) planTo(lastClick.lat, lastClick.lng);
        }
      } catch {
        announce("Couldn't resolve the nearest airport to that click.");
      }
      return;
    }
    lastPlaceLabel = null;
    fields.place.value = "";
    planTo(e.latlng.lat, e.latlng.lng);
  });
}

async function applyConfig() {
  try {
    const c = await fetchConfig();
    if (!c.ok) return;
    if (!readUrlState().origin) {
      originIata = c.default_origin || "JFK";
      fields.origin.value = originIata;
    }
    if (!readUrlState().threshold) {
      fields.threshold.value = c.default_threshold ?? 200;
    }
    const badge = $("#srcbadge"), note = $("#srcnote");
    if (c.flights_provider) {
      badge.textContent = `live (${c.flights_provider})`;
      badge.className = "badge badge--live";
      note.textContent = " — add a date for live fares";
    } else {
      badge.textContent = "estimate";
      badge.className = "badge badge--est";
      note.textContent = " — distance-based estimates, offline-friendly";
    }
    if (!c.has_geocode) search.disable("Search unavailable — click the map instead");
  } catch {
    // /api/config is best-effort UI polish; the app still works fully offline without it
  }
}

function restoreFromUrl() {
  const s = readUrlState();
  if (s.origin) { originIata = s.origin; fields.origin.value = originIata; }
  if (s.date) fields.date.value = s.date;
  if (s.ret) { fields.ret.value = s.ret; fields.round.checked = true; }
  if (s.travelers) fields.travelers.value = String(s.travelers);
  if (s.vot != null) fields.vot.value = String(s.vot);
  if (s.threshold != null) fields.threshold.value = String(s.threshold);
  if (s.maxg != null) fields.maxg.value = String(s.maxg);
  if (s.round != null) fields.round.checked = s.round;
  if (s.place) { lastPlaceLabel = s.place; fields.place.value = s.place; }
  if (s.lat != null && s.lng != null) {
    return { lat: s.lat, lng: s.lng };
  }
  return null;
}

function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  // Only register over a secure context; http.server on 127.0.0.1 counts as one.
  navigator.serviceWorker.register("./sw.js").catch(() => {
    // offline shell is a nice-to-have, never block the app on it
  });
}

function main() {
  initTheme();
  const map = initMap();
  wireModeToggle();
  wireForm();
  wireMap(map);
  search = initSearch({
    onChoose(r) {
      lastPlaceLabel = r.label;
      map.setView([r.lat, r.lng], 7);
      planTo(r.lat, r.lng);
    },
  });

  const pending = restoreFromUrl();
  applyConfig();
  registerServiceWorker();

  if (pending) {
    map.setView([pending.lat, pending.lng], 7);
    planTo(pending.lat, pending.lng);
  } else {
    renderEmpty();
  }
}

document.addEventListener("DOMContentLoaded", main);
