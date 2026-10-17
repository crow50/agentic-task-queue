"""Phase 8 Part A items 1 to 3: the cost fix, context measures and the alerts, through the real dispatcher."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from test_baseline import PASS
from test_limits import FIXTURE_ENVELOPE, LimitsCase, iso

UTC = timezone.utc


def row(when, label="worker", model="m", total=1.0, cost=None, session="s", **extra):
    record = {"time": iso(when), "label": label, "model": model, "exit": 0, "status": "ok", "total_cost_usd": total,
              "num_turns": 1, "duration_ms": 1, "usage": {}, "modelUsage": {}, "permission_denials": [],
              "terminal_reason": "completed", "is_error": False, "session_id": session, **extra}
    if cost is not None:
        record["cost_usd"] = cost
    return record


class UsageRowTests(LimitsCase):
    def test_every_row_carries_the_session_the_cost_and_the_context(self):
        self.sb.add_task("t")
        self.sb.run()
        worker, review = self.sb.usage()
        self.assertNotEqual(worker["session_id"], review["session_id"])
        for line in (worker, review):
            self.assertEqual(line["cost_usd"], FIXTURE_ENVELOPE["total_cost_usd"])  # a fresh session: the whole total
            self.assertEqual(line["context_tokens"], 2 + 43231 + 5513)  # the last iteration only
            self.assertEqual(line["context_pct"], 4.9)  # of the 1,000,000 window
        self.assertEqual(worker["task"], "t")

    def test_a_second_row_in_one_session_logs_only_the_increase(self):
        self.sb.set_scenario(worker=[{"envelope": {"session_id": "same", "total_cost_usd": 0.10}},
                                     {"envelope": {"session_id": "same", "total_cost_usd": 0.25}}],
                             review=[{"result": "VERDICT: FAIL\n- no"}, PASS])
        self.sb.add_task("t")
        self.sb.run()
        self.sb.run()
        costs = [u["cost_usd"] for u in self.sb.usage() if u["label"] == "worker"]
        self.assertEqual([round(c, 2) for c in costs], [0.10, 0.15])
        saved = json.loads((self.sb.base / "state" / "session_costs.json").read_text())
        self.assertEqual(saved["same"], 0.25)

    def test_a_failed_call_has_no_cost_and_does_not_touch_the_table(self):
        self.sb.set_scenario(worker=[{"exit": 1, "stdout": "", "stderr": "boom"}])
        self.sb.add_task("t")
        self.sb.run()
        (line,) = [u for u in self.sb.usage() if u["label"] == "worker"]
        self.assertIsNone(line["cost_usd"])
        self.assertFalse((self.sb.base / "state" / "session_costs.json").exists())


class UsageCommandTests(LimitsCase):
    def table(self, *args):
        out = self.sb.run("usage", *args).stdout
        return {r[0]: r[1:] for r in (line.split() for line in out.splitlines()) if len(r) == 7 and r[1] != "calls"}

    def test_rows_without_cost_usd_are_summed_by_the_fallback(self):
        now = datetime.now(UTC)
        self.sb.write_usage([
            row(now - timedelta(minutes=3), "coordinator", total=1.00, session=None),
            row(now - timedelta(minutes=2), "coordinator", total=1.30, session=None),
            row(now - timedelta(minutes=1), "worker", total=0.50, session=None),
        ])
        self.assertEqual(self.table("--by", "label")["coordinator"][2], "1.3000")
        self.assertEqual(self.table("--by", "label")["worker"][2], "0.5000")

    def test_new_rows_sum_cost_usd_not_the_running_total(self):
        now = datetime.now(UTC)
        self.sb.write_usage([row(now, "coordinator", total=0.10, cost=0.10), row(now, "coordinator", total=0.25, cost=0.15)])
        total = self.table("--by", "label")["coordinator"]
        self.assertEqual(total[2], "0.2500")  # not 0.35

    def test_by_day_model_label_and_agent(self):
        now = datetime.now(UTC)
        usage = {"input_tokens": 10, "cache_creation_input_tokens": 20, "output_tokens": 30, "cache_read_input_tokens": 999}
        self.sb.write_usage([
            row(now, "worker", "sonnet", cost=0.4, usage=usage, agent="scout"),
            row(now - timedelta(days=1), "review", "haiku", cost=0.1, usage=usage),
        ])
        by_day = self.table("--by", "day")
        self.assertEqual(len([k for k in by_day if k != "total"]), 2)
        self.assertEqual(self.table("--by", "model")["sonnet"], ["1", "0", "0.4000", "60", "1", "0"])
        self.assertEqual(sorted(k for k in self.table("--by", "label") if k != "total"), ["review", "worker"])
        self.assertEqual(sorted(k for k in self.table("--by", "agent") if k != "total"), ["(none)", "scout"])

    def test_an_unknown_grouping_is_refused_with_the_choices(self):
        proc = self.sb.python(str(self.sb.base / "dispatcher.py"), "usage", "--by", "colour")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("day|label|model|agent", proc.stderr)


if __name__ == "__main__":
    unittest.main()
