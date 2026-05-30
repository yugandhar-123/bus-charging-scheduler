"""
rules.py — Pluggable rule definitions for the charging scheduler.

DESIGN PRINCIPLE
================
The scheduler engine doesn't know what rules exist. It asks each rule for a
cost contribution and checks hard constraints. Adding a new rule = writing a
new function and registering it. The engine never changes.

Two kinds of rules:
  1. SoftRule  — contributes a weighted score; lower is better.
  2. HardRule  — returns True (pass) or False (violation); violations are never
                 allowed regardless of score.

To add a new rule (e.g. time-of-day electricity cost):
    @soft_rule("electricity_cost", weight_key="electricity")
    def electricity_cost_rule(bus_result, scenario):
        ...compute cost based on charge times...
        return cost_value

That's it. The engine picks it up automatically.
"""

from typing import Callable, Dict, List, Tuple
from scheduler.models import BusResult, Scenario


# ──────────────────────────────────────────────────────────────────────────────
# Rule registries
# ──────────────────────────────────────────────────────────────────────────────

# Each soft rule: (name, weight_key, fn(results, scenario) -> float)
_SOFT_RULES: List[Tuple[str, str, Callable]] = []

# Each hard rule: (name, fn(results, scenario) -> bool)  True = valid
_HARD_RULES: List[Tuple[str, Callable]] = []


def soft_rule(name: str, weight_key: str):
    """Decorator to register a soft scoring rule."""
    def decorator(fn: Callable):
        _SOFT_RULES.append((name, weight_key, fn))
        return fn
    return decorator


def hard_rule(name: str):
    """Decorator to register a hard constraint rule."""
    def decorator(fn: Callable):
        _HARD_RULES.append((name, fn))
        return fn
    return decorator


def get_soft_rules():
    return list(_SOFT_RULES)


def get_hard_rules():
    return list(_HARD_RULES)


# ──────────────────────────────────────────────────────────────────────────────
# Hard rules — must never be violated
# ──────────────────────────────────────────────────────────────────────────────

@hard_rule("range_constraint")
def range_constraint(results: List[BusResult], scenario: Scenario) -> Tuple[bool, str]:
    """
    A bus must never travel more than battery.full_range_km between consecutive
    charge points (or from origin to first charge, or from last charge to dest).
    """
    route = scenario.route
    battery = scenario.battery
    all_stops = route.stops()

    def _dist(a, b):
        i, j = all_stops.index(a), all_stops.index(b)
        lo, hi = min(i, j), max(i, j)
        return sum(route.segments[k].distance_km for k in range(lo, hi))

    for r in results:
        stops_visited = [r.bus.origin] + [e.station_id for e in r.charge_events] + [r.bus.destination]
        for i in range(len(stops_visited) - 1):
            dist = _dist(stops_visited[i], stops_visited[i + 1])
            if dist > battery.full_range_km + 0.01:  # small epsilon for float safety
                return False, (
                    f"{r.bus.id}: segment {stops_visited[i]}→{stops_visited[i+1]} "
                    f"is {dist:.0f} km but max range is {battery.full_range_km:.0f} km"
                )
    return True, ""


@hard_rule("station_order")
def station_order(results: List[BusResult], scenario: Scenario) -> Tuple[bool, str]:
    """Buses must visit stations in route order — no backtracking."""
    route = scenario.route
    for r in results:
        stops = route.stops()
        origin_idx = stops.index(r.bus.origin)
        dest_idx = stops.index(r.bus.destination)
        going_forward = dest_idx > origin_idx
        prev_idx = origin_idx
        for event in r.charge_events:
            station_idx = stops.index(event.station_id)
            if going_forward and station_idx <= prev_idx:
                return False, f"{r.bus.id}: visited {event.station_id} out of order"
            if not going_forward and station_idx >= prev_idx:
                return False, f"{r.bus.id}: visited {event.station_id} out of order"
            prev_idx = station_idx
    return True, ""


