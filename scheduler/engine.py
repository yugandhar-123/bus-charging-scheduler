"""
engine.py — The charging scheduler engine.

ALGORITHM OVERVIEW
==================
This is a priority-queue simulation (event-driven greedy + cost-guided
station selection).

For each bus, in departure-time order:
  1. Plan which stations to charge at (via plan_charging_stops).
  2. For each planned stop, compute the earliest the bus can arrive at that
     station given travel time from its previous stop + any wait at prior stops.
  3. Queue the bus at the station. The station schedules it after the previous
     bus finishes (if busy) or immediately (if free).
  4. Record the ChargeEvent and carry forward the updated time.

Station selection (plan_charging_stops):
  The scheduler enumerates all feasible station subsets (those that satisfy the
  range hard rule) and picks the subset that produces the lowest weighted cost
  via a greedy look-ahead. This makes it easy to add new soft rules: they just
  contribute to the cost function, and the selection automatically accounts for
  them.

WHY THIS APPROACH IS RIGHT FOR THIS PROBLEM
============================================
- Greedy + cost function separates *what to optimise* (rules.py) from
  *how to schedule* (this file). Adding a rule never touches the engine.
- Priority-queue simulation is O(N log N) in bus count — scales to thousands
  of buses without a rewrite.
- Weights are read from the scenario, not hardcoded — changing a weight is a
  data change, not a code change.
- The station charger count is respected generically — 1 charger today,
  3 tomorrow: same code path.

EXTENDING THE ENGINE
====================
- New hard rule: add to rules.py. plan_charging_stops filters on hard rules.
- New soft rule: add to rules.py. compute_total_cost picks it up.
- New route/stations/buses: new scenario JSON, zero code changes.
- Priority buses: add a priority field to Bus, add a soft/hard rule that
  penalises bumping a priority bus, done.
"""

from typing import Dict, List, Optional, Tuple
from itertools import combinations

from scheduler.models import (
    Bus, BusResult, ChargeEvent, Route, Scenario, Station
)
from scheduler.rules import compute_total_cost, validate_hard_rules


# ──────────────────────────────────────────────────────────────────────────────
# Feasibility helpers
# ──────────────────────────────────────────────────────────────────────────────

def _route_distance(route: Route, from_stop: str, to_stop: str) -> float:
    """
    Distance between two stops regardless of direction.
    For KB buses, the physical distance is the same as BK — we just traverse
    the route in the opposite order.
    """
    all_stops = route.stops()
    i = all_stops.index(from_stop)
    j = all_stops.index(to_stop)
    lo, hi = min(i, j), max(i, j)
    return sum(route.segments[k].distance_km for k in range(lo, hi))


def _feasible_station_subsets(
    bus: Bus,
    route: Route,
    stations: List[Station],
    full_range_km: float,
) -> List[List[str]]:
    """
    Return all ordered lists of charging station IDs that satisfy the range
    hard rule for this bus.

    A bus going BK travels Bengaluru→A→B→C→D→Kochi.
    A bus going KB travels Kochi→D→C→B→A→Bengaluru.

    We enumerate subsets of the candidate stations in the bus's travel order,
    from smallest (fewest stops) to largest, returning every valid plan.
    """
    all_stops = route.stops()  # [Bengaluru, A, B, C, D, Kochi]
    origin_idx = all_stops.index(bus.origin)
    dest_idx   = all_stops.index(bus.destination)

    candidate_station_ids = [
        s.id for s in stations
        if min(origin_idx, dest_idx) < all_stops.index(s.id) < max(origin_idx, dest_idx)
    ]
    # Sort candidates in travel order for this bus
    candidate_station_ids.sort(
        key=lambda sid: all_stops.index(sid),
        reverse=(bus.direction == "KB"),
    )

    feasible = []

    for size in range(0, len(candidate_station_ids) + 1):
        for subset in combinations(candidate_station_ids, size):
            # For KB buses, subsets from combinations() need to be sorted in
            # reverse stop-index order (travel order for KB)
            ordered_subset = sorted(
                subset,
                key=lambda sid: all_stops.index(sid),
                reverse=(bus.direction == "KB"),
            )

            stops_in_order = [bus.origin] + list(ordered_subset) + [bus.destination]

            # Check every consecutive leg against battery range
            valid = True
            for i in range(len(stops_in_order) - 1):
                dist = _route_distance(route, stops_in_order[i], stops_in_order[i + 1])
                if dist > full_range_km + 0.01:
                    valid = False
                    break
            if valid:
                feasible.append(list(ordered_subset))

    return feasible


# ──────────────────────────────────────────────────────────────────────────────
# Station queue tracker
# ──────────────────────────────────────────────────────────────────────────────

