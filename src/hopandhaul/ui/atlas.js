// Self-drawn base map: a Leaflet GridLayer that paints land, lakes, and country borders on a
// canvas tile instead of requesting one from a raster tile server. Replaces the CARTO tiles
// this app used to rent - CARTO now requires an API key and, without one, tiled its
// "API KEY REQUIRED" watermark across the whole map. Drawing the base ourselves keeps the app
// at zero keys forever and works fully offline, matching the rest of the PWA.
//
// Geometry comes from ./data/basemap.json (built by tools/build_basemap.py from Natural Earth
// 1:50m, public domain) at two detail tiers - "coarse" for whole-world views, "fine" for
// continent level and closer. Every color is read live from the active theme's CSS custom
// properties, so a theme swap just needs a redraw() - the same live-color pattern map.js
// already uses for pins and route lines.
const DATA_URL = "./data/basemap.json";
const FINE_MIN_ZOOM = 5; // basemap.json tier switch - matches renderGeoLabels' own z>=5 country cutoff

let dataPromise = null;
function loadData() {
  if (!dataPromise) {
    dataPromise = fetch(DATA_URL).then((r) => {
      if (!r.ok) throw new Error(`basemap fetch ${r.status}`);
      return r.json();
    });
  }
  return dataPromise;
}

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

/** Spherical mercator, normalized to [0, 1) x [0, 1) - the same projection Leaflet's own tile
 * grid uses, so a projected point at world-pixel scale `256 * 2**z` lines up with tile edges. */
function project(lng, lat) {
  const x = (lng + 180) / 360;
  const sinLat = Math.sin((lat * Math.PI) / 180);
  const y = 0.5 - Math.log((1 + sinLat) / (1 - sinLat)) / (4 * Math.PI);
  return [x, y];
}

export const Atlas = L.GridLayer.extend({
  options: { tileSize: 256, noWrap: true, updateWhenIdle: false, keepBuffer: 3 },

  initialize(options) {
    L.GridLayer.prototype.initialize.call(this, options);
    this._raw = null;
    // Projected-path cache keyed by "tier" - reprojecting on every tile paint is the difference
    // between smooth panning and visible per-tile jank at world scale (thousands of points).
    this._projected = {};
    loadData().then((raw) => {
      this._raw = raw;
      this.redraw();
    }).catch(() => {
      // Offline on first load, before the service worker has ever cached basemap.json: the
      // tile grid just stays the plain --map-bg water color set on #map itself (styles.css) -
      // no crash, no dangling network retry loop.
    });
  },

  /** Every path in a layer, projected once per tier and memoized - re-run only when the
   * geometry first arrives, never per tile or per zoom. Each path carries its projected bbox
   * so createTile can cull on rectangle overlap. Culling on "has a vertex near this tile"
   * instead is wrong twice over: a tile can sit entirely inside a big polygon (interior US,
   * Siberia, the Sahara) with no coastline vertex anywhere near it, and a long simplified
   * segment can cross a tile with both endpoints far away - both must still be drawn. */
  _tier(z) {
    const tier = z < FINE_MIN_ZOOM ? "coarse" : "fine";
    if (!this._raw) return null;
    if (!this._projected[tier]) {
      const src = this._raw[tier];
      const proj = (layer) => (src[layer] || []).map((path) => {
        const pts = path.map(([lng, lat]) => project(lng, lat));
        let minX = 1, minY = 1, maxX = 0, maxY = 0;
        for (const [x, y] of pts) {
          if (x < minX) minX = x;
          if (y < minY) minY = y;
          if (x > maxX) maxX = x;
          if (y > maxY) maxY = y;
        }
        return { pts, minX, minY, maxX, maxY };
      });
      this._projected[tier] = { land: proj("land"), lakes: proj("lakes"), borders: proj("borders") };
    }
    return this._projected[tier];
  },

  createTile(coords) {
    const tile = document.createElement("canvas");
    tile.width = tile.height = this.options.tileSize;
    const ctx = tile.getContext("2d");
    const data = this._tier(coords.z);
    if (!data) return tile; // no geometry yet - transparent tile over the plain water background

    const scale = this.options.tileSize * 2 ** coords.z;
    const originX = coords.x * this.options.tileSize;
    const originY = coords.y * this.options.tileSize;
    const bleed = 4; // px of slop past the tile edge so a stroke/fill never visibly seams

    // Coordinates are clamped: past ~2^25 canvas path math loses precision, and at high zoom
    // a continent ring's far side is hundreds of millions of tile-pixels away. Clamping a far
    // vertex to a box this much bigger than the tile can't move any drawn edge visibly.
    const CLAMP = 1e6;
    const toPx = (p) => [
      Math.max(-CLAMP, Math.min(CLAMP, p[0] * scale - originX)),
      Math.max(-CLAMP, Math.min(CLAMP, p[1] * scale - originY)),
    ];
    // Tile rect in the normalized [0,1) world the cached bboxes live in.
    const view = {
      minX: (originX - bleed) / scale, maxX: (originX + this.options.tileSize + bleed) / scale,
      minY: (originY - bleed) / scale, maxY: (originY + this.options.tileSize + bleed) / scale,
    };

    const water = cssVar("--map-water", "#8fb8c9");
    const land = cssVar("--map-land", "#d8d2bf");
    const coast = cssVar("--map-coast", "#7a8a90");
    const border = cssVar("--map-border", "#9a9484");
    const graticule = cssVar("--map-graticule", "");

    ctx.fillStyle = water;
    ctx.fillRect(0, 0, tile.width, tile.height);

    if (graticule) drawGraticule(ctx, scale, originX, originY, tile.width, tile.height, graticule);

    fillPaths(ctx, data.land, toPx, view, land, coast);
    fillPaths(ctx, data.lakes, toPx, view, water, coast, 0.6);
    strokePaths(ctx, data.borders, toPx, view, border);

    return tile;
  },
});

