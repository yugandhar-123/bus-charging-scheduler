"""
models.py — Pure data classes for the Bus Charging Scheduler.

Design philosophy: Everything is data. The scheduler logic is separate from
the shape of the data. Adding new fields here (priority flag, time-of-day costs,
driver shift IDs) never touches the engine.
"""

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class Segment:
    """One leg of a route (e.g. Bengaluru → A)."""
    from_stop: str
    to_stop: str
    distance_km: float

@dataclass
class Route:
    """
    A named route with ordered stops and a travel speed.
    Adding a new route or changing distances requires only a new data file —
    no code changes.
    """
    name: str
    origin: str
    destination: str
    speed_kmh: float
    segments: List[Segment]

    def travel_time_min(self, distance_km: float) -> float:
        return (distance_km / self.speed_kmh) * 60

    def stops(self) -> List[str]:
        """Ordered list of all stops on the route."""
        result = [self.segments[0].from_stop]
        for seg in self.segments:
            result.append(seg.to_stop)
        return result

    def distance_between(self, from_stop: str, to_stop: str) -> float:
        """
        Total distance between any two stops (must be in route order).
        Used to verify range constraints.
        """
        stops = self.stops()
        i = stops.index(from_stop)
        j = stops.index(to_stop)
        return sum(self.segments[k].distance_km for k in range(i, j))

    def stations_between(self, from_stop: str, to_stop: str) -> List[str]:
        """Intermediate stops (charging stations) between two endpoints."""
        stops = self.stops()
        i = stops.index(from_stop)
        j = stops.index(to_stop)
        return stops[i+1:j]


@dataclass
class Station:
    """
    A charging station along the route.
    chargers > 1 is already supported in data — the engine respects it.
    charge_time_min is per-station, so future variable-speed chargers need
    only a data change.
    """
    id: str
    name: str
    chargers: int
    charge_time_min: float


@dataclass
class Battery:
    """Battery configuration. Decoupled so future partial charging is easy."""
    full_range_km: float
    charge_to: str = "full"  # future: "partial", "X%"


@dataclass
class Weights:
    """
    Optimisation weights. One place, obvious names.
    Changing a weight = changing one value in the JSON file.
    New dimensions (e.g. electricity_cost) = new field here + new rule in engine.
    """
    individual: float = 1.0  # penalise long waits for a single bus
    operator: float = 1.0    # penalise uneven treatment across an operator's fleet
    overall: float = 1.0     # penalise high total network time
    extra: Dict[str, float] = field(default_factory=dict)

    def value_for(self, key: str) -> float:
        """Return a configured rule weight, including future JSON-defined keys."""
        if hasattr(self, key):
            return float(getattr(self, key))
        return float(self.extra.get(key, 1.0))


@dataclass
class Bus:
    """
    A bus in the scenario. Direction 'BK' = Bengaluru→Kochi, 'KB' = reverse.
    Adding new fields (priority, bus_type, max_passengers) = add here + scenario JSON.
    """
    id: str
    operator: str
    direction: str          # "BK" or "KB"
    departure_min: float    # minutes since midnight

    @property
    def origin(self) -> str:
        return "Bengaluru" if self.direction == "BK" else "Kochi"

    @property
    def destination(self) -> str:
        return "Kochi" if self.direction == "BK" else "Bengaluru"


@dataclass
class ChargeEvent:
    """
    One charging stop for a bus.
    Captures: where, when it arrived, when charging started, when it departed.
    wait_min = charge_start_min - arrival_min (0 if charger was free).
    """
    station_id: str
    arrival_min: float
    charge_start_min: float
    charge_end_min: float

    @property
    def wait_min(self) -> float:
        return self.charge_start_min - self.arrival_min


@dataclass
class BusResult:
    """
    Full computed timeline for one bus after scheduling.
    This is the output object — display code reads from here.
    """
    bus: Bus
    charge_events: List[ChargeEvent]
    departure_min: float
    arrival_min: float

    @property
    def total_wait_min(self) -> float:
        return sum(e.wait_min for e in self.charge_events)

    @property
    def total_travel_time_min(self) -> float:
        return self.arrival_min - self.departure_min


@dataclass
class Scenario:
    """
    Everything needed to run the scheduler.
    One scenario = one self-contained world.
    """
    id: str
    name: str
    description: str
    route: Route
    stations: List[Station]
    battery: Battery
    weights: Weights
    buses: List[Bus]

    def station_map(self) -> Dict[str, Station]:
        return {s.id: s for s in self.stations}
