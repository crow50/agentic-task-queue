"""Phase 9 Part A item 1: every task that ends leaves a record of what it produced.

state/outcomes/<id>.json says what kinds of result there are (a pull request, files, a report, an issue, or nothing),
the evidence for each, the session and the cost, and which Telegram messages announced it. /status and a follow-up
read from it; a task that finished before this existed gets the same record worked out from its file.
"""

import json
import unittest

from test_baseline import PASS, Sandbox, fail
from test_worktree_pr import URL, PrCase

FILE_BODY = "# Write it\n\nWrite out.md.\n\n## Acceptance Criteria\n- out.md exists\n"


class OutcomeCase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def outcome(self, stem):
        return json.loads((self.sb.base / "state" / "outcomes" / f"{stem}.json").read_text())

    def snippet(self, code):
        proc = self.sb.python("-c", "import dispatcher\n" + code)
        if proc.returncode != 0:
            raise AssertionError(f"snippet failed\n{proc.stderr}")
        return proc.stdout.strip()


class WrittenWhenATaskEndsTests(OutcomeCase):
    def test_a_plain_answer_leaves_a_report_outcome_with_everything_the_plan_lists(self):
        self.sb.set_scenario(worker=[{"result": "The answer is 42."}], review=[PASS])
        self.sb.add_task("ask")
        self.sb.run()

        record = self.outcome("ask")
        self.assertEqual((record["task"], record["status"], record["kinds"]), ("ask", "done", ["report"]))
        self.assertEqual(record["summary"], "The answer is 42.")
        self.assertTrue(record["session_id"])
        self.assertIsInstance(record["cost_usd"], float)
        self.assertRegex(record["finished"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertTrue(record["cwd"].endswith("workspace"))
        self.assertEqual((self.sb.base / "state" / "outcomes" / "ask.report.md").read_text(), "The answer is 42.")

    def test_the_outcome_remembers_the_message_that_announced_it(self):
        self.sb.set_scenario(worker=[{"result": "Done."}], review=[PASS])
        self.sb.add_task("ask")
        self.sb.run()

        (message_id,) = self.outcome("ask")["message_ids"]
        self.assertIsInstance(message_id, int)
        self.assertGreater(message_id, 0)

    def test_a_quiet_task_still_leaves_an_outcome_though_nothing_was_sent(self):
        self.sb.set_scenario(worker=[{"result": "Quietly done."}], review=[PASS])
        self.sb.add_task("hush", quiet="true")
        self.sb.run()

        self.assertEqual(self.outcome("hush")["message_ids"], [])
        self.assertEqual(self.outcome("hush")["kinds"], ["report"])

    def test_a_deliver_task_has_a_file_outcome_that_says_telegram_took_it(self):
        self.sb.set_scenario(worker=[{"result": "Wrote it.", "write": {"path": str(self.sb.base / "workspace" / "out.md"),
                                                                       "content": "hi\n"}}], review=[PASS])
        self.sb.add_task("deliverer", deliver="out.md", body=FILE_BODY)
        self.sb.run()

        record = self.outcome("deliverer")
        self.assertIn("file", record["kinds"])
        self.assertEqual(record["refs"]["file"], [{"path": "out.md", "delivered": True}])

    def test_a_file_telegram_refused_is_recorded_as_not_delivered(self):
        self.sb.telegram.queue_response("sendDocument", status=400, body={"ok": False, "description": "no"})
        self.sb.set_scenario(worker=[{"result": "Wrote it.", "write": {"path": str(self.sb.base / "workspace" / "out.md"),
                                                                       "content": "hi\n"}}], review=[PASS])
        self.sb.add_task("deliverer", deliver="out.md", body=FILE_BODY)
        self.sb.run()

        self.assertEqual(self.outcome("deliverer")["refs"]["file"], [{"path": "out.md", "delivered": False}])

    def test_a_task_that_runs_out_of_attempts_leaves_a_failed_outcome_with_the_last_feedback(self):
        self.sb.set_scenario(worker=[{"result": "Tried."}], review=[fail("Not done.")])
        self.sb.add_task("doomed", max_attempts="1")
        self.sb.run()

        record = self.outcome("doomed")
        self.assertEqual((record["status"], record["kinds"]), ("failed", ["none"]))
        self.assertIn("Not done.", record["summary"])
        self.assertEqual(len(record["message_ids"]), 1)

    def test_a_pass_that_produced_nothing_says_none(self):
        self.sb.set_scenario(worker=[{"result": ""}], review=[PASS])
        self.sb.add_task("empty", quiet="true")
        self.sb.run()
        self.assertEqual(self.outcome("empty")["kinds"], ["none"])

    def test_a_secret_in_the_report_is_not_kept_in_the_outcome(self):
        token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2
        self.sb.set_scenario(worker=[{"result": f"Used {token} to log in."}], review=[PASS])
        self.sb.add_task("leaky")
        self.sb.run()

        saved = (self.sb.base / "state" / "outcomes" / "leaky.json").read_text()
        saved += (self.sb.base / "state" / "outcomes" / "leaky.report.md").read_text()
        self.assertNotIn(token, saved)


class PullRequestOutcomeTests(PrCase):
    def test_a_repo_task_has_a_pr_outcome_carrying_the_checked_pull_request(self):
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.pr_task()
        self.sb.run()

        record = json.loads((self.sb.base / "state" / "outcomes" / "t.json").read_text())
        self.assertIn("pr", record["kinds"])
        self.assertEqual(record["refs"]["pr"], {"url": URL, "branch": "task/t", "draft": True, "checked": True})
        self.assertTrue(record["worktree"].endswith("worktrees/t"))


class OlderTasksTests(OutcomeCase):
    def test_a_task_from_before_outcomes_gets_one_worked_out_from_its_file(self):
        self.sb.add_task("old", state="done", pr_url="https://github.com/x/y/pull/7", branch="task/old", session_id="s-old",
                         body="# Old\n\nx\n\n## Acceptance Criteria\n- ok\n\n## Result (attempt 1, 2026-01-01T00:00:00Z)\n\nIt worked.\n")
        record = json.loads(self.snippet("import json; print(json.dumps(dispatcher.load_outcome('old')))"))
        self.assertEqual((record["status"], record["kinds"]), ("done", ["pr", "report"]))
        self.assertEqual(record["refs"]["pr"]["url"], "https://github.com/x/y/pull/7")
        self.assertEqual(record["session_id"], "s-old")
        self.assertIn("It worked.", record["summary"])

    def test_a_failed_and_a_cancelled_task_are_also_known(self):
        self.sb.add_task("bad", state="failed")
        self.sb.add_task("nope", state="cancelled")
        out = self.snippet("import json; print(json.dumps([(dispatcher.load_outcome(s) or {}).get('status') for s in ('bad', 'nope', 'ghost')]))")
        self.assertEqual(json.loads(out), ["failed", "cancelled", None])

    def test_a_task_that_is_still_queued_or_running_has_no_outcome_yet(self):
        self.sb.add_task("waiting")
        self.sb.add_task("busy", state="active")
        out = self.snippet("import json; print(json.dumps([dispatcher.load_outcome(s) for s in ('waiting', 'busy')]))")
        self.assertEqual(json.loads(out), [None, None])


class StatusShowsTheOutcomeTests(OutcomeCase):
    def test_status_of_a_finished_task_says_what_it_made(self):
        self.sb.set_scenario(worker=[{"result": "The answer is 42."}], review=[PASS])
        self.sb.add_task("ask")
        self.sb.run()
        text = self.snippet("print(dispatcher.task_status('ask')[1])")
        self.assertIn("done: a report", text)

    def test_the_task_list_names_the_last_ten_finished_tasks_with_their_kinds(self):
        for number in range(12):
            self.sb.add_task(f"t{number:02d}", state="done")
        self.sb.add_task("bad", state="failed")
        text = self.snippet("print(dispatcher.finished_tasks_text())")
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("- ")]), 10)
        self.assertIn("done", text)
        self.assertNotIn("t00", text)


if __name__ == "__main__":
    unittest.main()