/** Bbox-overlap cull. Overlap, not vertex-in-tile: a polygon that fully CONTAINS the tile
 * still overlaps, and drawing it fills the whole tile - which is exactly right for a tile
 * deep inside a continent. Only a path whose bbox is disjoint from the tile can be skipped. */
function pathTouches(p, view) {
  return p.maxX >= view.minX && p.minX <= view.maxX && p.maxY >= view.minY && p.minY <= view.maxY;
}

function fillPaths(ctx, paths, toPx, view, fill, stroke, strokeWidth = 1) {
  ctx.fillStyle = fill;
  ctx.strokeStyle = stroke;
  ctx.lineWidth = strokeWidth;
  ctx.lineJoin = "round";
  for (const path of paths) {
    if (!pathTouches(path, view)) continue;
    ctx.beginPath();
    path.pts.forEach((p, i) => {
      const [x, y] = toPx(p);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.closePath();
    ctx.fill();
    if (strokeWidth > 0) ctx.stroke();
  }
}

function strokePaths(ctx, paths, toPx, view, color) {
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 3]);
  ctx.lineJoin = "round";
  for (const path of paths) {
    if (!pathTouches(path, view)) continue;
    ctx.beginPath();
    path.pts.forEach((p, i) => {
      const [x, y] = toPx(p);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
  ctx.setLineDash([]);
}

/** Faint 10-degree lat/lng lines - purely decorative, off by default (empty --map-graticule)
 * and only drawn when a theme opts in via that token. */
function drawGraticule(ctx, scale, originX, originY, w, h, color) {
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  const step = 10;
  ctx.beginPath();
  for (let lng = -180; lng <= 180; lng += step) {
    const [x] = project(lng, 0);
    const px = x * scale - originX;
    if (px >= -1 && px <= w + 1) { ctx.moveTo(px, 0); ctx.lineTo(px, h); }
  }
  for (let lat = -80; lat <= 80; lat += step) {
    const [, y] = project(0, lat);
    const py = y * scale - originY;
    if (py >= -1 && py <= h + 1) { ctx.moveTo(0, py); ctx.lineTo(w, py); }
  }
  ctx.stroke();
}

export function createAtlas() {
  return new Atlas({ attribution: "Natural Earth" });
}
