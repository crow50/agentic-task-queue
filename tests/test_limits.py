"""Phase 1 (PLAN.md): stop wasting limit.

Written before the code. Each test runs dispatcher.py as a subprocess in the
same Sandbox the baseline tests use, with the fake claude and fake Telegram,
and drives the failure modes the plan lists: a review that cannot run, a usage
limit, an expired login, a bad recurring schedule, the daily attempt cap.
Stdlib only.
"""

import html
import json
import re
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from test_baseline import DEFAULT_BODY, PASS, Sandbox, fail

UTC = timezone.utc
FIXTURE_ENVELOPE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "envelope-success.json").read_text()
)

AUTH_EXPIRED = {"fixture": "auth-expired.txt", "exit": 1}
BOOM = {"exit": 1, "stdout": "", "stderr": "boom: not a limit and not a login problem"}
RATE_429 = {"exit": 1, "stdout": "", "stderr": "API Error: 429 rate_limit_error: slow down"}


def usage_limit(reset_in=None):
    """A failed claude call that says the usage limit was hit (optionally with a reset epoch)."""
    stderr = "Claude AI usage limit reached"
    if reset_in is not None:
        stderr += f"|{int(time.time() + reset_in)}"
    return {"exit": 1, "stdout": "", "stderr": stderr}


