"""Phase 8 Part A item 3 and Part B item 7's counting rules: the numbers that raise an alert, and that each says so once."""

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from taskq_lib import alerts  # noqa: E402
from test_baseline import DEFAULT_BODY, PASS  # noqa: E402
from test_limits import LimitsCase  # noqa: E402

UTC = timezone.utc


def context(tokens):
    return {"envelope": {"usage": {"iterations": [{"input_tokens": tokens}], "input_tokens": tokens}}}


def spent(dollars, tokens=0):
    return {"envelope": {"total_cost_usd": dollars, "usage": {"input_tokens": tokens, "iterations": [{"input_tokens": 1}]}}}


class AlertRuleTests(unittest.TestCase):
    def test_a_condition_is_sent_once_per_day(self):
        state = alerts.mark_sent({}, "a", "2026-10-16")
        self.assertTrue(alerts.sent_today(state, "a", "2026-10-16"))
        self.assertFalse(alerts.sent_today(state, "a", "2026-10-17"))
        self.assertFalse(alerts.sent_today(state, "b", "2026-10-16"))

    def test_marking_a_new_day_forgets_the_old_ones(self):
        state = alerts.mark_sent(alerts.mark_sent({}, "a", "2026-10-16"), "b", "2026-10-17")
        self.assertEqual(state, {"b": "2026-10-17"})

    def test_periods_of_each_schedule_form(self):
        self.assertEqual(alerts.schedule_period("every 30m"), 1800)
        self.assertEqual(alerts.schedule_period("every 2d"), 172800)
        self.assertEqual(alerts.schedule_period("daily at 06:30"), 86400)
        self.assertEqual(alerts.schedule_period("weekly on mon at 09:00"), 604800)
        self.assertIsNone(alerts.schedule_period("monthly"))


class ContextAlertTests(LimitsCase):
    def test_a_worker_over_the_warn_line_names_the_task_and_says_it_is_too_big(self):
        self.sb.configure(CONTEXT_WARN_PCT="70")
        self.sb.set_scenario(worker=[context(800000)], review=[PASS])
        self.sb.add_task("big-one")
        self.sb.run()
        (alert,) = [t for t in self.sb.texts() if "context" in t.lower()]
        self.assertIn("big-one", alert)
        self.assertIn("80%", alert)
        self.assertIn("smaller", alert)

    def test_the_same_condition_is_sent_once_a_day_and_again_the_next(self):
        self.sb.configure(CONTEXT_WARN_PCT="70", MAX_ATTEMPTS_PER_DAY="0")
        self.sb.set_scenario(worker=[context(800000)], review=[{"result": "VERDICT: FAIL\n- no"}, PASS])
        self.sb.add_task("big-one")
        self.sb.run()
        self.sb.run()
        self.assertEqual(len([t for t in self.sb.texts() if "context" in t.lower()]), 1)
        state = self.sb.base / "state" / "alerts.json"
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        state.write_text(json.dumps({k: yesterday for k in json.loads(state.read_text())}))
        self.sb.add_task("big-one", state="pending")
        self.sb.run()
        self.assertEqual(len([t for t in self.sb.texts() if "context" in t.lower()]), 2)

    def test_below_the_line_or_with_the_setting_at_zero_nothing_is_said(self):
        self.sb.set_scenario(worker=[context(800000)], review=[PASS])
        self.sb.add_task("a")
        self.sb.run()  # no setting: off
        self.sb.configure(CONTEXT_WARN_PCT="90")
        self.sb.add_task("b")
        self.sb.run()
        self.assertEqual([t for t in self.sb.texts() if "context" in t.lower()], [])


