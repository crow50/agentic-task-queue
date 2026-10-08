"""Phase 3 (PLAN.md): the verifier.

The reviewer can only read files, so what can be checked mechanically is
checked by code: a task's `verify:` command is run by the dispatcher after the
worker, only if it starts with an allowed prefix, and frontmatter is validated
against one table of known keys. Each test runs dispatcher.py as a subprocess
in the same Sandbox the earlier phases use. Stdlib only.
"""

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from test_baseline import PASS
from test_limits import LimitsCase, usage_limit

REPO = Path(__file__).resolve().parent.parent


class VerifyCase(LimitsCase):
    """LimitsCase plus helpers for scripts in the task's working directory."""

    def cwd(self):
        return self.sb.base / "workspace"

    def script(self, name, text):
        path = self.cwd() / "scripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return f"bash scripts/{name}"

    def task_text(self, state, name):
        return (self.sb.base / "tasks" / state / f"{name}.md").read_text()

    def check(self, state, name):
        path = self.sb.base / "tasks" / state / f"{name}.md"
        return subprocess.run(
            [sys.executable, str(self.sb.base / "dispatcher.py"), "check", str(path)],
            cwd=self.sb.base, env=self.sb.env(), capture_output=True, text=True,
        )

    def failure_reason(self, name):
        """The text of a task that was rejected or failed, as a person would read it."""
        return self.task_text("failed", name)


# ---------------------------------------------------------------- verify: the mechanical pass