def iso(moment):
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    return datetime.strptime(text.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class LimitsSandbox(Sandbox):
    def state_path(self, name):
        return self.base / "state" / name

    def write_state(self, name, text):
        path = self.state_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def write_pause(self, until, reason="usage limit"):
        self.write_state("paused_until", f"{iso(until)}\n{reason}\n")

    def read_pause(self):
        path = self.state_path("paused_until")
        return parse_iso(path.read_text().splitlines()[0]) if path.exists() else None

    def log_lines(self):
        path = self.base / "logs" / "dispatcher.log"
        return path.read_text().splitlines() if path.exists() else []

    def usage(self):
        path = self.base / "logs" / "usage.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def write_usage(self, lines):
        path = self.base / "logs" / "usage.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    def add_bare_task(self, name, state="pending", body=DEFAULT_BODY, **meta):
        """A task file with exactly the frontmatter given (no default escalation model)."""
        frontmatter = {"model": "fake-worker", "review_model": "fake-review",
                       "max_attempts": "3", "attempts": "0"}
        frontmatter.update(meta)
        front = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
        directory = self.base / "tasks" / state
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.md").write_text(f"---\n{front}\n---\n{body}")

    def texts(self):
        return [html.unescape(t) for t in self.telegram.texts()]

    def snippet(self, code, *args):
        proc = self.python("-c", code, *args)
        if proc.returncode != 0:
            raise AssertionError(f"snippet failed\n{proc.stderr}")
        return proc.stdout


class LimitsCase(unittest.TestCase):
    def setUp(self):
        self.sb = LimitsSandbox()
        self.addCleanup(self.sb.close)

    def roles(self):
        return [c["role"] for c in self.sb.claude_calls()]

    def models(self):
        return [c["model"] for c in self.sb.claude_calls()]

    def elapse_pause(self):
        self.sb.write_pause(datetime.now(UTC) - timedelta(minutes=1))


# ---------------------------------------------------------------- 1. review can't run


class ReviewCannotRunTests(LimitsCase):
    def test_a_review_usage_limit_parks_the_worker_report_and_charges_nothing(self):
        self.sb.set_scenario(worker=[{"result": "Wrote report.md"}], review=[usage_limit(), PASS])
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        for state in ("active", "done", "failed"):
            self.assertEqual(self.sb.names(state), [], state)
        meta, _ = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "0")
        parked = self.sb.base / meta["pending_review"]
        self.assertEqual(parked.name, "t.attempt-1.worker.txt")
        self.assertEqual(parked.read_text().strip(), "Wrote report.md")
        self.assertIsNotNone(self.sb.read_pause(), "a usage limit in the review pauses the queue too")

    def test_the_next_run_makes_one_review_call_and_no_worker_call(self):
        self.sb.set_scenario(worker=[{"result": "Wrote report.md"}], review=[usage_limit(), PASS])
        self.sb.add_task("t")
        self.sb.run()
        self.elapse_pause()
        self.sb.run()

        self.assertEqual(self.roles(), ["worker", "review", "review"])
        self.assertIn("Wrote report.md", self.sb.claude_calls()[2]["prompt"])
        self.assertEqual(self.sb.names("done"), ["t"])
        meta, body = self.sb.read_task("done", "t")
        self.assertEqual(meta["attempts"], "1")
        self.assertNotIn("pending_review", meta)
        self.assertIn("## Result (attempt 1,", body)
        self.assertIn("Wrote report.md", body)

    def test_a_review_error_that_is_not_a_limit_parks_without_pausing(self):
        self.sb.set_scenario(worker=[{"result": "Wrote report.md"}], review=[BOOM, PASS])
        self.sb.add_task("t")
        self.sb.run()

        meta, _ = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "0")
        self.assertIn("pending_review", meta)
        self.assertIsNone(self.sb.read_pause())
        self.assertEqual(self.sb.telegram.texts(), [])

        self.sb.run()
        self.assertEqual(self.roles(), ["worker", "review", "review"])
        self.assertEqual(self.sb.names("done"), ["t"])

    def test_a_review_that_cannot_log_in_parks_and_stops_the_queue(self):
        self.sb.set_scenario(worker=[{"result": "Wrote report.md"}], review=[AUTH_EXPIRED],
                             other=[AUTH_EXPIRED])
        self.sb.add_task("t")
        self.sb.run()

        meta, _ = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "0")
        self.assertIn("pending_review", meta)
        self.assertTrue(self.sb.state_path("auth_failed").exists())

    def test_a_review_that_keeps_failing_is_eventually_charged_as_a_failed_attempt(self):
        self.sb.set_scenario(worker=[{"result": "Wrote report.md"}], review=[BOOM])
        self.sb.add_task("t")
        for _ in range(3):
            self.sb.run()

        self.assertEqual(len(self.sb.claude_calls("worker")), 1, "the parked report is never redone")
        self.assertEqual(len(self.sb.claude_calls("review")), 3)
        meta, body = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "1")
        self.assertNotIn("pending_review", meta)
        self.assertIn("## Attempt 1 Feedback", body)
        self.assertIn("Review could not be completed", body)

    def test_a_missing_parked_report_falls_back_to_running_the_worker(self):
        self.sb.add_task("t", pending_review="logs/gone.worker.txt")
        self.sb.run()

        self.assertEqual(self.roles(), ["worker", "review"])
        self.assertEqual(self.sb.names("done"), ["t"])

    def test_a_parked_report_outside_logs_is_never_read(self):
        # The path comes from a task file a coordinator wrote: it must not turn the
        # reviewer, the task file and Telegram into a way to read arbitrary files.
        secret = self.sb.root / "secret.txt"
        secret.write_text("TOP-SECRET-CONTENT")
        (self.sb.base / "logs").mkdir(exist_ok=True)
        (self.sb.base / "logs" / "link.worker.txt").symlink_to(secret)
        for ref in ("../secret.txt", str(secret), "logs/link.worker.txt"):
            with self.subTest(ref=ref):
                self.sb.add_task("t", pending_review=ref)
                self.sb.run()
                self.assertEqual(self.sb.names("done"), ["t"])
                for call in self.sb.claude_calls():
                    self.assertNotIn("TOP-SECRET-CONTENT", call["prompt"])
                self.assertNotIn("TOP-SECRET-CONTENT", " ".join(self.sb.texts()))
                self.assertNotIn("TOP-SECRET-CONTENT", self.sb.read_task("done", "t")[1])
                (self.sb.base / "tasks" / "done" / "t.md").unlink()

    def test_retry_forgets_a_parked_review(self):
        self.sb.add_task("t", state="failed", attempts="3",
                         pending_review="logs/t.attempt-3.worker.txt", review_failures="2")
        self.sb.run("retry", "t")

        meta, _ = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "0")
        self.assertNotIn("pending_review", meta)
        self.assertNotIn("review_failures", meta)


