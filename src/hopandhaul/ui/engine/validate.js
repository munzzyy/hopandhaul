// validate.js - input validation for the browser engine's public entry points.
//
// server.py's HTTP handler used to be the trust boundary (query-string params off the wire,
// see its _v_* validators). On Pages there's no server, but there's still an untrusted-ish
// input path - a hand-edited share URL (state.js's readUrlState()) can hand app.js's form
// fields any string - so the same bounds checks apply here, at the new boundary: api.js, right
// before anything reaches the engine. Same constants/messages as server.py, so error text the
// UI already knows how to render (results.js's renderError) stays correct unchanged.

export class ValidationError extends Error {
  constructor(message) {
    super(message);
    this.name = "ValidationError";
    this.message = message;
  }
}

export const MAX_QUERY_TEXT_LEN = 200;
export const MAX_IATA_LEN = 4;
export const MAX_TRAVELERS = 9;
export const MIN_THRESHOLD = 0.0;
export const MAX_THRESHOLD = 100_000.0;
export const MIN_GROUND_H = 0.0;
export const MAX_GROUND_H = 48.0;
export const MIN_BUFFER_H = 0.0;
export const MAX_BUFFER_H = 24.0;
export const MAX_VOT = 10_000.0;

export function vLat(raw) {
  const v = Number(raw);
  if (raw === "" || raw === null || raw === undefined || Number.isNaN(v)) throw new ValidationError("lat must be a number");
  if (!(v >= -90.0 && v <= 90.0)) throw new ValidationError("lat must be between -90 and 90");
  return v;
}

export function vLng(raw) {
  const v = Number(raw);
  if (raw === "" || raw === null || raw === undefined || Number.isNaN(v)) throw new ValidationError("lng must be a number");
  if (!(v >= -180.0 && v <= 180.0)) throw new ValidationError("lng must be between -180 and 180");
  return v;
}

export function vIata(raw) {
  const v = String(raw).trim().toUpperCase();
  const asciiLettersOnly = /^[A-Z]+$/.test(v);
  if (!v || v.length > MAX_IATA_LEN || !asciiLettersOnly) {
    throw new ValidationError("origin must be a short airport code (letters only)");
  }
  return v;
}

export function vDate(raw, field) {
  const v = String(raw).trim();
  if (v.length !== 10 || v[4] !== "-" || v[7] !== "-") throw new ValidationError(`${field} must be YYYY-MM-DD`);
  const year = v.slice(0, 4), month = v.slice(5, 7), day = v.slice(8, 10);
  if (!/^\d+$/.test(year) || !/^\d+$/.test(month) || !/^\d+$/.test(day)) {
    throw new ValidationError(`${field} must be YYYY-MM-DD`);
  }
  const y = Number(year), m = Number(month), d = Number(day);
  const dt = new Date(Date.UTC(y, m - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== m - 1 || dt.getUTCDate() !== d) {
    throw new ValidationError(`${field} is not a real calendar date`);
  }
  return v;
}

export function vFloatRange(raw, field, lo, hi) {
  const v = Number(raw);
  if (raw === "" || raw === null || raw === undefined || Number.isNaN(v)) throw new ValidationError(`${field} must be a number`);
  if (!(v >= lo && v <= hi)) throw new ValidationError(`${field} must be between ${lo} and ${hi}`);
  return v;
}

export function vIntRange(raw, field, lo, hi) {
  const v = Number(raw);
  if (raw === "" || raw === null || raw === undefined || Number.isNaN(v) || !Number.isInteger(v)) {
    throw new ValidationError(`${field} must be a whole number`);
  }
  if (!(v >= lo && v <= hi)) throw new ValidationError(`${field} must be between ${lo} and ${hi}`);
  return v;
}

export function vBoolFlag(raw) {
  return raw === "1" || raw === "true" || raw === "yes" || raw === true;
}

export function vQueryText(raw, field = "q") {
  const v = String(raw ?? "").trim();
  if (!v) throw new ValidationError(`${field} is required`);
  if (v.length > MAX_QUERY_TEXT_LEN) throw new ValidationError(`${field} is too long (max ${MAX_QUERY_TEXT_LEN} chars)`);
  return v;
}

function optional(v) {
  return v === undefined || v === null || v === "" ? null : v;
}

/** Validates a fetchPlan()-style params object (string-or-number fields, same names api.js's
 * old query-string builder used) - mirrors server.py's parse_plan_params(). */
export function parsePlanParams(p) {
  const lat = vLat(p.lat);
  const lng = vLng(p.lng);
  const out = { dest_lat: lat, dest_lng: lng };

  const origin = optional(p.origin);
  out.origin_iata = origin !== null ? vIata(origin) : "JFK";

  const date = optional(p.date);
  out.date = date !== null ? vDate(date, "date") : null;

  const ret = optional(p.ret);
  out.ret = ret !== null ? vDate(ret, "ret") : null;
  if (out.ret && out.date && out.ret < out.date) {
    throw new ValidationError("return date must be on or after the depart date");
  }

  const vot = optional(p.vot);
  out.vot = vot !== null ? vFloatRange(vot, "vot", 0.0, MAX_VOT) : null;

  const threshold = optional(p.threshold);
  out.threshold = threshold !== null ? vFloatRange(threshold, "threshold", MIN_THRESHOLD, MAX_THRESHOLD) : 200.0;

  const maxGroundH = optional(p.maxGroundH);
  out.max_ground_h = maxGroundH !== null ? vFloatRange(maxGroundH, "maxGroundH", MIN_GROUND_H, MAX_GROUND_H) : 6.0;

  out.roundtrip = vBoolFlag(optional(p.round) ?? "0");

  const travelers = optional(p.travelers);
  out.travelers = travelers !== null ? vIntRange(travelers, "travelers", 1, MAX_TRAVELERS) : 1;

  const buffer_ = optional(p.buffer);
  out.transfer_buffer = buffer_ !== null ? vFloatRange(buffer_, "buffer", MIN_BUFFER_H, MAX_BUFFER_H) : 1.0;

  return out;
}

export function parseNearestParams(p) {
  return { lat: vLat(p.lat), lng: vLng(p.lng) };
}
