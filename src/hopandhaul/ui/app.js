// Entry point: wires state <-> URL, the form, search, map, results, theme, and language
// together. `rec` (the recommended option) is computed once here and passed to both draw()
// and the results render - no more independent recomputation in two places.
import { fetchConfig, fetchDates, fetchNearest, fetchPlan } from "./api.js";
import { readUrlState, writeUrlState, shareUrl, loadLangPref, saveLangPref } from "./state.js";
import { initMap, markOrigin, draw, clearMap, redrawLastPlan, renderGeoLabels } from "./map.js";
import { initSearch } from "./search.js";
import { renderPlan, renderError, renderEmpty, renderLoading, toggleSheet,
         renderDateStrip, renderDateStripLoading, renderDateStripError, clearDateStrip } from "./results.js";
import { initTheme, refreshThemeLabel } from "./theme.js";
import { loadLang, detectLang, t } from "./i18n.js";
import { initLangPicker, updateLauncherAfterInit } from "./lang.js";
import { esc } from "./format.js";

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
let lastConfig = null; // last successful /api/config payload, replayed by applyConfigBadge() on language change
let lastPlanData = null; // last successful /api/plan payload - re-rendered on language change with no refetch
let lastDatesData = null; // last successful /api/dates sweep - same deal, re-rendered never refetched
let datesToken = 0; // bumped by every planTo(); an older sweep resolving late checks it and bails
let planInFlight = false; // true from the moment a plan request starts until it settles
let searchDisabled = false; // cached has_geocode result, re-applied after a language switch re-renders the input
let lastErrorMsg = null; // message currently shown in the error panel, if any
let lastErrorWasNetwork = false; // true when lastErrorMsg is the client-side t("error.network")
                                  // string (as opposed to a server-supplied string) - only that
                                  // case needs its body re-translated on a language switch

function announce(msg) {
  liveRegion.textContent = msg;
}

// -------------------------------------------------------------------- static-string pass
/**
 * Apply catalog strings to every data-i18n / data-i18n-attr element in the document.
 * data-i18n sets textContent (the element's only job is holding that string - safe, since
 * these are always leaf `<span>`/`<title>`/etc. nodes with no child markup to clobber).
 * data-i18n-attr is a comma-separated `attr:key` list, e.g. "aria-label:lang.buttonAria" or
 * "content:meta.description". Re-run after every language switch to refresh in place.
 * t(key) === key means no catalog (not even English) has that key - a catalog-load failure,
 * not a real translation - so we skip the write and let the baked-in English HTML stand.
 */
export function applyStatic(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    const val = t(key);
    if (val !== key) el.textContent = val;
  });
  root.querySelectorAll("[data-i18n-attr]").forEach((el) => {
    for (const pair of el.getAttribute("data-i18n-attr").split(",")) {
      const [attr, key] = pair.split(":").map((s) => s.trim());
      if (!attr || !key) continue;
      const val = t(key);
      if (val !== key) el.setAttribute(attr, val);
    }
  });
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

/** The form, as the query params both /api/plan and /api/dates take. Shared so the date sweep
 * can never be run against different assumptions than the plan it sits under. */
function tripParams(lat, lng) {
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
  return params;
}

async function planTo(lat, lng) {
  lastClick = { lat, lng };
  lastPlanData = null; // clear stale plan immediately so a mid-flight language switch
                        // re-renders the loading state, not the previous place's plan
  lastDatesData = null;
  // Claim the strip for THIS plan before anything awaits. A sweep started by an earlier click
  // can still be in the air, and it must not paint a window built around the old date.
  const myDatesToken = ++datesToken;
  planInFlight = true;
  spinner.hidden = false;
  announce(t("announce.calculating"));
  renderLoading();

  const params = tripParams(lat, lng);

  let data;
  let isNetworkError = false;
  try {
    data = await fetchPlan(params);
  } catch (err) {
    if (err?.name === "AbortError") return; // superseded by a newer click - not an error
    isNetworkError = true;
    data = { ok: false, error: t("error.network") };
  }

  planInFlight = false;
  spinner.hidden = true;
  if (!data.ok) {
    lastPlanData = null;
    lastErrorMsg = data.error;
    lastErrorWasNetwork = isNetworkError;
    clearMap();
    renderError(data.error, true);
    announce(t("announce.cantPlan", { error: data.error || t("error.unknown") }));
    return;
  }

  lastErrorMsg = null;
  lastErrorWasNetwork = false;
  lastPlanData = data;
  const R = data.result;
  const rec = R.options.find((o) => o.name === R.recommended);
  draw(data, rec);
  renderPlan(data, lastPlaceLabel, true);
  syncUrl();

  const isDirect = rec.name === R.options.find((o) => o.status === "baseline").name;
  announce(isDirect ? t("announce.readyDirect") : t("announce.readySplit", { name: rec.name }));

  $("#copy-link")?.addEventListener("click", onCopyLink);
  $("#sheet-toggle")?.addEventListener("click", toggleSheet);
  // Delegated, and attached to the mount rather than the chips: renderDateStrip() replaces the
  // strip's whole innerHTML when the sweep lands, so a per-chip listener would be dead by then.
  $("#date-strip")?.addEventListener("click", onDateChipClick);

  // The sweep starts only once the plan is on screen. It prices the whole window, so it's the
  // slower call by roughly the width of that window, and the plan is the answer the visitor
  // actually asked for. A sweep with no anchor date is meaningless - same reason /api/dates
  // makes `date` required where /api/plan leaves it optional.
  if (fields.date.value) loadDateStrip(myDatesToken, lat, lng);
  else clearDateStrip();
}