# ---------------------------------------------------------------- 2. pause on a usage limit


class PauseTests(LimitsCase):
    def test_a_worker_usage_limit_pauses_until_the_reset_time_without_inline_retries(self):
        self.sb.configure(MAX_RATE_LIMIT_RETRIES="2")
        reset = time.time() + 2 * 3600
        self.sb.set_scenario(worker=[usage_limit(2 * 3600)])
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(len(self.sb.claude_calls("worker")), 1, "waiting minutes for an hours-long reset is pointless")
        self.assertEqual(self.sb.read_task("pending", "t")[0]["attempts"], "0")
        until = self.sb.read_pause()
        self.assertTrue(reset <= until.timestamp() <= reset + 300, until)
        (text,) = self.sb.texts()
        self.assertIn("paused until", text.lower())

    def test_a_paused_run_makes_no_calls_and_logs_one_line(self):
        self.sb.set_scenario(worker=[usage_limit(2 * 3600)])
        self.sb.add_task("t")
        self.sb.run()
        calls, notices, log_before = len(self.sb.claude_calls()), len(self.sb.texts()), self.sb.log_lines()

        self.sb.run()

        new = self.sb.log_lines()[len(log_before):]
        self.assertEqual(len(new), 1, new)
        self.assertIn("paused", new[0].lower())
        self.assertEqual(len(self.sb.claude_calls()), calls)
        self.assertEqual(len(self.sb.texts()), notices, "no second notice")
        self.assertEqual(self.sb.names("pending"), ["t"])

    def test_transient_errors_get_two_inline_retries_by_default(self):
        self.sb.configure(MAX_RATE_LIMIT_RETRIES=None, RATE_LIMIT_BASE_DELAY="0")
        self.sb.set_scenario(worker=[RATE_429])
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(len(self.sb.claude_calls("worker")), 3)  # first try + 2 retries
        self.assertEqual(self.sb.read_task("pending", "t")[0]["attempts"], "0")
        minutes = (self.sb.read_pause() - datetime.now(UTC)).total_seconds() / 60
        self.assertTrue(58 <= minutes <= 62, f"default cooldown is 60 minutes, got {minutes:.1f}")

    def test_the_cooldown_setting_is_used_when_the_message_has_no_reset_time(self):
        self.sb.configure(USAGE_LIMIT_COOLDOWN_MINUTES="2")
        self.sb.set_scenario(worker=[usage_limit()])
        self.sb.add_task("t")
        self.sb.run()

        seconds = (self.sb.read_pause() - datetime.now(UTC)).total_seconds()
        self.assertTrue(90 <= seconds <= 150, seconds)

    def test_a_pause_in_the_past_lets_the_run_proceed_and_announces_the_resume(self):
        self.elapse_pause()
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertIsNone(self.sb.read_pause(), "the expired pause file is removed")
        texts = self.sb.texts()
        self.assertEqual(len(texts), 2, texts)
        self.assertTrue(any("resumed" in t.lower() for t in texts), texts)
        self.assertTrue(any("Task done: t.md" in t for t in texts), texts)

    def test_a_pause_in_the_future_stops_the_run_before_anything_moves(self):
        self.sb.write_pause(datetime.now(UTC) + timedelta(minutes=30))
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.claude_calls(), [])
        self.assertEqual(self.sb.names("pending"), ["t"])
        self.assertEqual(self.sb.telegram.texts(), [])

    def test_an_unreadable_pause_file_never_blocks_the_queue(self):
        self.sb.write_state("paused_until", "soon\n")
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertFalse(self.sb.state_path("paused_until").exists())