class VerifyRunTests(VerifyCase):
    def test_a_verify_that_exits_1_consumes_the_attempt_and_makes_no_review_call(self):
        command = self.script("verify-fail.sh", "echo 'FAIL: 2 of 9 tests failed'\nexit 1\n")
        self.sb.set_scenario(worker=[{"result": "All tests pass, trust me."}])
        self.sb.add_task("t", verify=command)
        self.sb.run()

        self.assertEqual(self.roles(), ["worker"], "the reviewer is never called")
        self.assertEqual(self.sb.names("pending"), ["t"])
        meta, body = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "1", "the attempt is consumed")
        self.assertIn("## Attempt 1 Feedback", body)
        self.assertIn("exited 1", body)
        self.assertIn("FAIL: 2 of 9 tests failed", body, "the feedback carries the tail of the output")

    def test_the_next_attempt_sees_the_verify_output_and_runs_on_the_escalation_model(self):
        command = self.script("verify-fail.sh", "echo 'boom at line 7'\nexit 3\n")
        self.sb.add_task("t", verify=command)
        self.sb.run()
        self.sb.run()

        workers = self.sb.claude_calls("worker")
        self.assertIn("boom at line 7", workers[1]["prompt"])
        self.assertEqual(workers[1]["model"], "fake-escalated")
        self.assertEqual(self.roles(), ["worker", "worker"])

    def test_the_last_attempt_failing_verification_moves_the_task_to_failed(self):
        command = self.script("verify-fail.sh", "echo nope\nexit 1\n")
        self.sb.add_task("t", verify=command, max_attempts="1")
        self.sb.run()

        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertEqual(self.roles(), ["worker"])
        self.assertTrue(any("Task failed" in text and "nope" in text for text in self.sb.texts()))

    def test_only_the_tail_of_a_long_output_is_kept(self):
        command = self.script(
            "verify-long.sh", "for i in $(seq 1 3000); do echo \"line $i\"; done\nexit 1\n"
        )
        self.sb.add_task("t", verify=command)
        self.sb.run()

        _, body = self.sb.read_task("pending", "t")
        self.assertIn("line 3000", body)
        self.assertNotIn("line 1\n", body)
        self.assertLess(len(body), 6000)

    def test_a_verify_that_exits_0_with_review_skip_finishes_the_task_with_no_review(self):
        command = self.script("verify-ok.sh", "echo '12 tests, 0 failures'\n")
        self.sb.set_scenario(worker=[{"result": "Wrote the module."}])
        self.sb.add_task("t", verify=command, review="skip")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(self.roles(), ["worker"], "0 review calls")
        meta, body = self.sb.read_task("done", "t")
        self.assertEqual(meta["attempts"], "1")
        self.assertIn("Wrote the module.", body)
        self.assertIn("12 tests, 0 failures", body)
        (text,) = self.sb.texts()
        self.assertIn("Task done: t", text)
        self.assertIn("review skipped", text)

    def test_a_verify_that_exits_0_without_skip_still_gets_a_review_with_the_output_as_evidence(self):
        command = self.script("verify-ok.sh", "echo '12 tests, 0 failures'\n")
        self.sb.add_task("t", verify=command)
        self.sb.run()

        self.assertEqual(self.roles(), ["worker", "review"])
        prompt = self.sb.claude_calls("review")[0]["prompt"]
        self.assertIn("12 tests, 0 failures", prompt)
        self.assertIn("Mechanical Verification", prompt)
        self.assertIn(command, prompt)
        self.assertEqual(self.sb.names("done"), ["t"])

    def test_without_verify_the_reviewer_prompt_has_no_verification_section(self):
        self.sb.add_task("t")
        self.sb.run()

        self.assertNotIn("Mechanical Verification", self.sb.claude_calls("review")[0]["prompt"])

    def test_a_review_that_fails_a_verified_task_requeues_it_as_before(self):
        command = self.script("verify-ok.sh", "echo fine\n")
        self.sb.set_scenario(review=[{"result": "VERDICT: FAIL\n- criterion 2 is missing"}, PASS])
        self.sb.add_task("t", verify=command)
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        self.assertIn("criterion 2 is missing", self.sb.read_task("pending", "t")[1])

    def test_verify_runs_in_the_task_cwd(self):
        command = self.script("verify-cwd.sh", "pwd > where.txt\n")
        elsewhere = self.sb.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "scripts").mkdir()
        (elsewhere / "scripts" / "verify-cwd.sh").write_text("pwd > where.txt\n")
        self.sb.add_task("t", verify=command, cwd=str(elsewhere), review="skip")
        self.sb.run()

        self.assertEqual((elsewhere / "where.txt").read_text().strip(), str(elsewhere))
        self.assertFalse((self.cwd() / "where.txt").exists())

    def test_a_verify_that_never_finishes_is_stopped_and_fails_the_attempt(self):
        command = self.script("verify-hang.sh", "sleep 60\n")
        self.sb.configure(VERIFY_TIMEOUT_MINUTES="0.02")
        self.sb.add_task("t", verify=command)
        self.sb.run(timeout=30)

        self.assertEqual(self.roles(), ["worker"])
        meta, body = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "1")
        self.assertIn("did not finish", body)

    def test_a_verify_script_that_does_not_exist_fails_the_attempt_with_its_output(self):
        self.sb.add_task("t", verify="bash scripts/verify-missing.sh")
        self.sb.run()

        self.assertEqual(self.roles(), ["worker"])
        self.assertIn("exited 127", self.sb.read_task("pending", "t")[1])

    def test_the_command_sees_a_scrubbed_environment(self):
        command = self.script("verify-env.sh", "env > seen-env.txt\n")
        self.sb.add_task("t", verify=command, review="skip")
        self.sb.run()

        seen = (self.cwd() / "seen-env.txt").read_text()
        for name in ("FAKE_CLAUDE_SCENARIO", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            self.assertNotIn(name, seen)
        self.assertNotIn("test-token", seen)
        self.assertRegex(seen, r"(?m)^PATH=")

    def test_a_quoted_argument_keeps_its_closing_quote(self):
        command = self.script("verify-args.sh", 'printf "%s|" "$@" > args.txt\n') + ' "a b"'
        self.sb.add_task("t", verify=command, review="skip")
        self.sb.run()

        self.assertEqual((self.cwd() / "args.txt").read_text(), "a b|")

    def test_verify_output_is_kept_in_the_logs(self):
        command = self.script("verify-ok.sh", "echo evidence-line\n")
        self.sb.add_task("t", verify=command, review="skip")
        self.sb.run()

        log = (self.sb.base / "logs" / "t.attempt-1.verify.log").read_text()
        self.assertIn("evidence-line", log)

    def test_a_parked_review_runs_verify_again_and_still_shows_the_reviewer_the_output(self):
        command = self.script("verify-ok.sh", "echo '7 tests, 0 failures'\n")
        self.sb.set_scenario(review=[usage_limit(), PASS])
        self.sb.add_task("t", verify=command)
        self.sb.run()
        self.elapse_pause()
        self.sb.run()

        self.assertEqual(self.roles(), ["worker", "review", "review"])
        self.assertIn("7 tests, 0 failures", self.sb.claude_calls("review")[1]["prompt"])
        self.assertEqual(self.sb.names("done"), ["t"])


# ---------------------------------------------------------------- verify: the allowlist


class VerifyAllowlistTests(VerifyCase):
    def rejected(self, command, **extra):
        self.sb.add_task("t", verify=command, **extra)
        self.sb.run()
        self.assertEqual(self.sb.names("failed"), ["t"], command)
        self.assertEqual(self.sb.claude_calls(), [], "no claude call for an invalid task")
        self.assertFalse((self.cwd() / "marker").exists(), "the command never ran")
        return self.failure_reason("t")

    def test_a_command_off_the_allowlist_fails_the_task_with_a_reason_and_never_runs(self):
        reason = self.rejected("touch marker")

        self.assertIn("Invalid Task", reason)
        self.assertIn("touch marker", reason)
        self.assertIn("allowed prefix", reason)
        self.assertTrue(any("rejected as invalid" in text for text in self.sb.texts()))

    def test_a_shell_wrapper_is_not_on_the_allowlist(self):
        self.rejected("bash -c 'touch marker'")
        self.sb.run()  # nothing else happens

    def test_a_longer_name_does_not_ride_on_an_allowed_one(self):
        self.rejected("pytest-evil touch marker")

    def test_the_prefix_is_compared_word_by_word(self):
        self.rejected("python3 -m unittestx touch marker")
        self.rejected("python3 unittest")

    def test_a_script_outside_the_verify_prefix_is_refused(self):
        self.script("deploy.sh", "touch marker\n")
        self.rejected("bash scripts/deploy.sh")

    def test_chaining_is_just_arguments_because_no_shell_runs_it(self):
        self.script("verify-ok.sh", "echo \"$@\" > argv.txt\n")
        self.sb.add_task("t", verify="bash scripts/verify-ok.sh ; touch marker", review="skip")
        self.sb.run()

        self.assertFalse((self.cwd() / "marker").exists())
        self.assertEqual((self.cwd() / "argv.txt").read_text().split(), [";", "touch", "marker"])

    def test_unbalanced_quotes_are_rejected_cleanly(self):
        reason = self.rejected("bash scripts/verify-ok.sh 'oops")

        self.assertIn("cannot be parsed", reason)

    def test_an_empty_allowlist_runs_nothing(self):
        self.script("verify-ok.sh", "touch marker\n")
        self.sb.configure(VERIFY_ALLOWED_PREFIXES=",")  # an empty value means the default; "," means none
        self.rejected("bash scripts/verify-ok.sh")

    def test_the_allowlist_is_configurable(self):
        self.sb.configure(VERIFY_ALLOWED_PREFIXES="touch marker")
        self.sb.add_task("t", verify="touch marker", review="skip")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertTrue((self.cwd() / "marker").exists())

    def test_the_default_prefixes_are_unittest_pytest_and_verify_scripts(self):
        snippet = """
import json, sys, dispatcher
print(json.dumps({c: dispatcher.verify_problem(c) for c in json.loads(sys.argv[1])}))
"""
        commands = [
            "python3 -m unittest", "python3 -m unittest discover -s tests -v", "pytest", "pytest -q tests/",
            "bash scripts/verify-x.sh", "bash scripts/verify-x.sh 'a b'", "python3 -m pytest", "bash scripts/x.sh",
            "python3 -m unittestx", "pytest-evil", "bash",
        ]
        result = json.loads(self.sb.snippet(snippet, json.dumps(commands)))

        allowed = [c for c, problem in result.items() if problem is None]
        self.assertEqual(allowed, commands[:6])

    def test_an_allowed_command_runs_and_its_own_failure_fails_the_attempt(self):
        self.sb.add_task("t", verify="python3 -m unittest no_such_module", review="skip")
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"], "allowed, so it ran (and failed on its own)")
        self.assertIn("exited 1", self.sb.read_task("pending", "t")[1])
        self.assertEqual(self.roles(), ["worker"])

    def test_review_skip_without_verify_is_rejected(self):
        self.sb.add_task("t", review="skip")
        self.sb.run()

        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertIn("review: skip needs a verify", self.failure_reason("t"))

    def test_an_unknown_review_value_is_rejected(self):
        self.sb.add_task("t", verify="bash scripts/verify-ok.sh", review="maybe")
        self.sb.run()

        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertIn("review must be one of skip", self.failure_reason("t"))


# ---------------------------------------------------------------- frontmatter validation


class FrontmatterTests(VerifyCase):
    def test_an_unknown_key_fails_the_task_and_the_message_names_it(self):
        self.sb.add_task("t", colour="blue")
        self.sb.run()

        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertIn("unknown frontmatter key 'colour'", self.failure_reason("t"))
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertTrue(any("colour" in text for text in self.sb.texts()))

    def test_a_typo_of_a_known_key_says_which_key_was_meant(self):
        self.sb.add_task("t", valeu_class="deliverable")
        self.sb.run()

        reason = self.failure_reason("t")
        self.assertIn("unknown frontmatter key 'valeu_class'", reason)
        self.assertIn("did you mean 'value_class'", reason)

    def test_every_unknown_key_is_named(self):
        self.sb.add_task("t", colour="blue", shape="round")
        self.sb.run()

        reason = self.failure_reason("t")
        self.assertIn("'colour'", reason)
        self.assertIn("'shape'", reason)

    def test_value_class_deliverable_passes(self):
        self.sb.add_task("t", value_class="deliverable")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])

    def test_every_value_class_in_the_coordinators_memory_passes(self):
        for value in ("deliverable", "research", "verification", "admin"):
            self.sb.add_task(f"t-{value}", value_class=value)
        for _ in range(4):
            self.sb.run()

        self.assertEqual(len(self.sb.names("done")), 4)

    def test_a_misspelt_value_class_is_rejected_and_the_allowed_values_are_listed(self):
        self.sb.add_task("t", value_class="deliverble")
        self.sb.run()

        self.assertEqual(self.sb.names("failed"), ["t"])
        reason = self.failure_reason("t")
        self.assertIn("value_class must be one of", reason)
        self.assertIn("deliverable", reason)
        self.assertIn("'deliverble'", reason)
        self.assertEqual(self.sb.claude_calls(), [])

    def test_a_task_with_no_value_class_still_passes(self):
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])

    def test_bad_numbers_are_rejected(self):
        self.sb.add_task("a", max_attempts="three")
        self.sb.add_task("b", timeout_minutes="soon")
        self.sb.add_task("c", max_attempts="0")
        for _ in range(3):
            self.sb.run()

        self.assertEqual(self.sb.names("failed"), ["a", "b", "c"])
        self.assertIn("max_attempts must be an integer", self.failure_reason("a"))
        self.assertIn("timeout_minutes must be a number", self.failure_reason("b"))
        self.assertIn("max_attempts must be at least 1", self.failure_reason("c"))

    def test_a_schedule_key_on_a_plain_task_is_rejected(self):
        self.sb.add_task("t", schedule="daily at 06:30")
        self.sb.run()

        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertIn("only belongs in tasks/recurring/", self.failure_reason("t"))

    def test_the_keys_the_dispatcher_manages_are_accepted(self):
        self.sb.add_task("t", pending_review="logs/x.txt", review_failures="1")
        self.sb.run()

        self.assertEqual(self.sb.names("failed"), [])
        self.assertEqual(self.sb.names("done"), ["t"])

    def test_every_key_in_the_shipped_examples_is_known(self):
        shutil.copytree(REPO / "mcp", self.sb.base / "mcp")
        examples = sorted((REPO / "tasks" / "examples").glob("*.md"))
        self.assertTrue(examples)
        for path in examples:
            state = "recurring" if re.search(r"(?m)^schedule:", path.read_text()) else "pending"
            target = self.sb.base / "tasks" / state
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target / path.name)
            result = self.check(state, path.stem)
            self.assertEqual(result.returncode, 0, f"{path.name}\n{result.stdout}{result.stderr}")