async function loadDateStrip(token, lat, lng) {
  if (token !== datesToken) return;
  renderDateStripLoading();

  let data;
  try {
    data = await fetchDates(tripParams(lat, lng));
  } catch (err) {
    if (err?.name === "AbortError") return; // superseded by a newer click - not an error
    data = null;
  }
  if (token !== datesToken) return;

  if (!data?.ok) {
    // A dead sweep is not a dead plan: the recommendation above it still stands, so this never
    // touches renderError() (which would blow the whole panel away over a side feature).
    renderDateStripError();
    announce(t("dates.failed"));
    return;
  }
  lastDatesData = data;
  const out = renderDateStrip(data, fields.date.value);
  if (out?.summary) announce(out.summary);
}

const DAY_MS = 86400000;

/** Whole-day arithmetic on a calendar date, in UTC milliseconds. `new Date(y, m - 1, d + n)`
 * is LOCAL time and slides an hour across a DST boundary, which in a zone whose transition
 * lands near midnight moves the answer to the wrong calendar day. Same rule engine/dates.js
 * follows - and unlike that file, nothing in the parity gate would catch it here. */
function isoShift(iso, days) {
  const [y, m, d] = String(iso).split("-").map(Number);
  const ms = Date.UTC(y, m - 1, d);
  if (!Number.isFinite(ms) || !Number.isFinite(days)) return null;
  const out = new Date(ms + days * DAY_MS);
  return [
    out.getUTCFullYear(),
    String(out.getUTCMonth() + 1).padStart(2, "0"),
    String(out.getUTCDate()).padStart(2, "0"),
  ].join("-");
}

function daysBetween(fromIso, toIso) {
  const a = String(fromIso).split("-").map(Number);
  const b = String(toIso).split("-").map(Number);
  return Math.round((Date.UTC(b[0], b[1] - 1, b[2]) - Date.UTC(a[0], a[1] - 1, a[2])) / DAY_MS);
}

/** Pick a date off the strip: move #date (and the return with it), then re-plan.
 *
 * The re-plan has to be an explicit call. Assigning to fields.date.value does NOT fire the
 * `change` listener wireForm() attaches, and dispatching a synthetic one instead would plan
 * twice - once from here, once from that listener. */
function onDateChipClick(e) {
  const btn = e.target.closest("button[data-date]");
  if (!btn || !lastClick) return;
  const picked = btn.dataset.date;
  if (!picked || picked === fields.date.value) return;

  // A return date moves with its departure so the trip keeps its LENGTH - the same rule the
  // sweep priced each candidate under, so the chip's price is the one this re-plan produces.
  if (fields.date.value && fields.ret.value) {
    const shifted = isoShift(fields.ret.value, daysBetween(fields.date.value, picked));
    if (shifted) fields.ret.value = shifted;
  }
  fields.date.value = picked;
  // lockDatesToFuture()'s listener only fires on a user edit, so keep the return floor in step.
  fields.ret.min = picked;
  planTo(lastClick.lat, lastClick.lng);
}

/** Re-render whatever is currently on screen using the newly-loaded catalog, without any
 * network refetch - called after a language switch. */
function rerenderCurrent() {
  renderGeoLabels(); // re-translate the map's continent/country labels into the new language
  if (planInFlight) {
    renderLoading();
  } else if (lastPlanData) {
    renderPlan(lastPlanData, lastPlaceLabel);
    // Re-render only, never refetch: the day names and the basis tags are the whole reason the
    // strip needs this pass, and the numbers behind them didn't move. No announce() either -
    // a language switch isn't a new result.
    if (lastDatesData) renderDateStrip(lastDatesData, fields.date.value);
    $("#copy-link")?.addEventListener("click", onCopyLink);
    $("#sheet-toggle")?.addEventListener("click", toggleSheet);
    $("#date-strip")?.addEventListener("click", onDateChipClick);
    redrawLastPlan(); // map popups bake t() strings at draw time - re-translate them too
  } else if (lastErrorMsg != null) {
    // re-render the panel so the title/chrome re-translate; if the body was the client-side
    // network message it needs re-translating too, otherwise it's a server string as sent
    renderError(lastErrorWasNetwork ? t("error.network") : lastErrorMsg);
  } else if (lastClick) {
    // a plan attempt is in flight or previously errored with nothing cached - leave state as is
  } else {
    renderEmpty();
  }
}

let copyTimer = null; // module-level so a second click always clears the previous swap-back