class ResetTimeTests(LimitsCase):
    SNIPPET = """
import json, os, sys, time
os.environ["TZ"] = "UTC"
time.tzset()
from datetime import datetime, timezone
import dispatcher
now = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)
out = {}
for text in json.loads(sys.argv[1]):
    parsed = dispatcher.parse_reset_time(text, now)
    out[text] = parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else None
print(json.dumps(out))
"""

    def parse(self, *texts):
        return json.loads(self.sb.snippet(self.SNIPPET, json.dumps(list(texts))))

    def test_epoch_after_a_pipe(self):
        epoch = int(datetime(2026, 10, 4, 12, 0, tzinfo=UTC).timestamp())
        self.assertEqual(self.parse(f"Claude AI usage limit reached|{epoch}"),
                         {f"Claude AI usage limit reached|{epoch}": "2026-10-04T12:00:00Z"})

    def test_clock_time_today_or_tomorrow(self):
        got = self.parse("5-hour limit reached ∙ resets 3pm", "limit reached, resets at 9:30 AM")
        self.assertEqual(list(got.values()), ["2026-10-04T15:00:00Z", "2026-10-05T09:30:00Z"])

    def test_clock_time_in_a_named_zone(self):
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("America/Los_Angeles")
        except Exception:
            self.skipTest("no tz database on this machine")
        got = self.parse("limit reached ∙ resets 3pm (America/Los_Angeles)")
        self.assertEqual(list(got.values()), ["2026-10-04T22:00:00Z"])  # PDT is UTC-7

    def test_an_unknown_zone_falls_back_to_local_time(self):
        got = self.parse("limit reached ∙ resets 3pm (Not/A_Zone)")
        self.assertEqual(list(got.values()), ["2026-10-04T15:00:00Z"])  # the snippet's local zone is UTC

    def test_relative_wait(self):
        got = self.parse("Please try again in 45 minutes.", "try again in 2 hours")
        self.assertEqual(list(got.values()), ["2026-10-04T10:45:00Z", "2026-10-04T12:00:00Z"])

    def test_nothing_to_parse_means_none(self):
        far = int(datetime(2026, 12, 4, tzinfo=UTC).timestamp())
        past = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())
        got = self.parse("rate limit exceeded", "", f"usage limit reached|{far}",
                         f"usage limit reached|{past}", "try again in 999 hours")
        self.assertEqual(set(got.values()), {None})


# ---------------------------------------------------------------- 3. expired login


class AuthTests(LimitsCase):
    SNIPPET = """
import json, sys
import dispatcher
print(json.dumps([bool(dispatcher.AUTH_ERROR_RE.search(t)) for t in json.loads(sys.argv[1])]))
"""

    def test_pattern_matches_the_real_expiry_text_and_the_other_spellings(self):
        real = (Path(__file__).resolve().parent / "fixtures" / "auth-expired.txt").read_text()
        texts = [real, "Failed to authenticate", "OAuth session expired", "session expired",
                 "401 Unauthorized", "Not logged in", "Invalid API key", "Done."]
        got = json.loads(self.sb.snippet(self.SNIPPET, json.dumps(texts)))
        self.assertEqual(got, [True] * 7 + [False])

    def test_an_expired_login_stops_the_queue_with_one_notice_and_no_escalation(self):
        self.sb.set_scenario(worker=[AUTH_EXPIRED], other=[AUTH_EXPIRED])
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        self.assertEqual(self.sb.read_task("pending", "t")[0]["attempts"], "0")
        self.assertEqual(self.roles(), ["worker"])
        self.assertTrue(self.sb.state_path("auth_failed").exists())
        (text,) = self.sb.texts()
        self.assertIn("login expired", text.lower())

    def test_later_runs_make_one_cheap_probe_and_say_nothing_more(self):
        self.sb.set_scenario(worker=[AUTH_EXPIRED], other=[AUTH_EXPIRED])
        self.sb.add_task("t")
        self.sb.run()
        for _ in range(3):
            self.sb.run()

        self.assertEqual(self.roles(), ["worker", "other", "other", "other"])
        self.assertEqual(set(self.models()) - {"fake-worker"}, {self.models()[1]})
        self.assertNotIn("fake-escalated", self.models(), "an auth error never escalates")
        self.assertEqual(self.sb.read_task("pending", "t")[0]["attempts"], "0")
        self.assertEqual(len(self.sb.texts()), 1, "the expiry is reported once")
        self.assertEqual(sum("login" in line.lower() for line in self.sb.log_lines()), 1)

    def test_a_probe_that_succeeds_clears_the_stop_and_the_queue_runs(self):
        self.sb.set_scenario(worker=[AUTH_EXPIRED, {"result": "Wrote it"}], other=[{"result": "OK"}])
        self.sb.add_task("t")
        self.sb.run()
        self.assertTrue(self.sb.state_path("auth_failed").exists())
        self.sb.run()

        self.assertFalse(self.sb.state_path("auth_failed").exists())
        self.assertEqual(self.roles(), ["worker", "other", "worker", "review"])
        self.assertEqual(self.sb.names("done"), ["t"])
        texts = self.sb.texts()
        self.assertEqual(len(texts), 3, texts)  # expired, restored, done
        self.assertIn("restored", texts[1].lower())

    def test_a_second_expiry_after_a_recovery_is_reported_again(self):
        self.sb.set_scenario(worker=[AUTH_EXPIRED, {"result": "ok"}, AUTH_EXPIRED], other=[{"result": "OK"}])
        self.sb.add_task("a")
        self.sb.run()  # expired
        self.sb.run()  # probe ok, task a done
        self.sb.add_task("b")
        self.sb.run()  # expired again
        self.assertEqual(sum("login expired" in t.lower() for t in self.sb.texts()), 2)

    def test_a_successful_report_that_mentions_authentication_is_not_an_auth_error(self):
        report = "Fixed the OAuth session expired bug; unauthorized calls now get 401; authentication works."
        self.sb.set_scenario(worker=[{"result": report}], review=[{"result": "VERDICT: PASS\nAuthentication ok"}])
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertFalse(self.sb.state_path("auth_failed").exists())

    def test_an_error_envelope_with_exit_zero_still_counts_as_a_failed_call(self):
        step = {"envelope": {"is_error": True}, "result": "Failed to authenticate: OAuth session expired"}
        self.sb.set_scenario(worker=[step], other=[AUTH_EXPIRED])
        self.sb.add_task("t")
        self.sb.run()

        self.assertTrue(self.sb.state_path("auth_failed").exists())
        self.assertEqual(self.sb.read_task("pending", "t")[0]["attempts"], "0")


