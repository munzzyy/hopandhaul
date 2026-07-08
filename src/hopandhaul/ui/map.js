// Leaflet setup + draw(): markers, great-circle arcs, gateway pins.
import { esc, fmtMoney, fmtH, modeIcon, modeLabel } from "./format.js";
import { t, currentLangCode } from "./i18n.js";
import { CONTINENTS, COUNTRIES, continentName } from "./geo-labels.js";

let map = null;
let tileLayer = null;
let lastPlan = null; // { data, rec } — replayed by setMapTheme() so a theme toggle redraws colors
let currentTheme = null; // last theme actually applied — lets setMapTheme() no-op when unchanged

// One world, no repeats: the base map wraps infinitely by default, so panning used to reveal
// copies of Earth side by side. maxBounds (with full viscosity) plus a noWrap tile layer keeps
// exactly one world on screen.
const WORLD_BOUNDS = L.latLngBounds([-85, -180], [85, 180]);

function tileUrl(theme) {
  // *_nolabels bases: the tile images carry no place names, so the only labels on the map are
  // our own translated overlay (renderGeoLabels), instead of tile text baked in English.
  const style = theme === "dark" ? "dark_nolabels" : "rastertiles/voyager_nolabels";
  return `https://{s}.basemaps.cartocdn.com/${style}/{z}/{x}/{y}{r}.png`;
}

export function initMap() {
  // theme-boot.js sets data-theme pre-paint to one of 8 theme codes, not just "dark"/"light" —
  // read the light/dark SCHEME the browser already resolved from it (every [data-theme] block
  // in styles.css sets color-scheme) rather than assuming the raw attribute value IS the base,
  // so the very first tile request matches what's on screen for every theme, not just the two
  // literally named "dark" and "light".
  const bootBase = getComputedStyle(document.documentElement).colorScheme === "dark" ? "dark" : "light";
  currentTheme = bootBase;
  map = L.map("map", {
    zoomControl: true,
    minZoom: 2,
    maxBounds: WORLD_BOUNDS,
    maxBoundsViscosity: 1,
  }).setView([41, -30], 3);
  tileLayer = L.tileLayer(tileUrl(bootBase), {
    attribution: "&copy; OpenStreetMap &copy; CARTO",
    subdomains: "abcd",
    maxZoom: 19,
    noWrap: true,
    bounds: WORLD_BOUNDS,
  }).addTo(map);
  geoLabels.addTo(map);
  map.on("zoomend moveend", renderGeoLabels);
  renderGeoLabels();
  return map;
}

/** Swap the basemap for the given theme — Voyager (warm cream, "travel atlas") in light,
 * dark_nolabels in dark. Both label-free; our own overlay carries the (translated) place names.
 * Tiles only, and a no-op when `theme` matches what's already applied — callers like
 * refreshThemeLabel() run on every language switch too, and shouldn't trigger a tile reload when
 * the theme didn't change. Redrawing the plan overlay is the theme-change caller's job (apply()
 * in theme.js): route colors are hex snapshots taken at draw time, so they go stale on ANY theme
 * change — including one between two themes that share a tile base, where this correctly no-ops. */
export function setMapTheme(theme) {
  if (!tileLayer || theme === currentTheme) return;
  currentTheme = theme;
  tileLayer.setUrl(tileUrl(theme));
}

/** Re-run draw() with whatever plan is currently cached, with no theme/tile change — used to
 * re-translate map popups (built with t() at draw time) after a language switch. No-op if no
 * plan is on screen. */
export function redrawLastPlan() {
  if (lastPlan) draw(lastPlan.data, lastPlan.rec);
}

// --- translated place labels: continents + countries, names resolved per active locale ---
// The tiles are label-free; these overlay labels are the map's place names. Every name comes
// from Intl.DisplayNames("region") for the current UI language — country ISO codes and UN M49
// continent codes both localize — so all 56 languages are covered with no translation data.
const geoLabels = L.layerGroup();
let namesLocale = null, regionNames = null;

function regionNamesFor(locale) {
  if (locale !== namesLocale) {
    namesLocale = locale;
    try { regionNames = new Intl.DisplayNames([locale], { type: "region" }); }
    catch { regionNames = new Intl.DisplayNames(["en"], { type: "region" }); }
  }
  return regionNames;
}

function placeName(code) {
  try { return regionNames && (regionNames.of(code) || null); } catch { return null; }
}

// Cheap axis-aligned overlap test on pixel label boxes, so labels don't stack on each other.
function overlaps(a, b) {
  return Math.abs(a.x - b.x) * 2 < a.w + b.w && Math.abs(a.y - b.y) * 2 < a.h + b.h;
}

/** Redraw continent + country labels for the current zoom, view, and language. Continents are
 * placed first, so when you're zoomed out they win and you read continents; countries fill the
 * gaps and take over as you zoom in and they stop colliding. Called on zoom, pan, and language
 * change. Labels are non-interactive, so map clicks pass straight through to pick a destination. */
