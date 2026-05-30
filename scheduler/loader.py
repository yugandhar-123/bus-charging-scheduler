"""Scenario JSON loader and validation."""

import json
from pathlib import Path
from typing import Any, Dict, List

from scheduler.models import (
    Battery,
    Bus,
    Route,
    Scenario,
    Segment,
    Station,
    Weights,
)


class ScenarioValidationError(ValueError):
    """Raised when a scenario file is present but not schedulable."""


def _fail(path: Path, message: str) -> None:
    raise ScenarioValidationError(f"{path.name}: {message}")


def _mapping(value: Any, path: Path, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, f"{label} must be an object")
    return value


def _list(value: Any, path: Path, label: str) -> List[Any]:
    if not isinstance(value, list):
        _fail(path, f"{label} must be a list")
    return value


def _required(data: Dict[str, Any], key: str, path: Path, label: str) -> Any:
    if key not in data:
        _fail(path, f"{label} is missing required field '{key}'")
    return data[key]


def _number(value: Any, path: Path, label: str, *, positive: bool = False) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _fail(path, f"{label} must be a number")
    value = float(value)
    if positive and value <= 0:
        _fail(path, f"{label} must be greater than zero")
    return value


def _string(value: Any, path: Path, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, f"{label} must be a non-empty string")
    return value


def _parse_departure(time_str: str, path: Path, label: str = "departure") -> float:
    """Convert 'HH:MM' to minutes since midnight."""
    if not isinstance(time_str, str):
        _fail(path, f"{label} must be a string in HH:MM format")
    try:
        h_text, m_text = time_str.split(":", 1)
        h = int(h_text)
        m = int(m_text)
    except ValueError:
        _fail(path, f"{label} must be in HH:MM format")
    if not 0 <= h <= 23 or not 0 <= m <= 59:
        _fail(path, f"{label} must be a valid 24-hour time")
    return h * 60 + m


def _format_minutes(minutes: float) -> str:
    """Convert minutes since midnight to 'HH:MM', handling times past midnight."""
    total_minutes = int(round(minutes))
    total = total_minutes % (24 * 60)
    h = total // 60
    m = total % 60
    days_offset = total_minutes // (24 * 60)
    if days_offset > 0:
        return f"{h:02d}:{m:02d} (+{days_offset}d)"
    return f"{h:02d}:{m:02d}"


