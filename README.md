# 🚌 Bus Charging Scheduler

A production-style scheduling system for electric buses on the Bengaluru–Kochi corridor, built with Python + Streamlit.

**Live app:** _https://bus-charging-scheduler-cpwqefu3nbmoncda4x8hwt.streamlit.app/_

---

## Problem

Electric buses run a 540 km fixed route with 4 intermediate charging stations (A, B, C, D). Each bus starts with a 240 km range and must charge at least twice along the way. Stations have 1 charger each, so contention is inevitable. The scheduler decides:

1. Which stations each bus uses
2. The order in which buses access each charger
3. How to balance individual fairness, operator equity, and overall network efficiency — with tunable weights

---

## Features

- Loads any of 5 pre-built scenarios from JSON data files
- Displays raw input (bus roster) and full scheduler output side by side
- Per-bus timetable: arrival, charge start/end, wait time, final arrival
- Per-station charging order for all 4 stations
- Summary metrics: total/avg/max wait, operator breakdown
- Hard constraint validation: range, station order, charger capacity
- Tunable optimisation weights via scenario JSON — no code changes needed

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/bus-charging-scheduler
cd bus-charging-scheduler

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run
streamlit run app.py
```

Open `http://localhost:8501` in your browser.

Run the verification suite with:

```bash
python -m unittest discover -s tests
```

---

## How to change a weight

Open the scenario JSON file (e.g. `scenarios/scenario_4.json`) and edit the `weights` block:

```json
"weights": {
  "individual": 1.0,
  "operator":   2.0,
  "overall":    1.0
}
```

That's it. Restart the app (or just re-select the scenario — the app recalculates on every selection).

---

## How to add a new rule

**Example: penalise charging during peak electricity hours (22:00–06:00)**

1. Open `scheduler/rules.py`
2. Add a weight key to `Weights` in `models.py`:
   ```python
   electricity: float = 1.0
   ```
3. Add the field to the scenario JSON `weights` block:
   ```json
   "electricity": 1.5
   ```
4. Write the rule in `rules.py`:
   ```python
   @soft_rule("electricity_cost", weight_key="electricity")
   def electricity_cost_rule(results, scenario):
       PEAK_START, PEAK_END = 22 * 60, 30 * 60  # 22:00–06:00 next day
       cost = 0.0
       for r in results:
           for e in r.charge_events:
               if e.charge_start_min >= PEAK_START or e.charge_start_min <= PEAK_END:
                   cost += 1.0  # or a real cost multiplier
       return cost
   ```
5. Done. The engine picks it up automatically through the `get_soft_rules()` registry, and `scenario.weights.value_for("electricity")` reads the JSON weight.

No engine changes. No scenario-loader changes.

---

## How to add a new scenario

1. Copy `scenarios/scenario_1.json`
2. Change `id`, `name`, `description`, and the `buses` array
3. Optionally adjust `weights`, `stations` (add chargers), or `route` (change distances)
4. Save as `scenarios/scenario_6.json`
5. Restart the app — it appears in the dropdown automatically

---

## Folder Structure

```
bus-charging-scheduler/
├── app.py                      ← Streamlit UI (single entry point)
├── requirements.txt            ← Dependencies for Streamlit Cloud
├── README.md
├── ARCHITECTURE.md
│
├── scheduler/                  ← Core domain logic
│   ├── __init__.py
│   ├── models.py               ← Data classes (Bus, Station, Route, …)
│   ├── loader.py               ← JSON → typed objects
│   ├── rules.py                ← Pluggable soft/hard rules
│   └── engine.py               ← Scheduling algorithm
│
└── scenarios/                  ← Self-contained scenario data files
    ├── scenario_1.json         ← Even spacing
    ├── scenario_2.json         ← Bunched start
    ├── scenario_3.json         ← Asymmetric load
    ├── scenario_4.json         ← Operator-heavy (KPN)
    └── scenario_5.json         ← Worst-case convergence
```

---

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.10+ | Required by spec |
| UI | Streamlit | Required by spec; removes all frontend friction |
| Scheduling | Custom priority-queue simulation | See ARCHITECTURE.md |
| Data format | JSON | Human-readable, schema-flexible, no DB needed |
| State | In-memory | Spec explicitly says no DB needed |
| Hosting | Streamlit Community Cloud | Free, 2-click deploy from GitHub |

---

## Assumptions

See `ARCHITECTURE.md` for the full list. Key ones:

- Speed is uniform (60 km/h) with no traffic variation
- Charging always restores to full (no partial charging)
- Buses are processed in departure-time order; ties broken by bus ID
- For the greedy station-selection, we score against already-committed plans — this is fast and produces good results but is not globally optimal
- The scoring function uses the **sum** of weighted soft-rule costs; rules with higher weight have proportionally more influence on station selection

---

## Future Improvements

- Global optimisation (constraint solver / branch-and-bound) for provably optimal plans
- Real-time updates as buses report their actual positions
- Driver shift constraints
- Time-of-day electricity cost rules
- Multiple routes sharing stations
- Web-socket live dashboard

---

## API Summary (scheduler package)

```python
from scheduler import load_all_scenarios, schedule, format_minutes

scenarios = load_all_scenarios(Path("scenarios/"))
results, violations = schedule(scenarios[0])

for r in results:
    print(r.bus.id, format_minutes(r.arrival_min), r.total_wait_min)
```
