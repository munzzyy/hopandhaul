#!/usr/bin/env python3
"""
multicity.py - order N cities into one trip: `python -m hopandhaul.multicity --home DEN
--visit LAX,SEA,SFO,PDX`.

Two independent halves, kept separate on purpose (same split as trip.py/geo.py). The TSP
core (held_karp / nearest_neighbor / two_opt / solve_tour) takes a plain cost matrix and
returns a visiting order - no airports, no dollars, just numbers in and an order out, so it
can be tested and reasoned about on its own. price_leg() and build_cost_matrix() are the
other half: they fill that matrix in using the exact same reasoning the rest of this repo
already trusts, geo.py's flight/ground estimates and trip.py's $200-rule evaluate() deciding,
leg by leg, whether flying into a cheaper hub and grounding it beats the direct flight - the
same call server.py's plan() and duffel.py's build_and_evaluate() already make for a single
origin/destination pair, just run once per directed pair of stops.

Held-Karp is exact and cheap up to a handful of cities (bitmask DP, O(n^2 * 2^n)); past
EXACT_LIMIT cities to visit it switches to nearest-neighbor construction plus 2-opt, which
is a HEURISTIC - a good tour, not provably the cheapest one. Every leg is priced offline
(geo.py's distance estimate, no live Duffel lookups) so the same inputs always produce the
same tour - point this at DUFFEL_API_KEY-backed live prices some day and that guarantee is
the first thing to go.

Examples:
  hopandhaul multicity --home DEN --visit "LAX,SEA,SFO,PDX"
  python -m hopandhaul.multicity --home JFK --visit "Boston,Aspen" --open --travelers 2
  python -m hopandhaul.multicity --selftest
"""
from __future__ import annotations

import argparse
import json
import math
import sys

from . import geo, go, trip

EXACT_LIMIT = 9   # cities to visit (home excluded) - Held-Karp above this gets slow fast


# --------------------------------------------------------------------------- TSP core
def tour_cost(cost: list[list[float]], order: list[int], round_trip: bool) -> float:
    """Total cost of visiting `order` (a list of matrix indices) in sequence. Node 0 is
    always home and always order[0] - round_trip adds the closing leg back to it."""
    total = sum(cost[order[i]][order[i + 1]] for i in range(len(order) - 1))
    if round_trip:
        total += cost[order[-1]][order[0]]
    return round(total, 2)


def held_karp(cost: list[list[float]], round_trip: bool) -> dict:
    """Exact TSP over an n x n cost matrix; cost[i][j] is the price of going i -> j and
    need not equal cost[j][i] (a real leg often isn't - see geo.estimate_flight's hub-size
    premium, which lands harder at the destination than the origin). Node 0 is fixed as
    the start. round_trip closes the tour back to node 0; otherwise it ends wherever is
    cheapest. Bitmask DP over the other n-1 nodes: dp[mask][j] is the cheapest way to
    start at 0, visit exactly the nodes in mask, and end at j. A mask with a bit cleared
    is always numerically smaller than the mask it came from, so a single pass over
    mask = 1 .. 2**(n-1) - 1 in increasing order sees every predecessor before it's needed.
    """
    n = len(cost)
    if n == 1:
        return {"order": [0], "cost": 0.0}
    nodes = list(range(1, n))
    size = len(nodes)
    dp = [[math.inf] * size for _ in range(1 << size)]
    parent = [[-1] * size for _ in range(1 << size)]
    for i, node in enumerate(nodes):
        dp[1 << i][i] = cost[0][node]
    for mask in range(1, 1 << size):
        for i, node_i in enumerate(nodes):
            if not (mask & (1 << i)) or dp[mask][i] == math.inf:
                continue
            base = dp[mask][i]
            for j, node_j in enumerate(nodes):
                if mask & (1 << j):
                    continue
                nmask = mask | (1 << j)
                candidate = base + cost[node_i][node_j]
                if candidate < dp[nmask][j]:
                    dp[nmask][j] = candidate
                    parent[nmask][j] = i
    full = (1 << size) - 1
    best_cost, best_last = math.inf, -1
    for i in range(size):
        c = dp[full][i] + (cost[nodes[i]][0] if round_trip else 0.0)
        if c < best_cost:
            best_cost, best_last = c, i
    order_idx = []
    mask, last = full, best_last
    while last != -1:
        order_idx.append(last)
        prev = parent[mask][last]
        mask ^= 1 << last
        last = prev
    order_idx.reverse()
    return {"order": [0] + [nodes[i] for i in order_idx], "cost": round(best_cost, 2)}


