#!/usr/bin/env python3
"""
emissions.py - rough per-passenger CO2e estimate for a priced trip option.

This is a back-of-envelope model, not a certified carbon footprint. It exists so a "cheapest
vs greenest" comparison can sit next to the cost/time numbers this tool already produces - same
honesty rules as everywhere else in this codebase: label it an ESTIMATE, never silently
recommend the low-carbon option over the cheap one, let the person looking at the numbers
decide.

Factor basis (grams CO2e per passenger-km, well-to-wake where the source has it, so it
includes upstream fuel production, not just tailpipe/exhaust burn):
  - Flights: UK DEFRA/BEIS 2024 greenhouse gas reporting conversion factors ("Passenger flights"
    table, economy class, WTT+direct combined) and the EEA's per-km figures for EU aviation
    both land in the same rough bands: short-haul (<1500km, more taxi/climb per km flown)
    ~150-250 gCO2e/pkm, long-haul ~130-170 gCO2e/pkm. We use a simple two-band split on
    distance rather than a per-route lookup - good enough for "which of these two options is
    roughly greener," not good enough for an emissions filing.
  - A radiative-forcing multiplier is applied on top: aviation's non-CO2 effects (contrails,
    NOx, water vapor at altitude) roughly double warming impact per DEFRA's own guidance, which
    reports both a CO2-only and a "with RF" figure. We report both so nobody can accuse the
    number of hiding the bigger one.
  - Rail: DEFRA/EEA national-rail figures vary a lot by country grid (a French TGV on
    mostly-nuclear power is far cleaner than a diesel-hauled regional line) - 35-40 gCO2e/pkm is
    a reasonable EU-wide average and what we use as a single global rail factor. Real number
    could be 3-4x lower (electrified, clean grid) or 2x higher (diesel, coal grid) for a
    specific line; this is explicitly a blended default.
  - Coach/bus: DEFRA's "coach" category, ~27-30 gCO2e/pkm - full a coach is one of the
    lowest-carbon ways to move a person any real distance.
  - Car: DEFRA's average petrol/diesel car, ~170 gCO2e per VEHICLE-km (not per passenger) for
    an average-occupancy car; we divide by however many travelers we're told share the vehicle,
    same "per-vehicle" logic trip.py already uses for drive/rental legs.
  - Ferry: DEFRA's ferry (foot passenger) factor, ~20 gCO2e/pkm - not wired into the main
    options table below since ferry legs are rare/short in this tool's ground candidates, but
    included here for completeness and any future caller.

None of this is a live API lookup - deliberately. Emissions factors don't change day to day the
way fares do, hardcoding them keeps this module pure-stdlib with zero moving parts, and it means
the number can't silently go stale from an upstream outage.

Pure stdlib. Run `python -m hopandhaul.emissions --selftest`.
"""
from __future__ import annotations

# ---- factors (grams CO2e per passenger-km, well-to-wake) ------------------------------------
# Flight: split by distance band. The RF (radiative forcing) multiplier is kept separate so
# callers can show "CO2" and "CO2 with non-CO2 warming effects" as two different honest numbers
# instead of picking one silently.
FLIGHT_SHORT_HAUL_KM = 1500.0          # DEFRA/EEA's usual short/long-haul split
FLIGHT_SHORT_HAUL_G_PER_PKM = 246.0    # short-haul economy, more climb/descent per km flown
FLIGHT_LONG_HAUL_G_PER_PKM = 148.0     # long-haul economy, cruise dominates the profile
FLIGHT_RF_MULTIPLIER = 1.9             # DEFRA's "including radiative forcing" uplift (contrails, NOx, etc.)

RAIL_G_PER_PKM = 37.0                  # EU-average blended rail; a clean-grid electrified line
                                        # can be a fraction of this, a diesel regional line more
COACH_G_PER_PKM = 28.0                 # full motorcoach/intercity bus
FERRY_G_PER_PKM = 20.0                 # foot passenger, DEFRA ferry factor (not used by
                                        # co2e_for_leg's mode table below - kept for completeness)
CAR_G_PER_VEHICLE_KM = 170.0           # average petrol/diesel car, DEFRA "average car" factor - 
                                        # per VEHICLE, divide by occupancy for a per-person number