# ---------------------------------------------------------------- 4. real limit wording


class FailedCallsLogTests(LimitsCase):
    def test_every_failed_call_is_written_with_its_stdout_stderr_and_exit_code(self):
        self.sb.configure(MAX_RATE_LIMIT_RETRIES="2")
        step = {"exit": 3, "stdout": "partial stdout text", "stderr": "API Error: 429 rate limited, stderr text"}
        self.sb.set_scenario(worker=[step])
        self.sb.add_task("t")
        self.sb.run()

        text = (self.sb.base / "logs" / "failed-calls.log").read_text()
        self.assertEqual(len(re.findall(r"^=== ", text, re.M)), 3, "first try and two retries")
        self.assertIn("partial stdout text", text)
        self.assertIn("API Error: 429 rate limited, stderr text", text)
        self.assertIn("exit=3", text)
        self.assertIn("worker", text)

    def test_successful_calls_are_not_written(self):
        self.sb.add_task("t")
        self.sb.run()
        self.assertFalse((self.sb.base / "logs" / "failed-calls.log").exists())


# ---------------------------------------------------------------- 6. usage log


USAGE_KEYS = {"time", "label", "model", "exit", "status", "total_cost_usd", "num_turns", "duration_ms",
              "usage", "modelUsage", "permission_denials", "terminal_reason", "is_error"}