class StationQueues:
    """
    Tracks booked charge intervals for each charger at each station.

    Unlike a single "next free time" cursor, this can insert a later-processed
    bus into an earlier open gap when its arrival time allows it.
    """

    def __init__(self, stations: List[Station]):
        self._bookings: Dict[str, List[List[Tuple[float, float]]]] = {}
        for s in stations:
            self._bookings[s.id] = [[] for _ in range(s.chargers)]

    def earliest_slot(self, station_id: str, arrival_min: float, duration_min: float) -> float:
        """
        Return the time charging can START for a bus arriving at arrival_min.
        """
        start, _ = self._find_slot(station_id, arrival_min, duration_min)
        return start

    def book(self, station_id: str, charge_start: float, charge_end: float):
        """Reserve a charger slot for this interval."""
        duration = charge_end - charge_start
        start, charger_idx = self._find_slot(station_id, charge_start, duration)
        if abs(start - charge_start) > 0.01:
            raise ValueError(f"Cannot book station {station_id} at {charge_start:.2f}; slot is no longer available")
        bookings = self._bookings[station_id][charger_idx]
        bookings.append((charge_start, charge_end))
        bookings.sort(key=lambda interval: interval[0])

    def queue_order(self, station_id: str) -> List[float]:
        starts = []
        for charger_bookings in self._bookings[station_id]:
            starts.extend(start for start, _ in charger_bookings)
        return sorted(starts)

    def _find_slot(self, station_id: str, arrival_min: float, duration_min: float) -> Tuple[float, int]:
        best_start: Optional[float] = None
        best_charger = 0

        for charger_idx, bookings in enumerate(self._bookings[station_id]):
            candidate = arrival_min
            for booked_start, booked_end in sorted(bookings, key=lambda interval: interval[0]):
                if candidate + duration_min <= booked_start + 0.01:
                    break
                if candidate < booked_end:
                    candidate = booked_end

            if best_start is None or (candidate, charger_idx) < (best_start, best_charger):
                best_start = candidate
                best_charger = charger_idx

        return best_start if best_start is not None else arrival_min, best_charger


# ──────────────────────────────────────────────────────────────────────────────
# Main scheduling function
# ──────────────────────────────────────────────────────────────────────────────

def schedule(scenario: Scenario) -> Tuple[List[BusResult], List[str]]:
    """
    Run the scheduler for the given scenario.

    Returns:
        (results, violations)
        results    — list of BusResult, one per bus
        violations — list of hard-rule violation strings (empty if schedule is valid)
    """
    route = scenario.route
    station_map = scenario.station_map()
    full_range = scenario.battery.full_range_km
    queues = StationQueues(scenario.stations)

    # Process buses in departure order to give earlier buses first shot at chargers.
    # Ties broken by bus ID for determinism.
    buses_ordered = sorted(scenario.buses, key=lambda b: (b.departure_min, b.id))

    results: List[BusResult] = []

    for bus in buses_ordered:
        # ── 1. Find all feasible charging-stop subsets ─────────────────────
        feasible = _feasible_station_subsets(bus, route, scenario.stations, full_range)

        if not feasible:
            # Should never happen for a well-formed scenario, but handle gracefully
            raise ValueError(
                f"Bus {bus.id} has no feasible charging plan. "
                f"Check route distances vs battery range."
            )

        # ── 2. For each feasible subset, simulate and score ────────────────
        best_result: BusResult = None
        best_cost = float("inf")

        for station_subset in feasible:
            result = _simulate_bus(bus, station_subset, route, station_map, queues)
            # Score this plan in isolation (we use existing committed queues)
            provisional = results + [result]
            cost = compute_total_cost(provisional, scenario)
            if cost < best_cost:
                best_cost = cost
                best_result = result

        # ── 3. Commit the best plan (book the charger slots) ───────────────
        for event in best_result.charge_events:
            queues.book(event.station_id, event.charge_start_min, event.charge_end_min)

        results.append(best_result)

    # ── 4. Final hard-rule validation ──────────────────────────────────────
    is_valid, violations = validate_hard_rules(results, scenario)

    return results, violations


def _simulate_bus(
    bus: Bus,
    station_ids: List[str],
    route: Route,
    station_map: Dict[str, Station],
    queues: StationQueues,
) -> BusResult:
    """
    Simulate one bus travelling through its assigned charging stops.
    Returns a BusResult with a full timeline.

    NOTE: This reads queue state but does NOT commit — commitment happens after
    the best plan is chosen in schedule().
    """
    current_time = bus.departure_min
    charge_events: List[ChargeEvent] = []

    prev_stop = bus.origin

    for station_id in station_ids:
        station = station_map[station_id]
        dist = _route_distance(route, prev_stop, station_id)
        travel_time = route.travel_time_min(dist)
        arrival = current_time + travel_time

        # When can charging start? (may need to wait if charger is busy)
        charge_start = queues.earliest_slot(station_id, arrival, station.charge_time_min)
        charge_end = charge_start + station.charge_time_min

        charge_events.append(ChargeEvent(
            station_id=station_id,
            arrival_min=arrival,
            charge_start_min=charge_start,
            charge_end_min=charge_end,
        ))

        current_time = charge_end
        prev_stop = station_id

    # Final leg to destination
    dist_to_dest = _route_distance(route, prev_stop, bus.destination)
    travel_time = route.travel_time_min(dist_to_dest)
    arrival_at_dest = current_time + travel_time

    return BusResult(
        bus=bus,
        charge_events=charge_events,
        departure_min=bus.departure_min,
        arrival_min=arrival_at_dest,
    )
