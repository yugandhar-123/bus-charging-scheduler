# ARCHITECTURE.md — Bus Charging Scheduler

---

## 1. Framework / Approach

### What I chose: Priority-queue simulation with a cost-guided greedy station selector

The scheduler processes buses in departure-time order. For each bus it:

1. Enumerates all feasible charging-stop subsets (those satisfying the range hard rule).
2. For each subset, simulates the bus's full timeline against already-committed charger queues to compute an estimated weighted cost.
3. Commits the plan with the lowest cost and moves to the next bus.

This is a **greedy simulation** guided by a **weighted cost function** that reads directly from the scenario's weight configuration.

### Why this is the right fit

| Property | How this approach delivers it |
|---|---|
| **Correctness** | Hard rules are checked before any bus is committed. Unfeasible plans are never scored. |
| **Weight tunability** | The cost function reads `scenario.weights`. Changing a weight is a JSON edit. |
| **Extensibility** | Rules are registered with a decorator. New rules slot in without touching the engine. |
| **Scalability** | Linear in bus count for a fixed set of stations; station-plan enumeration is exponential in station count and is the main future optimization target. |
| **Determinism** | Ties in departure time broken by bus ID — same input always produces same output. |

### Why not a constraint solver (OR-Tools, PuLP)?

A constraint solver would give a globally optimal solution but requires encoding every rule as a mathematical constraint — which is brittle when rules change frequently. The greedy approach trades a small loss in global optimality for far better extensibility. Given that the spec says "we'll keep adding rules as we learn more," extensibility wins.

### Why not a simple FCFS queue?

First-come-first-served ignores operator fairness and overall network cost. The weighted cost function lets the scheduler trade individual wait, operator balance, and total network delay instead of using a single queue policy everywhere.

---

## 2. Data Structure Design

### The scenario file is a self-contained world

A scenario JSON carries:
- **Route**: named stops, segment distances, travel speed
- **Stations**: ID, name, charger count, charge time
- **Battery**: range, charge-to mode
- **Weights**: individual, operator, overall (plus any future dimensions)
- **Buses**: ID, operator, direction, departure time

Nothing is hardcoded in the engine. Every physical constant and configuration value comes from the data file.

### Why JSON (not CSV or a database)?

- Human-readable and writable without tooling
- Hierarchical — handles nested objects (route with segments, station with charger count) naturally
- Schema is implicit but flexible — adding a field to a JSON file is a one-liner
- No migration scripts, no DB setup, no ORM

### Key design decisions in the data model

**Segments carry distance, not travel time.** Travel time is derived at runtime from `speed_kmh`. Changing the speed (or making it per-segment) requires only a data change.

**Stations carry `chargers` as an integer.** Today it's 1. Tomorrow it's 3 for busy inner stations. The engine uses this generically — no code change.

**`charge_time_min` lives on the Station, not on a global constant.** Different stations can have different charger speeds. The field is already there.

**`direction` is a string code ("BK" / "KB"), not a boolean.** Future routes with more than two directions (circular routes, branch routes) can be added by extending the loader.

---

## 3. Changes I Anticipated (and how the design handles each)

### A. Different weights per scenario
**Status: done.** Each scenario JSON carries its own `weights` block. Scenario 4 already demonstrates `operator: 2.0`.

### B. More buses (50, 200, 1000)
**Status: partially handled.** The engine iterates over `scenario.buses` regardless of count and no loops are hardcoded to 20. For a fixed small station count this scales cleanly with buses, but adding many stations increases the feasible-stop search space and should be optimized with pruning or dynamic programming.

### C. More stations along the route (add E, F, G between existing stops)
**Status: handled by data.** Add new entries to `route.segments` and `stations` in the JSON. The engine enumerates feasible subsets dynamically from whatever stations are present. Zero code changes.

### D. Change a segment distance (e.g. A→B becomes 150 km instead of 120 km)
**Status: handled by data.** Edit `distance_km` in the JSON. All range checks recompute at load time.

### E. Change travel speed (or make it per-segment)
**Status: partially handled.** `speed_kmh` is on the route today. Making it per-segment requires adding `speed_kmh` to each segment in the JSON and updating `Route.travel_time_min()` — a 5-line change confined to `models.py`.

### F. Multiple chargers at one station (e.g. Station B gets 3 chargers)
**Status: handled.** `StationQueues` already manages a list of per-charger free times sized by `station.chargers`. Change `"chargers": 1` to `"chargers": 3` in the JSON.

### G. New operator (e.g. RedBus joins)
**Status: handled.** Operators are just strings on `Bus`. No operator registry exists in the engine. Adding a new operator = adding buses with `"operator": "redbus"` in the JSON.

### H. Priority buses (VIP, emergency)
**Status: data model ready.** Add `"priority": true` to a bus in JSON. Add a `priority` field to the `Bus` dataclass. Write a soft rule (or hard rule for pre-emption) in `rules.py`. Engine changes: zero.

### I. Time-of-day electricity cost
**Status: extensible.** Add `"electricity": 1.5` to `weights` in JSON. Add `electricity: float` to `Weights`. Write a `@soft_rule("electricity_cost", weight_key="electricity")` function in `rules.py` that sums cost based on charge-start times. Done.

