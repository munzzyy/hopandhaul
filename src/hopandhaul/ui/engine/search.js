// search.js - local, offline "type a place" search over the shipped airport DB.
//
// Not a port of anything in geo.py/geoapify.py - geoapify.py is a real typed-address geocoder
// (any street address, landmark, neighborhood) backed by a paid API, and there's no key for it
// on GitHub Pages. This is an honest, smaller replacement: search the same 4,175-airport
// database the map already ships, by IATA code, city, or airport name. It covers "type a city
// or airport" well; it will not resolve a street address the way the live geocoder does - the
// map-click flow (geo.nearestAirport) is still the primary, most precise way to set a point.
import { airports } from "./data.js";

export function searchAirports(query, limit = 6) {
  const q = String(query || "").trim().toLowerCase();
  if (!q) return [];

  const scored = [];
  for (const a of airports()) {
    const iata = a.iata.toLowerCase();
    const city = (a.city || "").toLowerCase();
    const name = (a.name || "").toLowerCase();
    let rank;
    if (iata === q) rank = 0;
    else if (city === q) rank = 1;
    else if (iata.startsWith(q)) rank = 2;
    else if (city.startsWith(q)) rank = 3;
    else if (name.startsWith(q)) rank = 4;
    else if (city.includes(q) || name.includes(q)) rank = 5;
    else continue;
    scored.push({ rank, a });
  }

  scored.sort((x, y) => (
    (x.rank - y.rank)
    || (x.a.hub - y.a.hub) // prefer real hubs over small fields on a tie
    || x.a.iata.localeCompare(y.a.iata)
  ));

  return scored.slice(0, Math.max(0, limit)).map(({ a }) => ({
    label: a.city ? `${a.city} (${a.iata}) — ${a.name}` : `${a.name} (${a.iata})`,
    lat: a.lat,
    lng: a.lng,
    type: "airport",
    country_code: a.country || null,
    iata: a.iata,
  }));
}