def load_scenario(path: Path) -> Scenario:
    """Load one scenario JSON file and return a validated Scenario."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        _fail(path, f"invalid JSON at line {exc.lineno}: {exc.msg}")

    data = _mapping(data, path, "scenario")

    route_data = _mapping(_required(data, "route", path, "scenario"), path, "route")
    segment_rows = _list(_required(route_data, "segments", path, "route"), path, "route.segments")
    if not segment_rows:
        _fail(path, "route.segments must contain at least one segment")

    segments: List[Segment] = []
    for idx, raw_segment in enumerate(segment_rows):
        row = _mapping(raw_segment, path, f"route.segments[{idx}]")
        segments.append(
            Segment(
                from_stop=_string(_required(row, "from", path, f"route.segments[{idx}]"), path, f"route.segments[{idx}].from"),
                to_stop=_string(_required(row, "to", path, f"route.segments[{idx}]"), path, f"route.segments[{idx}].to"),
                distance_km=_number(_required(row, "distance_km", path, f"route.segments[{idx}]"), path, f"route.segments[{idx}].distance_km", positive=True),
            )
        )

    for prev, nxt in zip(segments, segments[1:]):
        if prev.to_stop != nxt.from_stop:
            _fail(path, f"route is disconnected between {prev.to_stop} and {nxt.from_stop}")

    route = Route(
        name=_string(_required(route_data, "name", path, "route"), path, "route.name"),
        origin=_string(_required(route_data, "origin", path, "route"), path, "route.origin"),
        destination=_string(_required(route_data, "destination", path, "route"), path, "route.destination"),
        speed_kmh=_number(_required(route_data, "speed_kmh", path, "route"), path, "route.speed_kmh", positive=True),
        segments=segments,
    )

    stops = route.stops()
    if route.origin != stops[0] or route.destination != stops[-1]:
        _fail(path, "route origin/destination must match the first and last segment stops")

    station_rows = _list(_required(data, "stations", path, "scenario"), path, "stations")
    stations: List[Station] = []
    seen_station_ids = set()
    for idx, raw_station in enumerate(station_rows):
        row = _mapping(raw_station, path, f"stations[{idx}]")
        station_id = _string(_required(row, "id", path, f"stations[{idx}]"), path, f"stations[{idx}].id")
        if station_id in seen_station_ids:
            _fail(path, f"duplicate station id '{station_id}'")
        if station_id not in stops[1:-1]:
            _fail(path, f"station '{station_id}' is not an intermediate route stop")
        seen_station_ids.add(station_id)
        stations.append(
            Station(
                id=station_id,
                name=_string(_required(row, "name", path, f"stations[{idx}]"), path, f"stations[{idx}].name"),
                chargers=int(_number(_required(row, "chargers", path, f"stations[{idx}]"), path, f"stations[{idx}].chargers", positive=True)),
                charge_time_min=_number(_required(row, "charge_time_min", path, f"stations[{idx}]"), path, f"stations[{idx}].charge_time_min", positive=True),
            )
        )

    battery_data = _mapping(_required(data, "battery", path, "scenario"), path, "battery")
    battery = Battery(
        full_range_km=_number(_required(battery_data, "full_range_km", path, "battery"), path, "battery.full_range_km", positive=True),
        charge_to=_string(battery_data.get("charge_to", "full"), path, "battery.charge_to"),
    )

    weights_data = _mapping(data.get("weights", {}), path, "weights")
    known_weight_keys = {"individual", "operator", "overall"}
    extra_weights = {
        key: _number(value, path, f"weights.{key}")
        for key, value in weights_data.items()
        if key not in known_weight_keys
    }
    weights = Weights(
        individual=_number(weights_data.get("individual", 1.0), path, "weights.individual"),
        operator=_number(weights_data.get("operator", 1.0), path, "weights.operator"),
        overall=_number(weights_data.get("overall", 1.0), path, "weights.overall"),
        extra=extra_weights,
    )

    bus_rows = _list(_required(data, "buses", path, "scenario"), path, "buses")
    buses: List[Bus] = []
    seen_bus_ids = set()
    for idx, raw_bus in enumerate(bus_rows):
        row = _mapping(raw_bus, path, f"buses[{idx}]")
        bus_id = _string(_required(row, "id", path, f"buses[{idx}]"), path, f"buses[{idx}].id")
        if bus_id in seen_bus_ids:
            _fail(path, f"duplicate bus id '{bus_id}'")
        seen_bus_ids.add(bus_id)

        direction = _string(_required(row, "direction", path, f"buses[{idx}]"), path, f"buses[{idx}].direction")
        if direction not in {"BK", "KB"}:
            _fail(path, f"buses[{idx}].direction must be either 'BK' or 'KB'")

        buses.append(
            Bus(
                id=bus_id,
                operator=_string(_required(row, "operator", path, f"buses[{idx}]"), path, f"buses[{idx}].operator"),
                direction=direction,
                departure_min=_parse_departure(_required(row, "departure", path, f"buses[{idx}]"), path, f"buses[{idx}].departure"),
            )
        )

    if not buses:
        _fail(path, "buses must contain at least one bus")

    return Scenario(
        id=_string(_required(data, "id", path, "scenario"), path, "id"),
        name=_string(_required(data, "name", path, "scenario"), path, "name"),
        description=_string(data.get("description", "No description provided."), path, "description"),
        route=route,
        stations=stations,
        battery=battery,
        weights=weights,
        buses=buses,
    )


def load_all_scenarios(scenarios_dir: Path) -> List[Scenario]:
    """Load all scenario_*.json files from a directory, sorted by filename."""
    paths = sorted(scenarios_dir.glob("scenario_*.json"))
    return [load_scenario(p) for p in paths]


def format_minutes(minutes: float) -> str:
    """Public-facing time formatter for display layers."""
    return _format_minutes(minutes)
