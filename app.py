"""
app.py — Bus Charging Scheduler · Streamlit UI

Layout:
  ┌─────────────────────────────────────────────────────┐
  │  🚌 Bus Charging Scheduler                          │
  │  [Scenario Dropdown]                                │
  ├─────────────────────────────────────────────────────┤
  │  Scenario Overview  (description + weights)         │
  ├─────────────────────────────────────────────────────┤
  │  Input: Bus Roster  (raw scenario data as table)    │
  ├─────────────────────────────────────────────────────┤
  │  Per-Bus Timetable  (full charging timeline)        │
  ├─────────────────────────────────────────────────────┤
  │  Per-Station View   (charge order at A / B / C / D) │
  └─────────────────────────────────────────────────────┘
"""

import streamlit as st
import pandas as pd
from pathlib import Path

from scheduler.loader import ScenarioValidationError, load_all_scenarios, format_minutes
from scheduler.engine import schedule

# ──────────────────────────────────────────────────────────────────────────────
# Page config
# ──────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Bus Charging Scheduler",
    page_icon="🚌",
    layout="wide",
)

# ──────────────────────────────────────────────────────────────────────────────
# Load scenarios (cached so we don't re-read disk on every interaction)
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_data
def load_scenarios():
    base = Path(__file__).parent / "scenarios"
    return load_all_scenarios(base)


try:
    scenarios = load_scenarios()
except (OSError, ScenarioValidationError, ValueError) as exc:
    st.error(f"Could not load scenarios: {exc}")
    st.stop()

if not scenarios:
    st.error("No scenario files found in the scenarios directory.")
    st.stop()

scenario_names = [s.name for s in scenarios]

# ──────────────────────────────────────────────────────────────────────────────
# Header + scenario selector
# ──────────────────────────────────────────────────────────────────────────────

st.title("🚌 Bus Charging Scheduler")
st.caption("Bengaluru → A → B → C → D → Kochi · 540 km · 240 km battery range")

selected_name = st.selectbox(
    "Select a scenario",
    scenario_names,
    index=0,
    help="Each scenario represents a different departure pattern across 20 buses.",
)

scenario = next(s for s in scenarios if s.name == selected_name)

# ──────────────────────────────────────────────────────────────────────────────
# Run the scheduler
# ──────────────────────────────────────────────────────────────────────────────

try:
    with st.spinner("Running scheduler…"):
        results, violations = schedule(scenario)
except ValueError as exc:
    st.error(f"Scheduler could not produce a valid plan: {exc}")
    st.stop()

# ──────────────────────────────────────────────────────────────────────────────
# Section 1 — Scenario overview
# ──────────────────────────────────────────────────────────────────────────────

st.divider()
st.subheader("📋 Scenario Overview")

col1, col2, col3 = st.columns(3)

with col1:
    st.markdown("**Description**")
    st.info(scenario.description)

with col2:
    st.markdown("**Route**")
    stops = scenario.route.stops()
    st.info(" → ".join(stops))

    st.markdown("**Speed**")
    st.info(f"{scenario.route.speed_kmh:.0f} km/h")

with col3:
    st.markdown("**Optimisation Weights**")
    w = scenario.weights
    st.info(
        f"Individual: **{w.individual}**  \n"
        f"Operator:   **{w.operator}**  \n"
        f"Overall:    **{w.overall}**"
    )

# Hard rule violations (should be empty for a valid schedule)
if violations:
    st.error("⚠️ Hard constraint violations detected:")
    for v in violations:
        st.error(v)
else:
    st.success("✅ All hard constraints satisfied")

# ──────────────────────────────────────────────────────────────────────────────
# Section 2 — Input: raw scenario data
# ──────────────────────────────────────────────────────────────────────────────

st.divider()
st.subheader("📥 Input: Bus Roster")
st.caption("Raw departure schedule fed into the scheduler")

bus_rows = []
for bus in scenario.buses:
    direction_label = "Bengaluru → Kochi" if bus.direction == "BK" else "Kochi → Bengaluru"
    bus_rows.append({
        "Bus ID":    bus.id,
        "Operator":  bus.operator.title(),
        "Direction": direction_label,
        "Departure": format_minutes(bus.departure_min),
    })

df_input = pd.DataFrame(bus_rows)
st.dataframe(df_input, width="stretch", hide_index=True)

# ──────────────────────────────────────────────────────────────────────────────
# Section 3 — Per-bus timetable
# ──────────────────────────────────────────────────────────────────────────────

st.divider()
st.subheader("🗓️ Per-Bus Timetable")
st.caption("Full timeline for each bus: where it charges, when, how long it waits")