export function renderGeoLabels() {
  if (!map) return;
  geoLabels.clearLayers();
  const locale = currentLangCode();
  regionNamesFor(locale);
  const z = map.getZoom(), view = map.getBounds(), placed = [];
  const items = [];
  if (z <= 5) for (const c of CONTINENTS) items.push({ code: c.code, at: c.at, kind: "continent" });
  if (z >= 3) for (const iso in COUNTRIES) items.push({ code: iso, at: COUNTRIES[iso], kind: "country" });
  for (const it of items) {
    if (!view.contains(it.at)) continue;
    const name = it.kind === "continent" ? continentName(it.code, locale) : placeName(it.code);
    if (!name) continue;
    const p = map.latLngToContainerPoint(it.at);
    const box = { x: p.x, y: p.y, w: name.length * 7.5 + 12, h: 18 };
    if (placed.some((q) => overlaps(box, q))) continue;
    placed.push(box);
    geoLabels.addLayer(L.marker(it.at, {
      interactive: false, keyboard: false,
      icon: L.divIcon({ className: `geo-label geo-label--${it.kind}`, html: `<span>${esc(name)}</span>`, iconSize: [0, 0] }),
    }));
  }
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

let layers = [];
function clear() {
  layers.forEach((l) => map.removeLayer(l));
  layers = [];
}
function add(l) {
  layers.push(l.addTo(map));
  return l;
}

/** Great-circle points between two [lat,lng] pairs for a Leaflet polyline. */
export function arc(a, b, segs = 64) {
  const toR = Math.PI / 180, toD = 180 / Math.PI;
  const lat1 = a[0] * toR, lon1 = a[1] * toR, lat2 = b[0] * toR, lon2 = b[1] * toR;
  const d = 2 * Math.asin(Math.sqrt(
    Math.sin((lat2 - lat1) / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin((lon2 - lon1) / 2) ** 2,
  ));
  if (d === 0) return [a, b];
  const pts = [];
  for (let i = 0; i <= segs; i++) {
    const f = i / segs, A = Math.sin((1 - f) * d) / Math.sin(d), B = Math.sin(f * d) / Math.sin(d);
    const x = A * Math.cos(lat1) * Math.cos(lon1) + B * Math.cos(lat2) * Math.cos(lon2);
    const y = A * Math.cos(lat1) * Math.sin(lon1) + B * Math.cos(lat2) * Math.sin(lon2);
    const z = A * Math.sin(lat1) + B * Math.sin(lat2);
    pts.push([Math.atan2(z, Math.sqrt(x * x + y * y)) * toD, Math.atan2(y, x) * toD]);
  }
  return pts;
}

// 12px filled dot + 2px panel-colored ring, colored via the wrapper's `pin--*` class
// (currentColor) so a theme swap re-colors every pin with no re-render — the color comes
// entirely from CSS, matching the legend swatches exactly.
function pin(kind) {
  return L.divIcon({
    className: "",
    html: `<div class="pin-svg pin--${kind}" aria-hidden="true">`
      + `<svg width="12" height="12" viewBox="0 0 12 12">`
      + `<circle cx="6" cy="6" r="5" fill="currentColor" stroke="var(--panel)" stroke-width="2"/>`
      + `</svg></div>`,
    iconSize: [36, 36], iconAnchor: [18, 18],
  });
}

/** Drop a single marker (used for the origin pin on an "origin" mode click). */
export function markOrigin(a) {
  add(L.marker([a.lat, a.lng], { icon: pin("origin") })
    .bindPopup(esc(t("map.originPin", { name: a.name, iata: a.iata }))));
}

/** Clear the map on error so stale markers never linger next to a failed request. */
export function clearMap() {
  clear();
  lastPlan = null;
}

/** Draw the full plan result: origin/dest pins, faint direct-flight reference, the
 * recommended route's real geometry, and every candidate gateway hub. Colors are read live
 * from the CSS custom properties at draw time — zero hardcoded hexes in JS — so a theme
 * toggle just needs to re-call draw() with the same data to restyle everything. */
export function draw(data, rec) {
  clear();
  lastPlan = { data, rec };
  const O = data.origin, D = data.dest;
  const arcColor = cssVar("--arc"), railColor = cssVar("--rail"), refColor = cssVar("--map-ref");

  add(L.marker([O.lat, O.lng], { icon: pin("origin") })
    .bindPopup(esc(t("map.fromPin", { name: O.name, iata: O.iata }))));
  add(L.marker([D.lat, D.lng], { icon: pin("dest") })
    .bindPopup(esc(t("map.destPin", { name: D.name, iata: D.iata }))));

  add(L.polyline(arc([O.lat, O.lng], [D.lat, D.lng]),
    { color: refColor, weight: 1.5, dashArray: "2 6", opacity: 0.5 }));

  (rec?.geo || []).forEach((leg) => {
    const from = [leg.from.lat, leg.from.lng], to = [leg.to.lat, leg.to.lng];
    if (leg.type === "flight") {
      // dotted bead pattern = the profile's dotted-arc motif, distinct from the dashed
      // ground leg so the air/ground mode split reads at a glance.
      add(L.polyline(arc(from, to), { color: arcColor, weight: 3.5, lineCap: "round", dashArray: "1 8" }));
    } else {
      add(L.polyline([from, to], { color: railColor, weight: 3, lineCap: "round", dashArray: "8 7" }));
    }
  });

  data.gateways.forEach((g) => {
    const isRec = rec?.name?.startsWith(g.iata + " ");
    add(L.marker([g.lat, g.lng], { icon: pin(isRec ? "hub-rec" : "hub"), opacity: isRec ? 1 : 0.7 })
      .bindPopup(`${esc(g.name)} (${esc(g.iata)}) ${modeIcon(g.ground_mode)} `
        + `${esc(modeLabel(g.ground_mode))} ${fmtH(g.ground_hours)}, ${fmtMoney(g.ground_cost)}`));
  });

  const pts = [[O.lat, O.lng], [D.lat, D.lng], ...data.gateways.map((g) => [g.lat, g.lng])];
  map.fitBounds(L.latLngBounds(pts).pad(0.35), { maxZoom: 8 });
}
