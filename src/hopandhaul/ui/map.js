// Leaflet setup + draw(): markers, great-circle arcs, gateway pins.
import { esc, fmtMoney, fmtH, modeIcon, modeLabel } from "./format.js";
import { t } from "./i18n.js";

let map = null;
let tileLayer = null;
let lastPlan = null; // { data, rec } — replayed by setMapTheme() so a theme toggle redraws colors
let currentTheme = null; // last theme actually applied — lets setMapTheme() no-op when unchanged

function tileUrl(theme) {
  const style = theme === "dark" ? "dark_all" : "rastertiles/voyager";
  return `https://{s}.basemaps.cartocdn.com/${style}/{z}/{x}/{y}{r}.png`;
}

export function initMap() {
  // Read the theme theme-boot.js already set pre-paint, so the very first tile request
  // matches what's on screen — no dark-tile flash under a light boot.
  const bootTheme = document.documentElement.getAttribute("data-theme") || "dark";
  currentTheme = bootTheme;
  map = L.map("map", { zoomControl: true, worldCopyJump: true }).setView([41, -30], 3);
  tileLayer = L.tileLayer(tileUrl(bootTheme), {
    attribution: "&copy; OpenStreetMap &copy; CARTO",
    subdomains: "abcd",
    maxZoom: 19,
  }).addTo(map);
  return map;
}

/** Swap the basemap for the given theme — Voyager (warm cream, "travel atlas") in light,
 * dark_all (labels kept — users need city names to click) in dark. Re-draws the last plan
 * so route/pin colors (read live from CSS vars) follow the same toggle gesture. No-ops when
 * `theme` matches what's already applied — callers like refreshThemeLabel() run on every
 * language switch too, and shouldn't trigger a full map rebuild when the theme didn't change. */
export function setMapTheme(theme) {
  if (!tileLayer || theme === currentTheme) return;
  currentTheme = theme;
  tileLayer.setUrl(tileUrl(theme));
  if (lastPlan) draw(lastPlan.data, lastPlan.rec);
}

/** Re-run draw() with whatever plan is currently cached, with no theme/tile change — used to
 * re-translate map popups (built with t() at draw time) after a language switch. No-op if no
 * plan is on screen. */
export function redrawLastPlan() {
  if (lastPlan) draw(lastPlan.data, lastPlan.rec);
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