def nearest_neighbor(cost: list[list[float]]) -> list[int]:
    """Greedy construction: from home, always hop to whichever unvisited node is cheapest
    to reach next. A reasonable starting tour, not a good one on its own - solve_tour
    always runs two_opt over the result."""
    n = len(cost)
    visited = [False] * n
    visited[0] = True
    order = [0]
    current = 0
    for _ in range(n - 1):
        nxt, best = None, math.inf
        for j in range(n):
            if not visited[j] and cost[current][j] < best:
                nxt, best = j, cost[current][j]
        visited[nxt] = True
        order.append(nxt)
        current = nxt
    return order


def two_opt(cost: list[list[float]], order: list[int], round_trip: bool) -> list[int]:
    """Repeated best-improvement 2-opt: try reversing every segment of the tour and keep
    the reversal if it lowers the total. Home (position 0) never moves. Each candidate's
    cost is recomputed from scratch rather than tracked as an incremental delta - for an
    asymmetric matrix, reversing a segment flips the direction of every edge inside it, so
    a delta computed only from the two new boundary edges would be wrong."""
    order = list(order)
    n = len(order)
    improved = True
    while improved:
        improved = False
        best_cost = tour_cost(cost, order, round_trip)
        for i in range(1, n - 1):
            for k in range(i + 1, n):
                candidate = order[:i] + order[i:k + 1][::-1] + order[k + 1:]
                c = tour_cost(cost, candidate, round_trip)
                if c < best_cost - 1e-9:
                    order, best_cost, improved = candidate, c, True
    return order


def solve_tour(cost: list[list[float]], round_trip: bool = True) -> dict:
    """Dispatch to the exact solver for a small city count, the heuristic one otherwise.
    `cost` must be square, node 0 is home. Returns {'order', 'cost', 'method', 'exact'}."""
    n = len(cost)
    if n - 1 <= EXACT_LIMIT:
        res = held_karp(cost, round_trip)
        return {"order": res["order"], "cost": res["cost"],
                "method": "held-karp (exact)", "exact": True}
    order = two_opt(cost, nearest_neighbor(cost), round_trip)
    return {"order": order, "cost": tour_cost(cost, order, round_trip),
            "method": "nearest-neighbor + 2-opt (heuristic, not provably optimal)",
            "exact": False}


# --------------------------------------------------------------------------- pricing (reuses geo.py/trip.py)
def price_leg(origin: dict, dest: dict, *, travelers: int = 1,
             threshold: float = trip.DEFAULT_THRESHOLD, vot: float | None = None,
             transfer_buffer: float = 1.0, max_ground_h: float = 6.0) -> dict:
    """Cheapest honest way from one airport to another: the direct flight, or flying into
    a cheaper hub and grounding it from there, decided by trip.py's $200-rule evaluate() -
    exactly what server.py's plan() and duffel.py's build_and_evaluate() already do for a
    single origin/destination, minus the live-fare/weather/itinerary/emissions machinery
    those carry (a tour's cost matrix only needs the number, not a booking-ready itinerary,
    and skipping the network keeps the whole optimizer deterministic and fast)."""
    options = []
    direct = geo.estimate_flight(origin, dest)
    direct_cost = round(direct["price"] * max(1, travelers), 2)
    direct_name = f"fly direct to {dest['iata']}"
    options.append(trip.parse_option(f"{direct_name} | fly {direct_cost} {direct['hours']}"))

    for g in geo.discover_gateways(dest, origin=origin, max_ground_h=max_ground_h):
        gf = geo.estimate_flight(origin, g)
        fly_cost = round(gf["price"] * max(1, travelers), 2)
        ground_cost = trip.scale_leg_cost(g["ground_mode"], g["ground_cost"], travelers)
        name = f"{g['iata']} + {g['ground_mode']}"
        options.append(trip.parse_option(
            f"{name} | fly {fly_cost} {gf['hours']} ; "
            f"{g['ground_mode']} {ground_cost} {g['ground_hours']}"))

    res = trip.evaluate(options, threshold=threshold, vot=vot,
                        transfer_buffer=transfer_buffer, travelers=travelers)
    rec = next(o for o in res["options"] if o["name"] == res["recommended"])
    return {"from": origin["iata"], "to": dest["iata"], "name": rec["name"],
            "cost": rec["cost"], "hours": rec["hours_eff"],
            "is_split": rec["is_split"], "legs": rec["legs"]}