# ---------------------------------------------------------------- recurring templates


class RecurringValidationTests(VerifyCase):
    MONTHLY = "monthly on the 1st at 08:00"

    def test_a_monthly_schedule_is_rejected_once_with_a_clear_message_and_never_spawns(self):
        self.sb.add_task("monthly-va-policy-delta", state="recurring", schedule=self.MONTHLY)
        for _ in range(5):
            self.sb.run()

        (text,) = self.sb.texts()
        self.assertIn("monthly-va-policy-delta", text)
        self.assertIn(self.MONTHLY, text)
        self.assertIn("weekly on mon at 09:00", text, "the supported forms are listed")
        self.assertIn("daily at 06:30", text)
        self.assertEqual(self.sb.find("monthly-va-policy-delta-[0-9]*"), [])
        self.assertEqual(self.sb.names("recurring"), ["monthly-va-policy-delta"], "left in place to fix")
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertEqual(sum(self.MONTHLY in line for line in self.sb.log_lines()), 1)

    def test_the_supported_forms_from_the_logs_are_accepted(self):
        self.sb.add_task("weekly-digest", state="recurring", schedule="weekly on mon at 09:00")
        self.sb.add_task("model-list", state="recurring", schedule="every 14d")
        self.sb.run()

        self.assertFalse([t for t in self.sb.texts() if "not scheduled" in t])
        self.assertEqual(self.sb.names("recurring"), ["model-list", "weekly-digest"])
        self.assertTrue(self.sb.find("model-list-[0-9]*"), "a valid schedule spawns as before")

    def test_a_template_with_an_unknown_key_is_reported_once_and_never_spawns(self):
        self.sb.add_task("nightly", state="recurring", schedule="every 1d", valeu_class="admin")
        for _ in range(3):
            self.sb.run()

        (text,) = self.sb.texts()
        self.assertIn("valeu_class", text)
        self.assertEqual(self.sb.find("nightly-[0-9]*"), [])

    def test_a_template_with_a_bad_value_class_never_spawns(self):
        self.sb.add_task("nightly", state="recurring", schedule="every 1d", value_class="deliverble")
        self.sb.run()

        self.assertEqual(self.sb.find("nightly-[0-9]*"), [])
        self.assertIn("value_class", self.sb.texts()[0])

    def test_a_template_with_a_good_value_class_spawns_an_instance_without_schedule_keys(self):
        self.sb.add_task("nightly", state="recurring", schedule="every 1d", value_class="admin")
        self.sb.run()

        ((state, stem),) = self.sb.find("nightly-[0-9]*")
        self.assertEqual(state, "done")
        meta, _ = self.sb.read_task("done", stem)
        self.assertEqual(meta["value_class"], "admin")
        self.assertNotIn("schedule", meta)

    def test_a_template_is_checked_for_a_verify_command_too(self):
        self.sb.add_task("nightly", state="recurring", schedule="every 1d", verify="touch marker")
        self.sb.run()

        self.assertEqual(self.sb.find("nightly-[0-9]*"), [])
        self.assertIn("allowed prefix", self.sb.texts()[0])