class UsageLogTests(LimitsCase):
    def test_a_completed_task_adds_two_lines_with_the_envelope_fields(self):
        self.sb.add_task("t")
        self.sb.run()

        worker, review = self.sb.usage()
        self.assertEqual((worker["label"], review["label"]), ("worker", "review"))
        self.assertEqual((worker["model"], review["model"]), ("fake-worker", "fake-review"))
        for line in (worker, review):
            self.assertEqual(set(line), USAGE_KEYS)
            self.assertEqual(line["exit"], 0)
            self.assertFalse(line["is_error"])
            self.assertEqual(line["total_cost_usd"], FIXTURE_ENVELOPE["total_cost_usd"])
            self.assertEqual(line["num_turns"], FIXTURE_ENVELOPE["num_turns"])
            self.assertEqual(line["modelUsage"], FIXTURE_ENVELOPE["modelUsage"])
            self.assertEqual(line["usage"], FIXTURE_ENVELOPE["usage"])
            parse_iso(line["time"])

    def test_two_tasks_make_four_lines(self):
        self.sb.add_task("a")
        self.sb.add_task("b")
        self.sb.run()
        self.sb.run()
        self.assertEqual([u["label"] for u in self.sb.usage()], ["worker", "review"] * 2)

    def test_a_failed_call_is_recorded_without_envelope_fields(self):
        self.sb.set_scenario(worker=[BOOM])
        self.sb.add_task("t")
        self.sb.run()

        (line,) = [u for u in self.sb.usage() if u["label"] == "worker"]
        self.assertEqual(line["exit"], 1)
        self.assertTrue(line["is_error"])
        self.assertIsNone(line["total_cost_usd"])
        self.assertEqual(line["status"], "error")

    def test_the_status_says_why_a_call_failed(self):
        self.sb.set_scenario(worker=[usage_limit(3600)])
        self.sb.add_task("t")
        self.sb.run()
        self.assertEqual([u["status"] for u in self.sb.usage()], ["rate_limited"])

    def test_the_usage_command_prints_totals_by_day_and_model(self):
        today = datetime.now(UTC)
        earlier = today - timedelta(hours=36)

        def line(when, label, model, exit_code, cost, turns, model_usage):
            return {"time": iso(when), "label": label, "model": model, "exit": exit_code,
                    "status": "ok" if exit_code == 0 else "error", "total_cost_usd": cost,
                    "num_turns": turns, "duration_ms": 1000, "usage": {}, "modelUsage": model_usage,
                    "permission_denials": [], "terminal_reason": "completed", "is_error": exit_code != 0}

        self.sb.write_usage([
            line(earlier, "worker", "m-sonnet", 0, 1.00, 10, {
                "m-sonnet": {"costUSD": 0.75, "webSearchRequests": 0},
                "m-haiku": {"costUSD": 0.25, "webSearchRequests": 3}}),
            line(earlier, "review", "m-haiku", 0, 0.05, 1, {"m-haiku": {"costUSD": 0.05, "webSearchRequests": 0}}),
            line(today, "worker", "m-sonnet", 0, 0.50, 5, {"m-sonnet": {"costUSD": 0.50, "webSearchRequests": 0}}),
            line(today, "worker", "m-sonnet", 1, None, None, {}),
        ])
        out = self.sb.run("usage").stdout

        rows = {tuple(r[:2]): r[2:] for r in (text.split() for text in out.splitlines()) if len(r) == 7}
        day = lambda moment: moment.astimezone().strftime("%Y-%m-%d")  # noqa: E731
        # columns: calls errors cost_usd turns web_searches
        self.assertEqual(rows[(day(earlier), "m-sonnet")], ["1", "0", "0.7500", "10", "0"])
        self.assertEqual(rows[(day(earlier), "m-haiku")], ["1", "0", "0.3000", "1", "3"])
        self.assertEqual(rows[(day(today), "m-sonnet")], ["2", "1", "0.5000", "5", "0"])
        self.assertEqual(rows[("total", "all")], ["4", "1", "1.5500", "16", "3"])

    def test_the_usage_command_copes_with_no_log(self):
        proc = self.sb.run("usage")
        self.assertIn("no usage", proc.stdout.lower())