def build_cost_matrix(airports: list[dict], **price_kwargs) -> tuple[list[list[float]], dict]:
    """Price every directed pair among `airports` (index 0 = home). Returns (matrix, legs)
    where legs[(i, j)] is price_leg()'s full result for i -> j, so the printed itinerary
    can name which mode won each leg alongside the price."""
    n = len(airports)
    matrix = [[0.0] * n for _ in range(n)]
    legs = {}
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            leg = price_leg(airports[i], airports[j], **price_kwargs)
            matrix[i][j] = leg["cost"]
            legs[(i, j)] = leg
    return matrix, legs


def _dedupe_by_iata(airports: list[dict]) -> list[dict]:
    seen, out = set(), []
    for a in airports:
        if a["iata"] in seen:
            continue
        seen.add(a["iata"])
        out.append(a)
    return out


def plan_multicity(home_query: str, visit_queries: list[str], *, round_trip: bool = True,
                   travelers: int = 1, threshold: float = trip.DEFAULT_THRESHOLD,
                   vot: float | None = None, transfer_buffer: float = 1.0,
                   max_ground_h: float = 6.0) -> dict:
    """Resolve a home base and a set of cities, price every directed pair between them, and
    hand the resulting matrix to solve_tour(). Raises ValueError on an unresolvable place or
    fewer than 2 distinct cities to route (nothing to order)."""
    home, _ = go.resolve_airport(home_query)
    if not home:
        raise ValueError(f"no airport matches home {home_query!r}")
    airports = [home]
    unresolved = []
    for q in visit_queries:
        a, _ = go.resolve_airport(q)
        if a is None:
            unresolved.append(q)
        else:
            airports.append(a)
    if unresolved:
        raise ValueError(f"no airport matches: {', '.join(unresolved)}")

    airports = _dedupe_by_iata(airports)
    if len(airports) < 3:
        raise ValueError("need a home base plus at least 2 distinct cities to route a tour")

    price_kwargs = dict(travelers=travelers, threshold=threshold, vot=vot,
                        transfer_buffer=transfer_buffer, max_ground_h=max_ground_h)
    matrix, legs = build_cost_matrix(airports, **price_kwargs)
    solved = solve_tour(matrix, round_trip=round_trip)
    order = solved["order"]

    itinerary = [legs[(order[i], order[i + 1])] for i in range(len(order) - 1)]
    if round_trip:
        itinerary.append(legs[(order[-1], order[0])])

    return {
        "home": home["iata"], "round_trip": round_trip, "travelers": travelers,
        "method": solved["method"], "exact": solved["exact"],
        "stops": [airports[i]["iata"] for i in order],
        "total_cost": solved["cost"], "itinerary": itinerary,
        "airports": {a["iata"]: {"iata": a["iata"], "name": a["name"], "city": a.get("city")}
                    for a in airports},
    }


# --------------------------------------------------------------------------- reporting
def _fmt_money(x: float) -> str:
    return f"${x:,.0f}" if abs(x - round(x)) < 0.005 else f"${x:,.2f}"


def _fmt_hours(h: float) -> str:
    total_min = round(h * 60)
    hh, mm = divmod(total_min, 60)
    return f"{hh}h{mm:02d}" if mm else f"{hh}h"