# mode -> (g CO2e per passenger-km, is_per_vehicle) - is_per_vehicle mirrors trip.py's
# PER_VEHICLE_MODES split (drive/car/rental price per vehicle, not per traveler).
_GROUND_FACTORS = {
    "train": (RAIL_G_PER_PKM, False),
    "rail": (RAIL_G_PER_PKM, False),
    "bus": (COACH_G_PER_PKM, False),
    "coach": (COACH_G_PER_PKM, False),
    "shuttle": (COACH_G_PER_PKM, False),   # closest available proxy - a van, not a full coach,
                                            # but no DEFRA "shuttle van" line item exists
    "ferry": (FERRY_G_PER_PKM, False),
    "drive": (CAR_G_PER_VEHICLE_KM, True),
    "car": (CAR_G_PER_VEHICLE_KM, True),
    "rental": (CAR_G_PER_VEHICLE_KM, True),
    "ground": (COACH_G_PER_PKM, False),    # unknown/generic ground leg - coach is the safer
                                            # (lower) default than assuming a solo car
}
FLIGHT_MODES = {"fly", "flight", "plane", "air"}


def flight_g_per_pkm(distance_km: float, with_rf: bool = False) -> float:
    """Grams CO2e per passenger-km for a flight of the given great-circle distance.
    with_rf=True applies the radiative-forcing multiplier (see module docstring)."""
    g = FLIGHT_SHORT_HAUL_G_PER_PKM if distance_km < FLIGHT_SHORT_HAUL_KM else FLIGHT_LONG_HAUL_G_PER_PKM
    return g * FLIGHT_RF_MULTIPLIER if with_rf else g


def co2e_for_leg(mode: str, distance_km: float, travelers: int = 1, with_rf: bool = False) -> float:
    """kg CO2e for one leg, TOTAL for all travelers (matches how trip.py totals leg cost).

    mode: the same leg-mode vocabulary trip.py uses (fly/train/bus/drive/...).
    distance_km: straight-line/great-circle distance for a flight, or the ESTIMATE ground
    distance (road_km, already winding-adjusted) for a ground leg - see co2e_for_option for
    how this wires into the plan() response.
    travelers: passenger count; a per-vehicle mode (drive/car/rental) does NOT scale with it - 
    one car's emissions don't multiply because four people are in it, they divide per person
    for reporting, same as trip.py's cost math but inverted (cost is per-vehicle regardless;
    here we still want the per-vehicle TOTAL, since that's the physical trip actually taken).
    """
    if distance_km <= 0 or travelers < 1:
        return 0.0
    mode = (mode or "").lower()
    if mode in FLIGHT_MODES:
        g_per_pkm = flight_g_per_pkm(distance_km, with_rf=with_rf)
        return round(g_per_pkm * distance_km * travelers / 1000.0, 2)
    g_per_pkm, per_vehicle = _GROUND_FACTORS.get(mode, _GROUND_FACTORS["ground"])
    if per_vehicle:
        # one vehicle makes the whole trip regardless of how many people are in it - total
        # emissions are the same whether it's 1 traveler or 4, so no ×travelers here.
        return round(g_per_pkm * distance_km / 1000.0, 2)
    return round(g_per_pkm * distance_km * travelers / 1000.0, 2)


def co2e_for_option(legs: list[dict], travelers: int = 1, with_rf: bool = False) -> float:
    """kg CO2e for a whole option (sum of its legs). Each leg dict needs 'mode' and a distance - 
    checked in order 'distance_km' then 'road_km' then 'dist_km', so callers can hand this
    either a flight leg (which carries distance_km) or a ground leg (which carries road_km)
    without reshaping first. A leg missing all three contributes 0 rather than raising, so one
    partially-shaped leg doesn't blow up the whole option's estimate."""
    total = 0.0
    for leg in legs:
        dist = leg.get("distance_km", leg.get("road_km", leg.get("dist_km", 0.0))) or 0.0
        total += co2e_for_leg(leg.get("mode", ""), dist, travelers=travelers, with_rf=with_rf)
    return round(total, 2)