class BudgetAlertTests(LimitsCase):
    def test_the_cost_budget_alert_says_what_crossed_and_fires_once_a_day(self):
        self.sb.configure(DAILY_COST_BUDGET_USD="1.00")
        self.sb.set_scenario(worker=[spent(0.60)], review=[{**spent(0.60), "result": "VERDICT: PASS"}])
        self.sb.add_task("a")
        self.sb.add_task("b")
        self.sb.run()
        self.sb.run()
        (alert,) = [t for t in self.sb.texts() if "budget" in t.lower()]
        self.assertIn("$1.20", alert)
        self.assertIn("$1.00", alert)

    def test_the_token_budget_counts_billed_tokens(self):
        self.sb.configure(DAILY_TOKEN_BUDGET="1000")
        self.sb.set_scenario(worker=[spent(0.1, tokens=1500)], review=[PASS])
        self.sb.add_task("a")
        self.sb.run()
        (alert,) = [t for t in self.sb.texts() if "budget" in t.lower()]
        self.assertIn("tokens", alert)

    def test_under_budget_or_off_says_nothing(self):
        self.sb.configure(DAILY_COST_BUDGET_USD="50", DAILY_TOKEN_BUDGET="10000000")
        self.sb.add_task("a")
        self.sb.run()
        self.assertEqual([t for t in self.sb.texts() if "budget" in t.lower()], [])


def template(sb, name, schedule, last_run):
    directory = sb.base / "tasks" / "recurring"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\nmodel: fake-worker\nreview_model: fake-review\nmax_attempts: 1\nattempts: 0\nschedule: {schedule}\n"
        f"last_run: {last_run}\n---\n{DEFAULT_BODY}")


class MissedRunTests(LimitsCase):
    def test_a_template_that_missed_two_periods_raises_exactly_one_alert(self):
        five_hours_ago = (datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S")
        template(self.sb, "nightly", "every 1h", five_hours_ago)
        self.sb.add_task("nightly-20260101-000000", state="pending")  # an earlier instance that never finishes
        (self.sb.base / "tasks" / "pending" / "nightly-20260101-000000.md").write_text(
            "---\nmodel: fake-worker\nreview_model: fake-review\nmax_attempts: 3\nattempts: 0\ndepends_on: never-exists\n---\n" + DEFAULT_BODY)
        self.sb.run()
        self.sb.run()
        missed = [t for t in self.sb.texts() if "has not run since" in t]
        self.assertEqual(len(missed), 1)
        self.assertIn("nightly", missed[0])
        self.assertIn("last result", missed[0])

    def test_a_template_that_is_on_time_or_just_ran_says_nothing(self):
        template(self.sb, "nightly", "every 1h", (datetime.now() - timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%S"))
        self.sb.run()  # due: it is spawned and runs now, so it did not miss anything
        self.assertEqual([t for t in self.sb.texts() if "has not run since" in t], [])

    def test_the_alert_is_dropped_when_the_job_runs_again_so_a_new_miss_is_said(self):
        template(self.sb, "nightly", "every 1h", (datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S"))
        (self.sb.base / "tasks" / "pending").mkdir(parents=True, exist_ok=True)
        blocker = self.sb.base / "tasks" / "pending" / "nightly-20260101-000000.md"
        blocker.write_text("---\nmodel: fake-worker\nreview_model: fake-review\nmax_attempts: 3\nattempts: 0\n"
                           "depends_on: never-exists\n---\n" + DEFAULT_BODY)
        self.sb.run()
        seen = json.loads((self.sb.base / "state" / "seen.json").read_text())
        self.assertTrue(any(k.startswith("missed|") for k in seen))
        blocker.unlink()
        self.sb.run()  # the template is due, spawns an instance, which runs
        seen = json.loads((self.sb.base / "state" / "seen.json").read_text())
        self.assertFalse(any(k.startswith("missed|") for k in seen))


class EmptyResultTests(LimitsCase):
    def test_a_quiet_job_that_produced_nothing_says_so_in_one_line(self):
        self.sb.set_scenario(worker=[{"result": ""}], review=[PASS])
        self.sb.add_task("improver", quiet="true")
        self.sb.run()
        texts = self.sb.texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("produced nothing", texts[0])
        self.assertEqual(len(texts[0].splitlines()), 1)

    def test_a_quiet_job_with_a_report_stays_silent(self):
        self.sb.set_scenario(worker=[{"result": "Nothing to improve this week."}], review=[PASS])
        self.sb.add_task("improver", quiet="true")
        self.sb.run()
        self.assertEqual(self.sb.texts(), [])


if __name__ == "__main__":
    unittest.main()