def format_report(res: dict) -> str:
    L = []
    kind = "round trip" if res["round_trip"] else "open tour"
    L.append(f"MULTI-CITY TOUR: {' -> '.join(res['stops'])}"
             + (f" -> {res['home']}" if res["round_trip"] else ""))
    L.append(f"{kind}, {len(res['stops'])} stops, solved via {res['method']}")
    if res["travelers"] > 1:
        L.append(f"Travelers: {res['travelers']} - costs are GROUP TOTALS "
                 f"(per-person fares x{res['travelers']}; drive/rental legs are per vehicle).")
    L.append("")
    L.append("ITINERARY:")
    airports = res["airports"]
    for i, leg in enumerate(res["itinerary"], start=1):
        a_from, a_to = airports[leg["from"]], airports[leg["to"]]
        legs_str = " + ".join(f"{hop['mode']} {_fmt_money(hop['cost'])}" for hop in leg["legs"])
        kind_tag = "multimodal" if leg["is_split"] else "direct"
        L.append(f"  {i}. {a_from['iata']} -> {a_to['iata']}  "
                 f"{_fmt_money(leg['cost']).rjust(7)}  {_fmt_hours(leg['hours']).rjust(6)}  "
                 f"{kind_tag.ljust(10)} ({legs_str})")
    L.append("")
    L.append(f"TOTAL: {_fmt_money(res['total_cost'])} across {len(res['itinerary'])} legs")
    if not res["exact"]:
        L.append(f"  ({EXACT_LIMIT}+ cities to visit: nearest-neighbor + 2-opt found a good "
                 f"tour, not necessarily the cheapest possible one)")
    return "\n".join(L)