# --------------------------------------------------------------------------- self-test
def selftest():
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    # a 1000km short-haul flight, solo traveler: 246 g/pkm * 1000km / 1000 = 246 kg
    f1000 = co2e_for_leg("fly", 1000, travelers=1)
    check(f"1000km flight ~246 kg CO2e (got {f1000})", abs(f1000 - 246.0) < 0.5)

    # same flight with the radiative-forcing multiplier applied: 246 * 1.9 = 467.4 kg
    f1000_rf = co2e_for_leg("fly", 1000, travelers=1, with_rf=True)
    check(f"1000km flight w/ RF ~467.4 kg CO2e (got {f1000_rf})", abs(f1000_rf - 467.4) < 0.5)

    # a 300km train ride, solo traveler: 37 g/pkm * 300km / 1000 = 11.1 kg
    t300 = co2e_for_leg("train", 300, travelers=1)
    check(f"300km train ~11.1 kg CO2e (got {t300})", abs(t300 - 11.1) < 0.1)

    # long-haul band kicks in at/above 1500km: a 6000km flight uses the lower long-haul factor,
    # so per-km it should be cheaper than the short-haul band, not just bigger because farther
    f6000 = co2e_for_leg("fly", 6000, travelers=1)
    check(f"6000km long-haul flight ~888 kg CO2e (got {f6000})", abs(f6000 - 888.0) < 1.0)
    check("long-haul per-km factor is lower than short-haul per-km factor",
          flight_g_per_pkm(6000) < flight_g_per_pkm(1000))

    # flights scale linearly with travelers (per-person mode)
    f1000_x4 = co2e_for_leg("fly", 1000, travelers=4)
    check(f"flight scales x4 with travelers (got {f1000_x4} vs {f1000 * 4})",
          abs(f1000_x4 - f1000 * 4) < 0.5)

    # a drive/rental leg is per-VEHICLE - doesn't scale with travelers, unlike flight/train/bus
    d100_solo = co2e_for_leg("drive", 100, travelers=1)
    d100_grp = co2e_for_leg("drive", 100, travelers=4)
    check(f"drive leg emissions don't scale with travelers ({d100_solo} == {d100_grp})",
          abs(d100_solo - d100_grp) < 0.01)
    check(f"100km drive ~17.0 kg CO2e (got {d100_solo})", abs(d100_solo - 17.0) < 0.1)

    # bus/coach is cheaper per-km than driving solo - one of the honest "greener" signals
    b100 = co2e_for_leg("bus", 100, travelers=1)
    check(f"100km coach ({b100} kg) beats 100km solo drive ({d100_solo} kg) on CO2e",
          b100 < d100_solo)

    # zero/negative distance and zero travelers are handled without raising or going negative
    check("zero distance -> 0 kg", co2e_for_leg("fly", 0) == 0.0)
    check("negative distance -> 0 kg (defensive, not a real input)", co2e_for_leg("train", -5) == 0.0)
    check("zero travelers -> 0 kg", co2e_for_leg("fly", 1000, travelers=0) == 0.0)

    # unknown mode falls back to the generic ground factor rather than raising
    unk = co2e_for_leg("hoverboard", 100, travelers=1)
    check(f"unknown mode falls back to a ground default instead of raising (got {unk})",
          unk == co2e_for_leg("ground", 100, travelers=1))

    # a whole option: a flight leg (distance_km) + a ground leg (road_km) summed correctly
    legs = [{"mode": "fly", "distance_km": 800}, {"mode": "train", "road_km": 120}]
    opt_total = co2e_for_option(legs, travelers=2)
    expected = co2e_for_leg("fly", 800, travelers=2) + co2e_for_leg("train", 120, travelers=2)
    check(f"option total sums its legs correctly ({opt_total} == {round(expected, 2)})",
          abs(opt_total - expected) < 0.01)

    # a leg with no distance field at all contributes 0 instead of raising
    legs_missing = [{"mode": "fly", "distance_km": 500}, {"mode": "train"}]
    opt_partial = co2e_for_option(legs_missing, travelers=1)
    check(f"leg missing every distance key contributes 0, doesn't crash the option total "
          f"(got {opt_partial})", opt_partial == co2e_for_leg("fly", 500, travelers=1))

    # a direct flight should usually beat a fly-then-ground split on time/cost tradeoffs, but
    # NOT always on emissions - a short flight to a far-off gateway plus a long ground leg can
    # beat a long direct flight on CO2e even when it costs more; sanity-check the shape of that
    # by comparing a long direct flight to a short flight + long train
    direct = co2e_for_option([{"mode": "fly", "distance_km": 3000}], travelers=1)
    split = co2e_for_option(
        [{"mode": "fly", "distance_km": 600}, {"mode": "train", "road_km": 2400}], travelers=1)
    check(f"a shorter flight + long train ({split} kg) beats an equivalent-distance direct "
          f"flight ({direct} kg) on CO2e", split < direct)

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (emissions checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print("emissions.py — import me, or run with --selftest")
