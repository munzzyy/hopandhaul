// emissions.js - faithful JS port of hopandhaul/emissions.py's CO2e estimate. Same factor
// basis and honesty rules as the Python original (see emissions.py's module docstring for the
// full DEFRA/EEA sourcing) - this file just needs to keep producing the identical number.
import { pyRound } from "./pyround.js";

const FLIGHT_SHORT_HAUL_KM = 1500.0;
const FLIGHT_SHORT_HAUL_G_PER_PKM = 246.0;
const FLIGHT_LONG_HAUL_G_PER_PKM = 148.0;
const FLIGHT_RF_MULTIPLIER = 1.9;

const RAIL_G_PER_PKM = 37.0;
const COACH_G_PER_PKM = 28.0;
const FERRY_G_PER_PKM = 20.0;
const CAR_G_PER_VEHICLE_KM = 170.0;

// mode -> [g CO2e per passenger-km, isPerVehicle]
const GROUND_FACTORS = {
  train: [RAIL_G_PER_PKM, false],
  rail: [RAIL_G_PER_PKM, false],
  bus: [COACH_G_PER_PKM, false],
  coach: [COACH_G_PER_PKM, false],
  shuttle: [COACH_G_PER_PKM, false],
  ferry: [FERRY_G_PER_PKM, false],
  drive: [CAR_G_PER_VEHICLE_KM, true],
  car: [CAR_G_PER_VEHICLE_KM, true],
  rental: [CAR_G_PER_VEHICLE_KM, true],
  ground: [COACH_G_PER_PKM, false],
};
const FLIGHT_MODES = new Set(["fly", "flight", "plane", "air"]);

export function flightGPerPkm(distanceKm, withRf = false) {
  const g = distanceKm < FLIGHT_SHORT_HAUL_KM ? FLIGHT_SHORT_HAUL_G_PER_PKM : FLIGHT_LONG_HAUL_G_PER_PKM;
  return withRf ? g * FLIGHT_RF_MULTIPLIER : g;
}

/** kg CO2e for one leg, TOTAL for all travelers - mirrors emissions.co2e_for_leg(). */
export function co2eForLeg(mode, distanceKm, travelers = 1, withRf = false) {
  if (distanceKm <= 0 || travelers < 1) return 0.0;
  mode = (mode || "").toLowerCase();
  if (FLIGHT_MODES.has(mode)) {
    const gPerPkm = flightGPerPkm(distanceKm, withRf);
    return pyRound((gPerPkm * distanceKm * travelers) / 1000.0, 2);
  }
  const [gPerPkm, perVehicle] = GROUND_FACTORS[mode] || GROUND_FACTORS.ground;
  if (perVehicle) {
    return pyRound((gPerPkm * distanceKm) / 1000.0, 2);
  }
  return pyRound((gPerPkm * distanceKm * travelers) / 1000.0, 2);
}

/** kg CO2e for a whole option (sum of its legs) - mirrors emissions.co2e_for_option(). Each
 * leg needs 'mode' and a distance, checked in order distance_km, then road_km, then dist_km. */
export function co2eForOption(legs, travelers = 1, withRf = false) {
  let total = 0.0;
  for (const leg of legs) {
    const dist = leg.distance_km ?? leg.road_km ?? leg.dist_km ?? 0.0;
    total += co2eForLeg(leg.mode || "", dist, travelers, withRf);
  }
  return pyRound(total, 2);
}