### J. Driver shift constraints (a bus must reach a certain station before the driver's max shift ends)
**Status: extensible.** Add `driver_shift_end_min` to `Bus` in JSON and dataclass. Write a `@hard_rule("driver_shift")` in `rules.py` that checks `r.arrival_min <= bus.driver_shift_end_min`. Engine changes: zero.

### K. Multiple routes sharing stations
**Status: extensible.** Each scenario already carries a self-contained route. If two routes share Station B, run two schedulers with a shared `StationQueues` object. The `StationQueues` class is already decoupled from the route — it just tracks charger availability by station ID.

### L. Different battery capacities per bus (e.g. newer fleet with 320 km range)
**Status: extensible.** Move `full_range_km` from the scenario-level `battery` block to each bus entry in JSON. Update `Bus` dataclass. Update `_feasible_station_subsets` to read `bus.battery_range_km` instead of `scenario.battery.full_range_km`.

### M. Partial charging (charge to 80% instead of full)
**Status: extensible.** `Battery.charge_to` is already a string field (`"full"` today). Engine currently always charges to full, but the field is a hook: change the logic in `_simulate_bus` to compute range based on charge percentage. No data-model change needed.

### N. Adding a new scenario
**Status: trivially done.** Copy any `scenario_N.json`, edit the `buses` array and metadata, save as `scenario_6.json`. It appears in the dropdown on next run. No code changes.

### O. Real-time updates (buses report actual GPS position)
**Status: architecturally possible.** The engine is a pure function: `schedule(scenario) → results`. To support live updates, wrap it in a polling loop that rebuilds the scenario with updated bus positions and re-runs the scheduler. The function itself doesn't change.

---

## 4. How to change a weight

Open the relevant scenario JSON, e.g. `scenarios/scenario_4.json`:

```json
"weights": {
  "individual": 1.0,
  "operator":   2.0,
  "overall":    1.0
}
```

Change `"operator": 2.0` to `"operator": 0.5`. Reload the app. Done.

The weight value flows through this exact path:

```
scenario JSON
  └─ loader.py        (Weights dataclass)
       └─ engine.py   (passes scenario to compute_total_cost)
            └─ rules.py  (scenario.weights.value_for("operator"))
```

One value. One place.

---

## 5. How to add a new rule

**Scenario: penalise any single bus waiting more than 30 minutes (hard cap)**

Step 1 — Add a hard rule to `scheduler/rules.py`:

```python
@hard_rule("max_single_wait")
def max_single_wait(results, scenario):
    MAX_WAIT = 30  # minutes; could be a scenario config value
    for r in results:
        if r.total_wait_min > MAX_WAIT:
            return False, f"{r.bus.id} waited {r.total_wait_min:.0f} min (max {MAX_WAIT})"
    return True, ""
```

That's it. The engine calls `validate_hard_rules()` which iterates `get_hard_rules()` — the decorator registered it automatically.

**Scenario: add a soft rule for electricity cost**

Step 1 — Add `electricity: float = 1.0` to `Weights` in `models.py`.
Step 2 — Add `"electricity": 1.5` to each scenario's `weights` block.
Step 3 — Add in `rules.py`:

```python
@soft_rule("electricity_cost", weight_key="electricity")
def electricity_cost(results, scenario):
    PEAK_HOURS_START = 22 * 60  # 22:00 in minutes
    cost = 0.0
    for r in results:
        for e in r.charge_events:
            if e.charge_start_min % (24 * 60) >= PEAK_HOURS_START:
                cost += 10.0  # flat penalty per off-peak charge
    return cost
```

No engine changes. No loader changes. No UI changes. Unknown JSON weight keys are stored in `Weights.extra`, and `compute_total_cost()` reads them through `scenario.weights.value_for(...)`.

---

## 6. Assumptions Made

1. **Speed is uniform at 60 km/h.** The spec says "use a consistent speed." I chose 60 km/h as a realistic highway speed. Any value works — it's one JSON field.

2. **Buses are scheduled in departure-time order.** Earlier-departing buses get first access to chargers. This is the fairest baseline behaviour.

3. **Station selection is greedy (not globally optimal).** For each bus we pick the charging plan with the lowest weighted cost given already-committed buses. This is fast (milliseconds for 20 buses) and produces sensible, defensible plans. A constraint solver would be globally optimal but less extensible.

4. **The cost function scores the new bus's plan against the full set of already-committed results.** This means earlier buses anchor the schedule and later buses adapt around them — a natural model for a real dispatch system.

5. **Ties in departure time are broken by bus ID (alphabetical).** This ensures determinism: the same input always produces the same output.

6. **Charging always restores to full.** The spec states this. Partial charging is a one-line code change once the spec changes.

7. **Endpoints (Bengaluru, Kochi) are not scheduling stations.** The spec explicitly says so. They are start/end anchors only.

8. **Operator cost combines wait variance and station concentration.** This penalises unequal treatment within a fleet and also discourages sending one operator's buses through the same stations when alternatives exist.

9. **A bus is assumed to be ready to depart at its departure time.** Any pre-departure delays are out of scope.

10. **The 240 km range is enforced with a 0.01 km epsilon** to avoid floating-point edge cases on exact-boundary distances.
