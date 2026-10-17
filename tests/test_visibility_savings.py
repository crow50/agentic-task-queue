"""Phase 8 Part A item 5: the savings, each behind its own switch."""

import unittest
from pathlib import Path

from test_baseline import DEFAULT_BODY, PASS
from test_verifier import VerifyCase

REPO = Path(__file__).resolve().parent.parent
COVERED = "# Task\n\n## Acceptance Criteria\n- The report exists [verify]\n- It has three sections [verify]\n"
PARTLY = "# Task\n\n## Acceptance Criteria\n- The report exists [verify]\n- It reads well\n"


class SkipIfVerifiedTests(VerifyCase):
    def passing(self):
        return self.script("verify-ok.sh", "echo ok\n")

    def test_a_verified_task_with_every_criterion_covered_makes_zero_review_calls(self):
        self.sb.add_task("t", verify=self.passing(), review="skip-if-verified", body=COVERED)
        self.sb.run()
        self.assertEqual(self.roles(), ["worker"])
        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertIn("review skipped", self.sb.texts()[0])

    def test_a_criterion_the_command_does_not_cover_still_gets_its_review(self):
        self.sb.add_task("t", verify=self.passing(), review="skip-if-verified", body=PARTLY)
        self.sb.run()
        self.assertEqual(self.roles(), ["worker", "review"])

    def test_a_task_without_a_verify_command_is_refused_not_silently_reviewed(self):
        self.sb.add_task("t", review="skip-if-verified", body=COVERED)
        self.sb.run()
        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertIn("needs a verify command", self.failure_reason("t"))

    def test_a_failing_verify_fails_the_attempt_without_a_review(self):
        command = self.script("verify-bad.sh", "exit 1\n")
        self.sb.add_task("t", verify=command, review="skip-if-verified", body=COVERED)
        self.sb.run()
        self.assertEqual(self.roles(), ["worker"])
        self.assertEqual(self.sb.names("pending"), ["t"])

    def test_without_the_switch_the_review_still_runs(self):
        self.sb.add_task("t", verify=self.passing(), body=COVERED)
        self.sb.run()
        self.assertEqual(self.roles(), ["worker", "review"])


class MaxTurnsTests(VerifyCase):
    def test_max_turns_is_passed_to_the_worker_only(self):
        self.sb.add_task("t", max_turns="7")
        self.sb.run()
        worker, review = self.sb.claude_calls()
        self.assertEqual(worker["argv"][worker["argv"].index("--max-turns") + 1], "7")
        self.assertNotIn("--max-turns", review["argv"])

    def test_without_it_no_limit_is_passed(self):
        self.sb.add_task("t")
        self.sb.run()
        self.assertNotIn("--max-turns", self.sb.claude_calls()[0]["argv"])

    def test_a_bad_value_is_refused(self):
        self.sb.add_task("t", max_turns="0")
        self.sb.run()
        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertIn("max_turns", self.failure_reason("t"))

    def test_a_worker_stopped_at_the_limit_is_told_why_on_the_next_attempt(self):
        stop = {"exit": 1, "envelope": {"subtype": "error_max_turns", "is_error": True, "result": "", "terminal_reason": "max_turns"}}
        self.sb.set_scenario(worker=[stop, {"result": "Done."}], review=[PASS])
        self.sb.add_task("t", max_turns="3")
        self.sb.run()
        _, body = self.sb.read_task("pending", "t")
        self.assertIn("turn limit", body)
        self.assertIn("max_turns: 3", body)


class PromptOrderTests(VerifyCase):
    def test_the_preamble_is_the_first_thing_in_every_worker_prompt(self):
        preamble = (REPO / "worker_preamble.md").read_text()
        self.sb.add_task("a")
        self.sb.add_task("b", body=DEFAULT_BODY + "\nExtra detail only this task has.\n")
        self.sb.run()
        self.sb.run()
        workers = [c for c in self.sb.claude_calls() if c["role"] == "worker"]
        self.assertEqual(len(workers), 2)
        for call in workers:
            self.assertTrue(call["prompt"].startswith(preamble))
        self.assertIn("Extra detail", workers[1]["prompt"][len(preamble):])

    def test_the_preamble_tells_workers_to_bring_back_summaries(self):
        text = (REPO / "worker_preamble.md").read_text()
        self.assertIn("with a script", text)
        self.assertIn("summary", text)


if __name__ == "__main__":
    unittest.main()
