"""Phase 8 Part A items 1 and 2: what a call really cost, and how full its context was.

taskq_lib.accounting is pure (no files, no settings), so these tests call it directly.
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from taskq_lib import accounting  # noqa: E402


def envelope(iteration, windows=None):
    return {"usage": {"iterations": [{"input_tokens": 1, "output_tokens": 1,
                                      "cache_read_input_tokens": 1, "cache_creation_input_tokens": 1}, iteration]},
            "modelUsage": windows or {"claude-sonnet-5": {"contextWindow": 1000, "costUSD": 1.0}}}


class CostIncreaseTests(unittest.TestCase):
    def test_two_rows_in_one_session_at_010_and_025_total_025_not_035(self):
        costs = {}
        first, costs = accounting.cost_increase(costs, "s1", 0.10)
        second, costs = accounting.cost_increase(costs, "s1", 0.25)
        self.assertAlmostEqual(first + second, 0.25)
        self.assertAlmostEqual(second, 0.15)

    def test_a_new_session_counts_its_whole_total(self):
        costs = {"s1": 0.25}
        cost, costs = accounting.cost_increase(costs, "s2", 0.05)
        self.assertEqual(cost, 0.05)
        self.assertEqual(costs, {"s1": 0.25, "s2": 0.05})

    def test_a_total_that_dropped_is_a_new_run_of_the_same_id(self):
        cost, _ = accounting.cost_increase({"s1": 0.9}, "s1", 0.2)
        self.assertEqual(cost, 0.2)

    def test_a_resumed_session_with_a_new_id_continues_the_old_total(self):
        cost, costs = accounting.cost_increase({"old": 1.0}, "new", 1.25, resumed_from="old")
        self.assertAlmostEqual(cost, 0.25)
        self.assertEqual(costs["new"], 1.25)

    def test_no_total_or_no_session_costs_nothing_extra(self):
        self.assertEqual(accounting.cost_increase({}, "s", None), (None, {}))
        cost, costs = accounting.cost_increase({}, None, 0.4)
        self.assertEqual((cost, costs), (0.4, {}))

    def test_the_table_stays_bounded_and_keeps_the_newest(self):
        costs = {}
        for n in range(10):
            _, costs = accounting.cost_increase(costs, f"s{n}", 1.0, keep=4)
        self.assertEqual(list(costs), ["s6", "s7", "s8", "s9"])


class LegacyCostTests(unittest.TestCase):
    def test_coordinator_rows_without_cost_usd_use_the_rise_in_file_order(self):
        rows = [{"label": "coordinator", "total_cost_usd": t} for t in (0.10, 0.25, 0.30, 0.05, 0.15)]
        self.assertEqual([round(c, 2) for c in accounting.fill_costs(rows)], [0.10, 0.15, 0.05, 0.05, 0.10])

    def test_other_labels_are_fresh_sessions_so_the_total_is_the_cost(self):
        rows = [{"label": "worker", "total_cost_usd": t} for t in (0.5, 0.3, 0.7)]
        self.assertEqual(accounting.fill_costs(rows), [0.5, 0.3, 0.7])

    def test_a_row_with_cost_usd_keeps_it_and_one_without_a_total_is_zero(self):
        rows = [{"label": "coordinator", "total_cost_usd": 2.0, "cost_usd": 0.4}, {"label": "worker", "total_cost_usd": None}]
        self.assertEqual(accounting.fill_costs(rows), [0.4, 0.0])


class ContextTests(unittest.TestCase):
    def test_context_is_the_last_iteration_over_the_models_window(self):
        env = envelope({"input_tokens": 10, "cache_read_input_tokens": 600, "cache_creation_input_tokens": 90, "output_tokens": 5})
        self.assertEqual(accounting.context_of(env, "claude-sonnet-5"), (700, 70.0))

    def test_the_window_comes_from_the_called_model_not_a_sub_agent(self):
        env = envelope({"input_tokens": 100}, {"claude-haiku-4-5": {"contextWindow": 200, "costUSD": 9.0},
                                               "claude-sonnet-5": {"contextWindow": 1000, "costUSD": 0.1}})
        self.assertEqual(accounting.context_of(env, "claude-sonnet-5"), (100, 10.0))

    def test_without_a_matching_model_the_dearest_one_is_used(self):
        env = envelope({"input_tokens": 100}, {"a": {"contextWindow": 200, "costUSD": 1.0}, "b": {"contextWindow": 1000, "costUSD": 5.0}})
        self.assertEqual(accounting.context_of(env, "sonnet"), (100, 10.0))

    def test_missing_pieces_give_none(self):
        self.assertEqual(accounting.context_of({}, "m"), (None, None))
        self.assertEqual(accounting.context_of({"usage": {"iterations": []}}, "m"), (None, None))
        no_window = {"usage": {"iterations": [{"input_tokens": 5}]}, "modelUsage": {}}
        self.assertEqual(accounting.context_of(no_window, "m"), (5, None))

    def test_billed_tokens_leave_out_cache_reads(self):
        usage = {"input_tokens": 3, "output_tokens": 4, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 999}
        self.assertEqual(accounting.tokens_of(usage), 12)
        self.assertEqual(accounting.tokens_of(None), 0)


if __name__ == "__main__":
    unittest.main()