@hard_rule("charger_capacity")
def charger_capacity(results: List[BusResult], scenario: Scenario) -> Tuple[bool, str]:
    """
    At any instant, a station must not have more buses charging than it has chargers.
    Supports stations with chargers > 1 without code changes.
    """
    for station in scenario.stations:
        events_here: List[Tuple[float, int, str]] = []
        for r in results:
            for e in r.charge_events:
                if e.station_id == station.id:
                    events_here.append((e.charge_start_min, 1, r.bus.id))
                    events_here.append((e.charge_end_min, -1, r.bus.id))

        active = 0
        # End events sort before start events at the same minute, so back-to-back
        # charging sessions are allowed.
        for _, delta, _ in sorted(events_here, key=lambda item: (item[0], item[1])):
            active += delta
            if active > station.chargers:
                return False, (
                    f"Station {station.id}: more than {station.chargers} "
                    f"bus(es) charging simultaneously"
                )
    return True, ""


# ──────────────────────────────────────────────────────────────────────────────
# Soft rules — contribute weighted cost; lower total cost is better
# ──────────────────────────────────────────────────────────────────────────────

@soft_rule("individual_wait", weight_key="individual")
def individual_wait_cost(results: List[BusResult], scenario: Scenario) -> float:
    """
    Penalises long waits for individual buses.
    Cost = max wait experienced by any single bus.
    Using max (not sum) prevents sacrificing one bus for the group.
    """
    if not results:
        return 0.0
    return max(r.total_wait_min for r in results)


@soft_rule("operator_variance", weight_key="operator")
def operator_variance_cost(results: List[BusResult], scenario: Scenario) -> float:
    """
    Penalises unequal treatment across buses of the same operator.
    Cost = sum of per-operator wait-time variance.
    When operator weight is high, the scheduler tries to equalise wait times
    within each fleet — visible in Scenario 4.
    """
    from collections import defaultdict
    operator_waits: Dict[str, List[float]] = defaultdict(list)
    for r in results:
        operator_waits[r.bus.operator].append(r.total_wait_min)

    total_variance = 0.0
    for waits in operator_waits.values():
        if len(waits) < 2:
            continue
        mean = sum(waits) / len(waits)
        variance = sum((w - mean) ** 2 for w in waits) / len(waits)
        total_variance += variance
    return total_variance


@soft_rule("operator_station_balance", weight_key="operator")
def operator_station_balance_cost(results: List[BusResult], scenario: Scenario) -> float:
    """
    Penalises concentrating one operator's fleet on the same stations.

    When one operator dominates a departure wave, spreading that fleet across
    feasible charging stations lowers the chance that its own buses stack up
    behind each other later in the route.
    """
    station_counts: Dict[Tuple[str, str], int] = {}
    for r in results:
        for event in r.charge_events:
            key = (r.bus.operator, event.station_id)
            station_counts[key] = station_counts.get(key, 0) + 1

    return sum(count * count for count in station_counts.values())


@soft_rule("overall_network_time", weight_key="overall")
def overall_network_time_cost(results: List[BusResult], scenario: Scenario) -> float:
    """
    Penalises high total accumulated delay across the whole network.
    Cost = sum of total wait times for all buses.
    """
    return sum(r.total_wait_min for r in results)


# ──────────────────────────────────────────────────────────────────────────────
# Score aggregator (used by the engine)
# ──────────────────────────────────────────────────────────────────────────────

def compute_total_cost(results: List[BusResult], scenario: Scenario) -> float:
    """
    Weighted sum of all soft rule costs.
    Weight values come from scenario.weights — one value, one place.
    """
    total = 0.0
    for name, weight_key, fn in get_soft_rules():
        w = scenario.weights.value_for(weight_key)
        cost = fn(results, scenario)
        total += w * cost
    return total


def validate_hard_rules(results: List[BusResult], scenario: Scenario) -> Tuple[bool, List[str]]:
    """Check all hard rules. Returns (is_valid, list_of_violations)."""
    violations = []
    for name, fn in get_hard_rules():
        ok, msg = fn(results, scenario)
        if not ok:
            violations.append(f"[{name}] {msg}")
    return len(violations) == 0, violations
