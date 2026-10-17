"""Phase 8 Part B items 4 and 7: every notice for a condition carries the button for its obvious next step, each
condition is said once, and one that is still true after a day is said once more with the time that has passed."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from test_baseline import PASS, fail
from test_limits import AUTH_EXPIRED, LimitsCase, usage_limit
from test_visibility_alerts import template
from test_worktree_pr import PrCase

UTC = timezone.utc


def buttons_of(sandbox, containing):
    """The (label, data) pairs on the message whose text contains `containing`."""
    for request in sandbox.telegram.calls("sendMessage"):
        if containing in request.params.get("text", "") and request.params.get("reply_markup"):
            rows = json.loads(request.params["reply_markup"])["inline_keyboard"]
            return [(b["text"], b["callback_data"]) for row in rows for b in row]
    return None


class AlertButtonTests(LimitsCase):
    def test_a_task_that_failed_for_good_carries_retry_and_open(self):
        self.sb.set_scenario(worker=[{"result": "tried"}], review=[fail("nope")])
        self.sb.add_task("doomed", max_attempts="1")
        self.sb.run()
        self.assertEqual(buttons_of(self.sb, "Task failed"), [("Retry", "act:retry:doomed"), ("Open", "act:open:doomed")])

    def test_a_pause_for_a_usage_limit_carries_resume(self):
        self.sb.set_scenario(worker=[usage_limit(3600)])
        self.sb.add_task("t")
        self.sb.run()
        self.assertEqual(buttons_of(self.sb, "Queue paused"), [("Resume", "act:resume")])

    def test_an_expired_login_carries_resume_to_check_it_again(self):
        self.sb.set_scenario(worker=[AUTH_EXPIRED])
        self.sb.add_task("t")
        self.sb.run()
        self.assertEqual(buttons_of(self.sb, "login expired"), [("Resume", "act:resume")])

    def test_a_task_waiting_for_approval_carries_approve_and_reject(self):
        self.sb.configure(APPROVAL_MIN_TIER="1")
        self.sb.add_task("writer", allowed_tools="Read,Write")
        self.sb.run()
        self.assertEqual(buttons_of(self.sb, "writer"), [("Approve", "act:approve:writer"), ("Reject", "act:reject:writer")])

    def test_a_missed_run_carries_open(self):
        template(self.sb, "nightly", "every 1h", (datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S"))
        (self.sb.base / "tasks" / "pending").mkdir(parents=True, exist_ok=True)
        (self.sb.base / "tasks" / "pending" / "nightly-20260101-000000.md").write_text(
            "---\nmodel: m\nreview_model: r\nmax_attempts: 3\nattempts: 0\ndepends_on: never-exists\n---\n"
            "# T\n\n## Acceptance Criteria\n- ok\n")
        self.sb.run()
        self.assertEqual(buttons_of(self.sb, "has not run since"), [("Open", "act:open:nightly")])

    def test_a_long_task_id_never_makes_a_callback_over_64_bytes(self):
        stem = "t" * 80
        self.sb.set_scenario(worker=[{"result": "tried"}], review=[fail("nope")])
        self.sb.add_task(stem, max_attempts="1")
        self.sb.run()
        data = [d for _, d in buttons_of(self.sb, "Task failed")]
        self.assertTrue(data and all(len(d.encode()) <= 64 for d in data))
        table = json.loads((self.sb.base / "state" / "callback_args.json").read_text())
        self.assertIn(stem, table.values())


class RepeatTests(LimitsCase):
    def alerts(self):
        return [t for t in self.sb.texts() if "login expired" in t.lower() or "still true" in t.lower()]

    def test_a_condition_that_stays_true_is_said_once_then_once_more_after_a_day_with_the_elapsed_time(self):
        self.sb.set_scenario(worker=[AUTH_EXPIRED], other=[AUTH_EXPIRED])
        self.sb.add_task("t")
        self.sb.run()
        for _ in range(3):
            self.sb.run()  # every cycle the login is still bad
        self.assertEqual(len(self.alerts()), 1)

        path = self.sb.base / "state" / "seen_at.json"
        seen = json.loads(path.read_text())
        old = (datetime.now(UTC) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
        path.write_text(json.dumps({k: {**v, "first": old} for k, v in seen.items()}))
        self.sb.run()
        again = self.alerts()
        self.assertEqual(len(again), 2)
        self.assertIn("Still true after 1 day", again[1])
        self.assertIn("login expired", again[1].lower())
        self.assertEqual(buttons_of(self.sb, "Still true"), [("Resume", "act:resume")])
        self.sb.run()
        self.sb.run()
        self.assertEqual(len(self.alerts()), 2, "the reminder is sent once, not every cycle")

    def test_a_condition_that_clears_and_comes_back_is_said_afresh(self):
        self.sb.set_scenario(worker=[AUTH_EXPIRED, {"result": "ok"}], other=[AUTH_EXPIRED, {"result": "ok"}], review=[PASS])
        self.sb.add_task("t")
        self.sb.run()
        self.sb.run()  # the probe fails again: nothing new
        self.assertEqual(len(self.alerts()), 1)


class HeldPrButtonTests(PrCase):
    def test_a_held_pull_request_carries_retry_and_cancel(self):
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.sb.gh_rules([{"match": "pr list", "stdout": "3\n"}])
        self.pr_task()
        self.sb.run()
        for request in self.sb.telegram.calls("sendMessage"):
            if "held" in request.params.get("text", "").lower():
                rows = json.loads(request.params["reply_markup"])["inline_keyboard"]
                self.assertEqual([(b["text"], b["callback_data"]) for row in rows for b in row],
                                 [("Retry", "act:retry:t"), ("Cancel", "act:cancel:t")])
                return
        self.fail("no held notice with buttons")


if __name__ == "__main__":
    unittest.main()