class PermissionDenialTests(LimitsCase):
    DENIALS = [
        {"tool_name": "Bash", "tool_use_id": "a", "tool_input": {"command": "git push origin main"}},
        {"tool_name": "Bash", "tool_use_id": "b", "tool_input": {"command": "cd sub && ls"}},
    ]

    def test_denied_commands_are_named_in_the_done_report(self):
        self.sb.set_scenario(worker=[{"result": "Pushed it.", "envelope": {"permission_denials": self.DENIALS}}])
        self.sb.add_task("t")
        self.sb.run()

        (text,) = self.sb.texts()
        self.assertIn("denied", text.lower())
        self.assertIn("git push origin main", text)
        self.assertIn("cd sub && ls", text)
        self.assertEqual(self.sb.usage()[0]["permission_denials"], self.DENIALS)

    def test_a_clean_run_has_no_denial_line(self):
        self.sb.add_task("t")
        self.sb.run()
        (text,) = self.sb.texts()
        self.assertNotIn("denied", text.lower())

    def test_denials_survive_a_parked_review(self):
        self.sb.set_scenario(
            worker=[{"result": "Pushed it.", "envelope": {"permission_denials": self.DENIALS}}],
            review=[BOOM, PASS],
        )
        self.sb.add_task("t")
        self.sb.run()
        self.sb.run()

        (text,) = self.sb.texts()
        self.assertIn("git push origin main", text)


# ---------------------------------------------------------------- 7. say each problem once


class SayItOnceTests(LimitsCase):
    BAD = "monthly on the 1st at 08:00"

    def schedule_lines(self):
        return [line for line in self.sb.log_lines() if self.BAD in line]

    def test_a_bad_schedule_run_ten_times_gives_one_log_line_and_one_message(self):
        self.sb.add_task("monthly-report", state="recurring", schedule=self.BAD)
        for _ in range(10):
            self.sb.run()

        self.assertEqual(len(self.schedule_lines()), 1, self.schedule_lines())
        (text,) = self.sb.texts()
        self.assertIn("monthly-report", text)
        self.assertIn(self.BAD, text)
        self.assertEqual(self.sb.find("monthly-report-[0-9]*"), [], "it never spawns")
        self.assertEqual(self.sb.claude_calls(), [])

    def test_a_different_bad_schedule_is_a_new_condition(self):
        self.sb.add_task("monthly-report", state="recurring", schedule=self.BAD)
        self.sb.run()
        self.sb.run()
        self.sb.add_task("monthly-report", state="recurring", schedule="fortnightly")
        self.sb.run()
        self.sb.run()
        self.assertEqual(len(self.sb.texts()), 2)

    def test_the_same_bad_schedule_after_a_fix_is_reported_again(self):
        self.sb.add_task("monthly-report", state="recurring", schedule=self.BAD)
        self.sb.run()
        self.sb.add_task("monthly-report", state="recurring", schedule="every 1d")
        self.sb.run()  # spawns, runs, passes
        self.sb.add_task("monthly-report", state="recurring", schedule=self.BAD)
        self.sb.run()
        self.assertEqual(sum(self.BAD in t for t in self.sb.texts()), 2)

    def test_a_dependency_on_a_cancelled_task_is_reported_once(self):
        self.sb.add_task("dep", state="cancelled")
        self.sb.add_task("child", depends_on="dep")
        for _ in range(5):
            self.sb.run()

        lines = [t for t in self.sb.log_lines() if "child.md" in t and "which was cancelled" in t]
        self.assertEqual(len(lines), 1, lines)
        (text,) = self.sb.texts()
        self.assertIn("child.md", text)
        self.assertIn("dep", text)
        self.assertEqual(self.sb.names("pending"), ["child"])

    def test_a_task_that_depends_on_a_cancelled_task_does_not_block_others(self):
        self.sb.add_task("dep", state="cancelled")
        self.sb.add_task("child", depends_on="dep")
        self.sb.add_task("other")
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["other"])

    def test_condition_state_lives_in_state_seen_json(self):
        self.sb.add_task("monthly-report", state="recurring", schedule=self.BAD)
        self.sb.run()
        seen = json.loads(self.sb.state_path("seen.json").read_text())
        self.assertEqual(len(seen), 1)


# ---------------------------------------------------------------- Pro plan limits


