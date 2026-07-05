// Leaflet setup + draw(): markers, great-circle arcs, gateway pins.
import { esc, fmtMoney, fmtH, modeEmoji, modeLabel } from "./format.js";

let map = null;

export function initMap() {
  map = L.map("map", { zoomControl: true, worldCopyJump: true }).setView([41, -30], 3);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution: "&copy; OpenStreetMap &copy; CARTO",
    subdomains: "abcd",
    maxZoom: 19,
  }).addTo(map);
  return map;
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

function pin(emoji) {
  return L.divIcon({
    className: "", html: `<div class="pin" aria-hidden="true">${emoji}</div>`,
    iconSize: [24, 24], iconAnchor: [12, 20],
  });
}

/** Drop a single marker (used for the origin pin on an "origin" mode click). */
export function markOrigin(a) {
  add(L.marker([a.lat, a.lng], { icon: pin("🟢") })
    .bindPopup(`Origin: ${esc(a.name)} (${esc(a.iata)})`));
}

/** Clear the map on error so stale markers never linger next to a failed request. */
export function clearMap() {
  clear();
}

/** Draw the full plan result: origin/dest pins, faint direct-flight reference, the
 * recommended route's real geometry, and every candidate gateway hub. */
export function draw(data, rec) {
  clear();
  const O = data.origin, D = data.dest;

  add(L.marker([O.lat, O.lng], { icon: pin("🟢") })
    .bindPopup(`From: ${esc(O.name)} (${esc(O.iata)})`));
  add(L.marker([D.lat, D.lng], { icon: pin("🔴") })
    .bindPopup(`Destination: ${esc(D.name)} (${esc(D.iata)})`));

  add(L.polyline(arc([O.lat, O.lng], [D.lat, D.lng]),
    { color: "#3a4658", weight: 1.5, dashArray: "2,6", opacity: 0.7 }));

  (rec?.geo || []).forEach((leg) => {
    const from = [leg.from.lat, leg.from.lng], to = [leg.to.lat, leg.to.lng];
    if (leg.type === "flight") {
      add(L.polyline(arc(from, to), { color: "#58a6ff", weight: 3, opacity: 0.95 }));
    } else {
      add(L.polyline([from, to], { color: "#a371f7", weight: 3, dashArray: "6,7", opacity: 0.95 }));
    }
  });

  data.gateways.forEach((g) => {
    const isRec = rec?.name?.startsWith(g.iata + " ");
    add(L.marker([g.lat, g.lng], { icon: pin(isRec ? "🟣" : "⚪"), opacity: isRec ? 1 : 0.6 })
      .bindPopup(`${esc(g.name)} (${esc(g.iata)}) → ${modeEmoji(g.ground_mode)} `
        + `${esc(modeLabel(g.ground_mode))} ${fmtH(g.ground_hours)}, ${fmtMoney(g.ground_cost)}`));
  });

  const pts = [[O.lat, O.lng], [D.lat, D.lng], ...data.gateways.map((g) => [g.lat, g.lng])];
  map.fitBounds(L.latLngBounds(pts).pad(0.35), { maxZoom: 8 });
}