# ---------------------------------------------------------------- dispatcher.py check


class CheckCommandTests(VerifyCase):
    def test_a_good_task_prints_ok_and_exits_0(self):
        self.sb.add_task("t", value_class="research")
        result = self.check("pending", "t")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)

    def test_a_bad_task_lists_every_problem_and_exits_1(self):
        self.sb.add_task("t", valeu_class="research", verify="touch marker")
        result = self.check("pending", "t")

        self.assertEqual(result.returncode, 1)
        self.assertIn("valeu_class", result.stdout)
        self.assertIn("touch marker", result.stdout)
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertFalse((self.cwd() / "marker").exists())

    def test_a_template_in_recurring_is_checked_as_a_template(self):
        self.sb.add_task("m", state="recurring", schedule=RecurringValidationTests.MONTHLY)
        result = self.check("recurring", "m")

        self.assertEqual(result.returncode, 1)
        self.assertIn(RecurringValidationTests.MONTHLY, result.stdout)

    def test_a_good_template_passes(self):
        self.sb.add_task("m", state="recurring", schedule="weekly on mon at 09:00")

        self.assertEqual(self.check("recurring", "m").returncode, 0)

    def test_a_missing_file_exits_1(self):
        result = subprocess.run(
            [sys.executable, str(self.sb.base / "dispatcher.py"), "check", "nope.md"],
            cwd=self.sb.base, env=self.sb.env(), capture_output=True, text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("no such file", result.stdout)

    def test_the_coordinator_may_run_it(self):
        snippet = "import coordinator_bot as bot\nprint(bot.default_allowed_tools())"
        tools = self.sb.snippet(snippet)

        self.assertRegex(tools, r"Bash\(python3 \S+/dispatcher\.py check:\*\)")


# ---------------------------------------------------------------- the instructions


class InstructionTests(VerifyCase):
    def prompts(self):
        self.sb.add_task("t")
        self.sb.run()
        return self.sb.claude_calls("worker")[0]["prompt"], self.sb.claude_calls("review")[0]["prompt"]

    def test_the_worker_is_told_what_the_reviewer_cannot_do_and_what_to_paste(self):
        worker, _ = self.prompts()

        self.assertRegex(worker, r"(?s)reviewer has no shell, no GitHub CLI and no network")
        self.assertRegex(worker, r"(?s)raw output\s+verbatim")
        self.assertRegex(worker, r"(?s)URL and its rendered\s+body")
        self.assertRegex(worker, r"(?s)one at a time.*`&&`, `;`, a pipe or\s+`2>&1`.*denied")
        self.assertTrue(worker.startswith("You are running unattended"))

    def test_the_reviewer_is_told_pasted_output_is_evidence(self):
        _, review = self.prompts()

        self.assertRegex(review, r"(?s)pasted in the worker's report.*is evidence")
        self.assertRegex(review, r"(?s)only when the\s+evidence for it is missing or contradictory")
        self.assertIn("strict automated reviewer", review)

    def test_the_coordinator_is_taught_verify_review_skip_schedules_and_check(self):
        text = (REPO / "coordinator" / "CLAUDE.md").read_text()

        self.assertIn("`verify:`", text)
        self.assertIn("`review: skip`", text)
        self.assertIn("VERIFY_ALLOWED_PREFIXES", text)
        for form in ("every 14d", "daily at 06:30", "weekly on mon at 09:00"):
            self.assertIn(form, text)
        self.assertRegex(text, r"(?s)monthly")
        self.assertIn("dispatcher.py check <file>", text)
        self.assertIn("`value_class`", text)
        for value in ("deliverable", "research", "verification", "admin"):
            self.assertIn(value, text)

    def test_the_settings_are_documented_with_their_defaults(self):
        text = (REPO / ".env.example").read_text()

        self.assertRegex(text, r"(?m)^VERIFY_ALLOWED_PREFIXES=python3 -m unittest,pytest,bash scripts/verify-$")
        self.assertRegex(text, r"(?m)^VERIFY_TIMEOUT_MINUTES=10$")

    def test_the_readme_explains_verify(self):
        text = (REPO / "README.md").read_text()

        self.assertIn("### Verifying with a command", text)
        self.assertIn("review: skip", text)
        self.assertIn("monthly on the 1st at 08:00", text)

    def test_the_usage_text_lists_check(self):
        result = subprocess.run(
            [sys.executable, str(self.sb.base / "dispatcher.py"), "bogus"],
            cwd=self.sb.base, env=self.sb.env(), capture_output=True, text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("check <file>", result.stderr)


if __name__ == "__main__":
    unittest.main()