class EscalationTests(LimitsCase):
    def second_attempt_model(self, **meta):
        self.sb.set_scenario(review=[fail("again"), PASS])
        self.sb.add_bare_task("t", **meta)
        self.sb.run()
        self.sb.run()
        workers = self.sb.claude_calls("worker")
        self.assertEqual(len(workers), 2)
        return workers[1]["model"]

    def test_sonnet_is_the_default_escalation(self):
        self.assertEqual(self.second_attempt_model(), "claude-sonnet-5")

    def test_the_default_is_configurable(self):
        self.sb.configure(DEFAULT_ESCALATION_MODEL="my-sonnet")
        self.assertEqual(self.second_attempt_model(), "my-sonnet")

    def test_a_task_that_names_opus_gets_opus(self):
        self.assertEqual(self.second_attempt_model(escalation_model="claude-opus-4-8"), "claude-opus-4-8")

    def test_a_task_already_on_opus_is_not_downgraded(self):
        self.assertEqual(self.second_attempt_model(model="claude-opus-4-8"), "claude-opus-4-8")


class DailyCapTests(LimitsCase):
    def worker_line(self, when=None, label="worker", status="ok"):
        when = when or datetime.now(UTC)
        return {"time": iso(when), "label": label, "model": "fake-worker", "exit": 0, "status": status,
                "total_cost_usd": 0.1, "num_turns": 1, "duration_ms": 1, "usage": {}, "modelUsage": {},
                "permission_denials": [], "terminal_reason": "completed", "is_error": False}

    def test_at_the_cap_the_queue_pauses_until_midnight_with_one_notice(self):
        self.sb.configure(MAX_ATTEMPTS_PER_DAY="3")
        self.sb.write_usage([self.worker_line() for _ in range(3)])
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.claude_calls(), [])
        self.assertEqual(self.sb.names("pending"), ["t"])
        until = self.sb.read_pause().astimezone()
        self.assertEqual((until.hour, until.minute), (0, 0))
        self.assertTrue(timedelta(0) < until - datetime.now(UTC) <= timedelta(hours=26))
        (text,) = self.sb.texts()
        self.assertIn("daily", text.lower())
        self.sb.run()
        self.assertEqual(len(self.sb.texts()), 1, "one notice, not one per run")

    def test_below_the_cap_the_queue_runs(self):
        self.sb.configure(MAX_ATTEMPTS_PER_DAY="3")
        self.sb.write_usage([self.worker_line() for _ in range(2)])
        self.sb.add_task("t")
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["t"])

    def test_only_todays_worker_attempts_count(self):
        self.sb.configure(MAX_ATTEMPTS_PER_DAY="3")
        yesterday = datetime.now(UTC) - timedelta(hours=36)
        self.sb.write_usage(
            [self.worker_line(yesterday) for _ in range(5)]  # another day
            + [self.worker_line(label="review") for _ in range(5)]  # reviews are not attempts
            + [self.worker_line(status="rate_limited") for _ in range(5)]  # a limit hit costs no attempt
            + [self.worker_line(status="auth") for _ in range(5)]
            + [self.worker_line()]
        )
        self.sb.add_task("t")
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["t"])

    def test_the_default_cap_is_twelve(self):
        self.sb.write_usage([self.worker_line() for _ in range(11)])
        self.sb.add_task("a")
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["a"])  # that run made the 12th attempt
        self.sb.add_task("b")
        self.sb.run()
        self.assertEqual(self.sb.names("pending"), ["b"])
        self.assertIsNotNone(self.sb.read_pause())

    def test_real_attempts_reach_the_cap(self):
        self.sb.configure(MAX_ATTEMPTS_PER_DAY="2")
        self.sb.set_scenario(review=[fail("no")])
        self.sb.add_task("t", max_attempts="5")
        self.sb.run()
        self.sb.run()
        calls = len(self.sb.claude_calls())
        self.sb.run()

        self.assertEqual(len(self.sb.claude_calls()), calls)
        self.assertEqual(self.sb.read_task("pending", "t")[0]["attempts"], "2")
        self.assertIsNotNone(self.sb.read_pause())

    def test_no_pause_when_there_is_nothing_to_run(self):
        self.sb.configure(MAX_ATTEMPTS_PER_DAY="1")
        self.sb.write_usage([self.worker_line()])
        self.sb.run()
        self.assertIsNone(self.sb.read_pause())
        self.assertEqual(self.sb.texts(), [])


if __name__ == "__main__":
    unittest.main()