timetable_rows = []
for r in sorted(results, key=lambda x: (x.bus.direction, x.bus.id)):
    direction_label = "BK →" if r.bus.direction == "BK" else "KB ←"
    stations_used = " → ".join(e.station_id for e in r.charge_events) or "—"

    charge_details = []
    for e in r.charge_events:
        wait_str = f" (wait {e.wait_min:.0f} min)" if e.wait_min > 0 else ""
        charge_details.append(
            f"{e.station_id}: arrive {format_minutes(e.arrival_min)}, "
            f"charge {format_minutes(e.charge_start_min)}–{format_minutes(e.charge_end_min)}"
            f"{wait_str}"
        )
    charge_summary = " | ".join(charge_details) if charge_details else "—"

    timetable_rows.append({
        "Bus ID":        r.bus.id,
        "Dir":           direction_label,
        "Operator":      r.bus.operator.title(),
        "Departs":       format_minutes(r.departure_min),
        "Stations Used": stations_used,
        "Charge Events": charge_summary,
        "Total Wait":    f"{r.total_wait_min:.0f} min",
        "Arrives":       format_minutes(r.arrival_min),
        "Trip Time":     f"{r.total_travel_time_min:.0f} min",
    })

df_timetable = pd.DataFrame(timetable_rows)

# Highlight rows with significant wait times
def _highlight_wait(row):
    wait = int(row["Total Wait"].replace(" min", ""))
    if wait >= 50:
        return ["background-color: #ffd6d6"] * len(row)
    elif wait >= 25:
        return ["background-color: #fff3cd"] * len(row)
    return [""] * len(row)

styled = df_timetable.style.apply(_highlight_wait, axis=1)
st.dataframe(styled, width="stretch", hide_index=True)

st.caption("🔴 Red = wait ≥ 50 min  |  🟡 Yellow = wait ≥ 25 min  |  White = wait < 25 min")

# ──────────────────────────────────────────────────────────────────────────────
# Section 4 — Per-station view
# ──────────────────────────────────────────────────────────────────────────────

st.divider()
st.subheader("🔌 Per-Station Charging Order")
st.caption("The sequence in which buses charged at each station")

# Build station columns
station_cols = st.columns(len(scenario.stations))

for col, station in zip(station_cols, scenario.stations):
    with col:
        st.markdown(f"**Station {station.id}**")
        st.caption(f"{station.chargers} charger{'s' if station.chargers > 1 else ''} · {station.charge_time_min:.0f} min/charge")

        # Collect all charge events at this station, sorted by start time
        events_here = []
        for r in results:
            for e in r.charge_events:
                if e.station_id == station.id:
                    events_here.append((e.charge_start_min, r.bus.id, r.bus.operator, e.wait_min, e.charge_end_min))

        events_here.sort(key=lambda x: x[0])

        if not events_here:
            st.info("No buses charged here")
            continue

        station_rows = []
        for rank, (start, bus_id, operator, wait, end) in enumerate(events_here, 1):
            station_rows.append({
                "#":        rank,
                "Bus":      bus_id,
                "Op":       operator.title(),
                "Start":    format_minutes(start),
                "End":      format_minutes(end),
                "Wait":     f"{wait:.0f}m",
            })

        df_station = pd.DataFrame(station_rows)
        st.dataframe(df_station, width="stretch", hide_index=True)

# ──────────────────────────────────────────────────────────────────────────────
# Section 5 — Summary stats
# ──────────────────────────────────────────────────────────────────────────────

st.divider()
st.subheader("📊 Summary")

total_wait = sum(r.total_wait_min for r in results)
max_wait = max(r.total_wait_min for r in results)
avg_wait = total_wait / len(results) if results else 0
max_trip = max(r.total_travel_time_min for r in results)
min_trip = min(r.total_travel_time_min for r in results)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Buses Scheduled", len(results))
c2.metric("Total Network Wait", f"{total_wait:.0f} min")
c3.metric("Max Single-Bus Wait", f"{max_wait:.0f} min")
c4.metric("Avg Wait Per Bus", f"{avg_wait:.1f} min")
c5.metric("Trip Time Range", f"{min_trip:.0f}–{max_trip:.0f} min")

# Operator breakdown
st.markdown("**Wait time by operator**")
from collections import defaultdict
op_waits = defaultdict(list)
for r in results:
    op_waits[r.bus.operator].append(r.total_wait_min)

op_rows = []
for op, waits in sorted(op_waits.items()):
    op_rows.append({
        "Operator": op.title(),
        "Buses":    len(waits),
        "Total Wait (min)": f"{sum(waits):.0f}",
        "Avg Wait (min)":   f"{sum(waits)/len(waits):.1f}",
        "Max Wait (min)":   f"{max(waits):.0f}",
    })

st.dataframe(pd.DataFrame(op_rows), width="stretch", hide_index=True)