# --------------------------------------------------------------------------- CLI
def _split_visits(raw: list[str]) -> list[str]:
    out = []
    for group in raw:
        out.extend(s.strip() for s in group.split(",") if s.strip())
    return out


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Multi-city tour optimizer: order N cities into one trip, "
                    "flight+ground leg by leg, with the $200 fly-then-train rule per hop.")
    p.add_argument("--home", help="home base airport code or city, e.g. DEN")
    p.add_argument("--visit", action="append", default=[],
                   help="comma-separated cities/codes to visit, e.g. 'LAX,SEA,SFO,PDX' "
                        "(repeatable)")
    p.add_argument("--open", action="store_true",
                   help="end the tour at the last stop instead of returning home")
    p.add_argument("--travelers", type=int, default=1,
                   help="group size: per-person legs scale x N, drive/rental legs are per vehicle")
    p.add_argument("--threshold", type=float, default=trip.DEFAULT_THRESHOLD,
                   help=f"min $ a per-leg split must save vs direct (default {trip.DEFAULT_THRESHOLD:g})")
    p.add_argument("--vot", type=float, default=None, help="value of time $/hr")
    p.add_argument("--transfer-buffer", type=float, default=1.0,
                   help="hours added per connection on a split leg (default 1.0)")
    p.add_argument("--max-ground-hours", type=float, default=6.0, dest="max_ground_h",
                   help="longest ground leg a gateway split may use (default 6.0)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    p.add_argument("--selftest", action="store_true", help="run built-in checks and exit")
    return p


def main(argv=None) -> int:
    trip._force_utf8()
    p = _build_parser()
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()

    visits = _split_visits(args.visit)
    if not args.home or not visits:
        p.error("give --home and at least two --visit cities")

    try:
        if args.travelers < 1:
            raise ValueError("--travelers must be >= 1")
        if args.threshold < 0:
            raise ValueError("--threshold must be >= 0")
        res = plan_multicity(
            args.home, visits, round_trip=not args.open, travelers=args.travelers,
            threshold=args.threshold, vot=args.vot, transfer_buffer=args.transfer_buffer,
            max_ground_h=args.max_ground_h)
    except ValueError as e:
        print(f"multicity: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(format_report(res))
    return 0


# --------------------------------------------------------------------------- self-test
def _approx(a, b, tol=0.01):
    return abs(a - b) <= tol


def selftest() -> int:
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    # Case 1: pure TSP core, a hand-built 4-node cost matrix on a line (home=0, A=1, B=10,
    # C=11) with cost = |i - j|. The cheap tour hugs the line (22); crossing back and forth
    # (home -> B -> A -> C -> home) wastes a trip across the gap twice (40). Held-Karp has
    # to land on the cheap one instead of any old valid one.
    line_cost = [[abs(p - q) for q in (0, 1, 10, 11)] for p in (0, 1, 10, 11)]
    hk = held_karp(line_cost, round_trip=True)
    check(f"held_karp finds the known-optimal 4-stop round trip (cost {hk['cost']}, want 22)",
          _approx(hk["cost"], 22))
    bad_order_cost = tour_cost(line_cost, [0, 2, 1, 3], round_trip=True)  # home,B,A,C,home
    check(f"a crisscrossing order (cost {bad_order_cost}) is worse than the optimal one",
          hk["cost"] < bad_order_cost)

    # Case 2: 3-city known-optimal order, AND the round-trip-vs-open-tour difference. Home
    # is 100 from X and 500 from Y; X-Y is 50. Round trip visits the same triangle either
    # direction, so order doesn't matter (650 both ways) - but an OPEN tour should end at
    # the FARTHER city (Y) rather than double back to it, since the return leg is free to
    # skip: home->X->Y (100+50=150) beats home->Y->X (500+50=550).
    tri_cost = [[0, 100, 500], [100, 0, 50], [500, 50, 0]]
    rt = held_karp(tri_cost, round_trip=True)
    ot = held_karp(tri_cost, round_trip=False)
    check(f"round trip: order doesn't matter on a symmetric triangle (cost {rt['cost']}, want 650)",
          _approx(rt["cost"], 650))
    check(f"open tour finds the known-optimal order home->X->Y (cost {ot['cost']}, want 150)",
          ot["order"] == [0, 1, 2] and _approx(ot["cost"], 150))
    check("round trip and open tour disagree on cost for the same cities (650 vs 150)",
          rt["cost"] != ot["cost"])

    # Case 3: two_opt must never move home out of position 0, and must never make a tour
    # worse than nearest_neighbor's own starting point.
    nn = nearest_neighbor(line_cost)
    check("nearest_neighbor starts at home", nn[0] == 0)
    improved = two_opt(line_cost, nn, round_trip=True)
    check("two_opt keeps home fixed at position 0", improved[0] == 0)
    check("two_opt never makes the tour more expensive than its starting point",
          tour_cost(line_cost, improved, round_trip=True)
          <= tour_cost(line_cost, nn, round_trip=True))

    # Case 4: solve_tour dispatches to the exact solver at/under EXACT_LIMIT and reports it.
    small = solve_tour(line_cost, round_trip=True)
    check("solve_tour uses the exact method for 3 cities to visit", small["exact"] is True)
    check(f"solve_tour's exact result matches held_karp directly (got {small['cost']})",
          _approx(small["cost"], hk["cost"]))

    # Case 5: solve_tour falls back to the heuristic above EXACT_LIMIT, and still returns a
    # valid, complete tour (every node visited exactly once, home first) on a bigger
    # synthetic ring where the cheap tour is simply "go around the ring in order."
    ring_n = EXACT_LIMIT + 3
    ring_cost = [[abs(((i - j + ring_n // 2) % ring_n) - ring_n // 2) for j in range(ring_n)]
                for i in range(ring_n)]
    big = solve_tour(ring_cost, round_trip=True)
    check(f"solve_tour uses the heuristic above {EXACT_LIMIT} cities to visit",
          big["exact"] is False and "heuristic" in big["method"])
    check("the heuristic tour visits every stop exactly once, home first",
          big["order"][0] == 0 and sorted(big["order"]) == list(range(ring_n)))
    # held_karp itself has no size cap (only solve_tour's dispatcher does) - call it
    # directly on the same matrix to get the true optimum and grade the heuristic against it.
    ring_exact = held_karp(ring_cost, round_trip=True)
    check(f"the heuristic tour (cost {big['cost']}) never beats the true optimum "
          f"(cost {ring_exact['cost']})", big["cost"] >= ring_exact["cost"] - 1e-9)
    check(f"nearest-neighbor + 2-opt lands within 20% of the true optimum on this ring "
          f"(heuristic {big['cost']}, optimal {ring_exact['cost']})",
          big["cost"] <= ring_exact["cost"] * 1.2)

    # Case 6: price_leg() reuses trip.py's real $200-rule reasoning, not a copy of it - the
    # exact JFK->ASE pair server.py's own selftest already proves flips from direct to a
    # DEN split once the threshold drops from $200 to $50 (DEN+bus saves ~$65: under $200,
    # over $50). If this ever disagrees with server.py, the two engines have drifted apart.
    jfk, ase = geo.by_iata("JFK"), geo.by_iata("ASE")
    leg_hi = price_leg(jfk, ase, threshold=200)
    leg_lo = price_leg(jfk, ase, threshold=50)
    check(f"JFK->ASE at the $200 rule stays direct (got {leg_hi['name']!r})",
          leg_hi["is_split"] is False)
    check(f"the SAME leg at a $50 rule takes the DEN split (got {leg_lo['name']!r})",
          leg_lo["is_split"] is True and leg_lo["name"].startswith("DEN"))
    check("the split leg is cheaper than the direct one it replaced",
          leg_lo["cost"] < leg_hi["cost"])

    # Case 7: build_cost_matrix + plan_multicity end to end, offline, deterministic. Home
    # JFK, visit ASE and BOS - a low threshold should let the ASE legs price via the DEN
    # split (cheaper) while BOS (a well-served major hub with no useful gateway) stays
    # direct - and running the same inputs twice must produce the identical tour and cost.
    bos = geo.by_iata("BOS")
    matrix, legs = build_cost_matrix([jfk, ase, bos], threshold=50)
    check("build_cost_matrix prices every directed pair (3x3 minus the diagonal = 6 legs)",
          len(legs) == 6)
    check("ASE legs take the split at this threshold, BOS legs don't (no useful gateway)",
          legs[(0, 1)]["is_split"] is True and legs[(0, 2)]["is_split"] is False)

    res_a = plan_multicity("JFK", ["Aspen", "Boston"], threshold=50)
    res_b = plan_multicity("JFK", ["Aspen", "Boston"], threshold=50)
    check("plan_multicity resolves city names via go.resolve_airport",
          set(res_a["stops"]) == {"JFK", "ASE", "BOS"})
    check("plan_multicity is deterministic: identical inputs give an identical tour",
          res_a["stops"] == res_b["stops"] and _approx(res_a["total_cost"], res_b["total_cost"]))
    check("a round trip's itinerary has one leg per stop (closes back to home)",
          len(res_a["itinerary"]) == len(res_a["stops"]))
    open_res = plan_multicity("JFK", ["Aspen", "Boston"], threshold=50, round_trip=False)
    check("an open tour's itinerary has one fewer leg than a round trip's (no closing leg)",
          len(open_res["itinerary"]) == len(open_res["stops"]) - 1)

    # Case 8: travelers scale a leg's cost the same way trip.py's own group math does -
    # per-person modes (fly) x N, so 4 travelers on the same route costs strictly less than
    # a naive 4x of the solo total once any ground leg (priced per vehicle) is involved.
    solo = price_leg(jfk, ase, threshold=50, travelers=1)
    group = price_leg(jfk, ase, threshold=50, travelers=4)
    check(f"group of 4 costs less than 4x solo (solo {solo['cost']}, group {group['cost']}, "
          f"4x solo {4 * solo['cost']})", group["cost"] < 4 * solo["cost"])

    # Case 9: bad input is rejected with a clear error, not a crash or a silent wrong answer.
    try:
        plan_multicity("JFK", ["Nowhere Made Up Placename Zzyzx"])
        check("an unresolvable visit city raises ValueError", False)
    except ValueError as e:
        check("an unresolvable visit city raises ValueError", "no airport matches" in str(e))
    try:
        plan_multicity("JFK", ["Boston"])   # only 1 distinct city besides home
        check("fewer than 2 distinct visit cities raises ValueError", False)
    except ValueError:
        check("fewer than 2 distinct visit cities raises ValueError", True)

    # Case 10: the CLI's --threshold guard rejects a negative value with a clean exit code.
    rc_neg = main(["--home", "JFK", "--visit", "Aspen,Boston", "--threshold", "-5"])
    check("negative --threshold is rejected with a clean exit code", rc_neg == 2)

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (10 cases)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
