import copy
import tempfile
import unittest
from pathlib import Path

from scheduler.engine import StationQueues, schedule
from scheduler.loader import ScenarioValidationError, load_all_scenarios, load_scenario
from scheduler.models import Station
from scheduler.rules import validate_hard_rules


SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"


class SchedulerIntegrationTests(unittest.TestCase):
    def test_all_bundled_scenarios_schedule_without_hard_rule_violations(self):
        scenarios = load_all_scenarios(SCENARIOS_DIR)

        self.assertEqual(len(scenarios), 5)
        for scenario in scenarios:
            with self.subTest(scenario=scenario.id):
                results, violations = schedule(scenario)
                self.assertEqual(len(results), len(scenario.buses))
                self.assertEqual(violations, [])

                ok, validation_messages = validate_hard_rules(results, scenario)
                self.assertTrue(ok)
                self.assertEqual(validation_messages, [])

    def test_station_queue_can_use_gap_before_future_booking(self):
        queues = StationQueues([Station(id="A", name="Station A", chargers=1, charge_time_min=25)])
        queues.book("A", 200, 225)

        self.assertEqual(queues.earliest_slot("A", 120, 25), 120)

        queues.book("A", 120, 145)
        self.assertEqual(queues.queue_order("A"), [120, 200])

    def test_multiple_chargers_allow_parallel_bookings(self):
        queues = StationQueues([Station(id="B", name="Station B", chargers=2, charge_time_min=25)])

        self.assertEqual(queues.earliest_slot("B", 100, 25), 100)
        queues.book("B", 100, 125)
        self.assertEqual(queues.earliest_slot("B", 100, 25), 100)
        queues.book("B", 100, 125)
        self.assertEqual(queues.earliest_slot("B", 100, 25), 125)

    def test_weight_changes_can_change_schedule(self):
        scenario = load_scenario(SCENARIOS_DIR / "scenario_1.json")
        low_operator = copy.deepcopy(scenario)
        low_operator.weights.operator = 0.0
        high_operator = copy.deepcopy(scenario)
        high_operator.weights.operator = 10.0

        low_results, _ = schedule(low_operator)
        high_results, _ = schedule(high_operator)

        low_signature = [
            (r.bus.id, tuple(e.station_id for e in r.charge_events), round(r.total_wait_min, 2))
            for r in sorted(low_results, key=lambda result: result.bus.id)
        ]
        high_signature = [
            (r.bus.id, tuple(e.station_id for e in r.charge_events), round(r.total_wait_min, 2))
            for r in sorted(high_results, key=lambda result: result.bus.id)
        ]

        self.assertNotEqual(low_signature, high_signature)


class LoaderValidationTests(unittest.TestCase):
    def test_invalid_departure_time_raises_clear_error(self):
        source = SCENARIOS_DIR / "scenario_1.json"
        text = source.read_text(encoding="utf-8").replace('"departure": "19:00"', '"departure": "99:00"', 1)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scenario_bad.json"
            path.write_text(text, encoding="utf-8")

            with self.assertRaisesRegex(ScenarioValidationError, "valid 24-hour time"):
                load_scenario(path)


if __name__ == "__main__":
    unittest.main()