/** Flash the copy-link button to an ok/err state for 1.8s, then restore its normal label.
 * Rebuilds the restored label via t("results.copyLink") rather than replaying a snapshot of
 * the button's original innerHTML - a snapshot goes stale if the language changes during the
 * 1.8s window, and would restore the button to whatever language was active when it was clicked. */
function flashCopyButton(btn, { ok, label }) {
  clearTimeout(copyTimer);
  btn.innerHTML = label;
  btn.classList.toggle("btn--ok", ok);
  btn.classList.toggle("btn--err", !ok);
  copyTimer = setTimeout(() => {
    btn.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#i-link"/></svg> ${esc(t("results.copyLink"))}`;
    btn.classList.remove("btn--ok", "btn--err");
  }, 1800);
}

async function onCopyLink() {
  const btn = $("#copy-link");
  const url = shareUrl(currentShareState());
  try {
    await navigator.clipboard.writeText(url);
    if (btn) {
      flashCopyButton(btn, {
        ok: true,
        label: `<svg class="icon" aria-hidden="true"><use href="#i-check"/></svg> ${esc(t("copy.copied"))}`,
      });
    }
    announce(t("announce.copied"));
  } catch {
    // sighted users previously never saw this failure at all - the button just silently
    // didn't change. Now it flashes a visible error state, matching the success path.
    if (btn) {
      // No dedicated short button-label key exists for this failure - reuses the existing
      // announce.copyFail sentence rather than inventing a new string; verbose for a button,
      // but every word is already translated in all 46 catalogs.
      flashCopyButton(btn, { ok: false, label: esc(t("announce.copyFail")) });
    }
    announce(t("announce.copyFail"));
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

// The fare engine has no booking-lead-time curve for a date that has already been and gone, so
// it prices a past date exactly like no date at all (geo.fareDateMultiplier returns a neutral
// 1.0). The plan notes now say so out loud, but the honest place to stop it is the picker:
// give both date fields a floor of today so the calendar simply won't offer yesterday.
function lockDatesToFuture() {
  const today = new Date();
  const iso = [
    today.getFullYear(),
    String(today.getMonth() + 1).padStart(2, "0"),
    String(today.getDate()).padStart(2, "0"),
  ].join("-");
  fields.date.min = iso;
  fields.ret.min = iso;
  // A return can't precede the departure either. Keep that floor moving with the depart field.
  fields.date.addEventListener("change", () => {
    fields.ret.min = fields.date.value || iso;
    if (fields.ret.value && fields.ret.value < fields.ret.min) fields.ret.value = "";
  });
}

function wireForm() {
  lockDatesToFuture();
  fields.origin.addEventListener("change", (e) => {
    originIata = (e.target.value || "JFK").toUpperCase().trim().slice(0, 4);
    e.target.value = originIata;
    if (lastClick) planTo(lastClick.lat, lastClick.lng);
  });
  // A return date implies round trip - flip the checkbox before the shared re-plan below runs.
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
        announce(t("announce.noNearest"));
      }
      return;
    }
    lastPlaceLabel = null;
    fields.place.value = "";
    planTo(e.latlng.lat, e.latlng.lng);
  });
}

function applyConfigBadge(c) {
  const badge = $("#srcbadge"), note = $("#srcnote");
  if (c.flights_provider) {
    badge.textContent = t("badge.live", { provider: c.flights_provider });
    badge.className = "badge badge--live";
    note.textContent = t("badge.liveNote");
  } else {
    badge.textContent = t("badge.estimate");
    badge.className = "badge badge--est";
    note.textContent = t("badge.estNote");
  }
}

async function applyConfig() {
  try {
    const c = await fetchConfig();
    if (!c.ok) return;
    lastConfig = c;
    if (!readUrlState().origin) {
      originIata = c.default_origin || "JFK";
      fields.origin.value = originIata;
    }
    if (!readUrlState().threshold) {
      fields.threshold.value = c.default_threshold ?? 200;
    }
    applyConfigBadge(c);
    searchDisabled = !c.has_geocode;
    if (searchDisabled) search.disable(t("search.unavailable"));
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

/** Resolve which language to boot with: explicit saved choice, else autodetect from the
 * browser, else English. Always resolves - loadLang() itself never throws or rejects. */
async function bootLang() {
  const saved = loadLangPref();
  const code = saved || detectLang();
  const resolved = await loadLang(code);
  // Persist the *chosen* code (even if its catalog hasn't landed yet and we're rendering
  // English) so the user's choice sticks; only first-visit autodetect writes eagerly too,
  // so a repeat visit doesn't silently re-detect a different browser-language preference.
  if (!saved) saveLangPref(code);
  updateLauncherAfterInit(code); // sets <html lang>/dir and the launcher's EN/FR/... code
  return resolved;
}

async function main() {
  await bootLang();
  applyStatic();
  initTheme();
  initLangPicker(() => {
    applyStatic();
    refreshThemeLabel();
    if (lastConfig) applyConfigBadge(lastConfig);
    // applyStatic() only touches data-i18n(-attr) elements - the disabled search's placeholder
    // was set imperatively by search.disable(), so it needs its own re-localize here.
    if (searchDisabled) search.disable(t("search.unavailable"));
    rerenderCurrent();
  });

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
