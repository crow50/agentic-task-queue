"""Pin today's dispatcher behavior before anything in it changes (Phase 0).

Every test runs dispatcher.py as a subprocess inside a Sandbox: a temp copy of
the queue with a fake `claude` and a fake Telegram. These are characterization
tests — they describe what the dispatcher does now, not what it should do.

The Sandbox never reads the repo's real .env, logs or tasks, and the subprocess
environment is scrubbed, so no real bot token or claude login can leak in.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fake_telegram import FakeTelegram

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
FIXTURES = TESTS / "fixtures"
FAKE_CLAUDE = TESTS / "fake_claude.py"

QUEUE_STATES = ("pending", "active", "done", "failed", "recurring", "cancelled")
DEFAULT_BODY = "# Task\n\nDo the thing.\n\n## Acceptance Criteria\n- It is done.\n"

PASS = {"result": "VERDICT: PASS"}


def fail(*bullets):
    return {"result": "VERDICT: FAIL\n" + "\n".join(f"- {b}" for b in bullets)}


class Sandbox:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="taskq-test-"))
        self.base = self.root / "queue"
        self.home = self.root / "home"
        self.base.mkdir()
        self.home.mkdir()
        for script in ("dispatcher.py", "coordinator_bot.py"):
            shutil.copy2(REPO / script, self.base / script)

        self.telegram = FakeTelegram()
        self.telegram.start()
        self.scenario_path = self.root / "scenario.json"
        self.calls_path = Path(f"{self.scenario_path}.calls.jsonl")
        self.set_scenario()

        self.settings = {
            "CLAUDE_BIN": str(FAKE_CLAUDE),
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "4242",
            "TELEGRAM_API_BASE": self.telegram.url,
            "MAX_RATE_LIMIT_RETRIES": "0",  # a rate limit must never make a test sleep
            "RATE_LIMIT_BASE_DELAY": "0",
            "DEFAULT_TIMEOUT_MINUTES": "1",
            "REVIEW_TIMEOUT_MINUTES": "1",
        }
        self.configure()

    def configure(self, **settings):
        """Change .env settings; a value of None removes the key."""
        for key, value in settings.items():
            if value is None:
                self.settings.pop(key, None)
            else:
                self.settings[key] = value
        (self.base / ".env").write_text("".join(f"{k}={v}\n" for k, v in self.settings.items()))

    def set_scenario(self, worker=None, review=None, other=None):
        """Script the fake claude: a list of steps per role (see fake_claude.py)."""
        scenario = {"worker": worker, "review": review, "other": other}
        scenario = {k: v for k, v in scenario.items() if v is not None}
        self.scenario_path.write_text(json.dumps(scenario))

    def close(self):
        self.telegram.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def env(self):
        """The only environment a subprocess sees — nothing is inherited."""
        return {
            "PATH": os.pathsep.join(
                [str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"]
            ),
            "HOME": str(self.home),
            "LC_ALL": "C.UTF-8",
            "FAKE_CLAUDE_SCENARIO": str(self.scenario_path),
        }

    def python(self, *args, timeout=60):
        return subprocess.run(
            [sys.executable, *args],
            cwd=self.base, env=self.env(), capture_output=True, text=True, timeout=timeout,
        )

    def run(self, *args, timeout=60):
        """One dispatcher cycle; raises with the output if the process errors."""
        proc = self.python(str(self.base / "dispatcher.py"), *args, timeout=timeout)
        if proc.returncode != 0:
            raise AssertionError(
                f"dispatcher.py exited {proc.returncode}\n"
                f"--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}"
            )
        return proc

    def add_task(self, name, state="pending", body=DEFAULT_BODY, **meta):
        frontmatter = {
            "model": "fake-worker",
            "escalation_model": "fake-escalated",
            "review_model": "fake-review",
            "max_attempts": "3",
            "attempts": "0",
        }
        frontmatter.update(meta)
        front = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
        directory = self.base / "tasks" / state
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.md").write_text(f"---\n{front}\n---\n{body}")

    def names(self, state):
        directory = self.base / "tasks" / state
        return sorted(p.stem for p in directory.glob("*.md")) if directory.is_dir() else []

    def find(self, pattern):
        """(state, stem) for every task file matching a glob, across all states."""
        return sorted(
            (state, p.stem)
            for state in QUEUE_STATES
            for p in (self.base / "tasks" / state).glob(f"{pattern}.md")
        )

    def read_task(self, state, name):
        text = (self.base / "tasks" / state / f"{name}.md").read_text()
        match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        meta = dict(line.split(": ", 1) for line in match.group(1).splitlines())
        return meta, match.group(2)

    def claude_calls(self, role=None):
        if not self.calls_path.exists():
            return []
        calls = [json.loads(line) for line in self.calls_path.read_text().splitlines()]
        return [c for c in calls if role is None or c["role"] == role]


class BaselineCase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def roles(self):
        return [c["role"] for c in self.sb.claude_calls()]


# ---------------------------------------------------------------- the five pins


class PassTests(BaselineCase):
    def test_pass_moves_task_to_done(self):
        self.sb.set_scenario(worker=[{"result": "Wrote hello.txt"}], review=[PASS])
        self.sb.add_task("hello")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["hello"])
        for state in ("pending", "active", "failed"):
            self.assertEqual(self.sb.names(state), [], state)
        meta, body = self.sb.read_task("done", "hello")
        self.assertEqual(meta["attempts"], "1")
        self.assertIn("## Result (attempt 1,", body)
        self.assertIn("Wrote hello.txt", body)

    def test_pass_makes_one_worker_and_one_read_only_review_call(self):
        self.sb.add_task("hello")
        self.sb.run()

        self.assertEqual(self.roles(), ["worker", "review"])
        worker, review = self.sb.claude_calls()
        self.assertEqual(worker["model"], "fake-worker")
        self.assertEqual(review["model"], "fake-review")
        self.assertEqual(review["allowed_tools"], "Read,Glob,Grep")
        self.assertIn("It is done.", review["prompt"])  # the acceptance criteria

    def test_pass_notifies_telegram_once(self):
        self.sb.set_scenario(worker=[{"result": "Wrote hello.txt"}])
        self.sb.add_task("hello")
        self.sb.run()

        (request,) = self.sb.telegram.calls("sendMessage")
        self.assertEqual(request.params["chat_id"], "4242")
        self.assertEqual(request.token, "test-token")
        self.assertIn("Task done: hello.md (attempt 1/3)", request.params["text"])
        self.assertIn("Wrote hello.txt", request.params["text"])


class FailTests(BaselineCase):
    def test_fail_requeues_with_feedback(self):
        self.sb.set_scenario(review=[fail("missing the report")])
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        for state in ("active", "done", "failed"):
            self.assertEqual(self.sb.names(state), [], state)
        meta, body = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "1")
        self.assertIn("## Attempt 1 Feedback", body)
        self.assertIn("- missing the report", body)
        self.assertNotIn("VERDICT", body)
        self.assertEqual(self.sb.telegram.texts(), [])  # a non-final failure is silent

    def test_retry_sees_feedback_and_runs_on_the_escalation_model(self):
        self.sb.set_scenario(review=[fail("missing the report"), PASS])
        self.sb.add_task("t")
        self.sb.run()
        self.sb.run()

        first, _, second, _ = self.sb.claude_calls()
        self.assertEqual(first["model"], "fake-worker")
        self.assertEqual(second["model"], "fake-escalated")
        self.assertIn("## Attempt 1 Feedback", second["prompt"])
        self.assertIn("missing the report", second["prompt"])
        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(self.sb.read_task("done", "t")[0]["attempts"], "2")

    def test_last_attempt_moves_task_to_failed(self):
        self.sb.set_scenario(review=[fail("still wrong")])
        self.sb.add_task("t", max_attempts="2")
        self.sb.run()
        self.assertEqual(self.sb.names("pending"), ["t"])
        self.sb.run()

        self.assertEqual(self.sb.names("failed"), ["t"])
        for state in ("pending", "active", "done"):
            self.assertEqual(self.sb.names(state), [], state)
        meta, body = self.sb.read_task("failed", "t")
        self.assertEqual(meta["attempts"], "2")
        self.assertIn("## Attempt 2 Feedback", body)

        (text,) = self.sb.telegram.texts()  # only the final failure is announced
        self.assertIn("Task failed: t.md", text)
        self.assertIn("Exhausted 2 attempts", text)
        self.assertIn("still wrong", text)

        calls_before = len(self.sb.claude_calls())
        self.sb.run()  # nothing left to do
        self.assertEqual(len(self.sb.claude_calls()), calls_before)


class DependencyTests(BaselineCase):
    def test_failed_dependency_cascades_without_calling_claude(self):
        self.sb.add_task("dep", state="failed")
        self.sb.add_task("child", depends_on="dep")
        self.sb.add_task("grandchild", depends_on="child")
        self.sb.run()
        self.sb.run()  # the chain may need a second cycle, depending on file order

        self.assertEqual(self.sb.names("failed"), ["child", "dep", "grandchild"])
        self.assertEqual(self.sb.names("pending"), [])
        _, body = self.sb.read_task("failed", "child")
        self.assertIn("## Dependency Failed", body)
        self.assertIn("dependency 'dep'", body)
        self.assertEqual(self.sb.claude_calls(), [])
        texts = self.sb.telegram.texts()
        self.assertEqual(len(texts), 2)
        self.assertTrue(any("Task not run: child.md" in t for t in texts), texts)
        self.assertTrue(any("Task not run: grandchild.md" in t for t in texts), texts)

    def test_task_runs_once_its_dependency_is_done(self):
        self.sb.add_task("dep", state="done")
        self.sb.add_task("child", depends_on="dep")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["child", "dep"])
        self.assertEqual(self.roles(), ["worker", "review"])


class RecurringTests(BaselineCase):
    def test_due_template_spawns_exactly_one_instance(self):
        self.sb.add_task("nightly", state="recurring", schedule="every 1d")
        self.sb.run()
        self.sb.run()  # not due again for a day

        ((state, stem),) = self.sb.find("nightly-[0-9]*")
        self.assertEqual(state, "done")  # spawned, picked up and passed in the same cycle
        meta, _ = self.sb.read_task("done", stem)
        self.assertNotIn("schedule", meta)
        self.assertNotIn("last_run", meta)
        self.assertEqual(meta["attempts"], "1")
        self.assertEqual(self.roles(), ["worker", "review"])

        template, _ = self.sb.read_task("recurring", "nightly")
        self.assertEqual(template["schedule"], "every 1d")
        self.assertIn("last_run", template)

    def test_template_waits_while_its_instance_is_still_queued(self):
        self.sb.add_task("nightly", state="recurring", schedule="every 1d")
        self.sb.add_task("nightly-20200101-000000", depends_on="never-finishes")
        self.sb.run()

        self.assertEqual(self.sb.find("nightly-[0-9]*"), [("pending", "nightly-20200101-000000")])
        self.assertNotIn("last_run", self.sb.read_task("recurring", "nightly")[0])


# ---------------------------------------------------------------- plan items 2, 3, 5


class FakeClaudeTests(BaselineCase):
    def call(self, prompt):
        return subprocess.run(
            [str(FAKE_CLAUDE), "-p", "--model", "fake-model", "--output-format", "json"],
            input=prompt, env=self.sb.env(), capture_output=True, text=True, timeout=30,
        )

    def test_is_executable(self):
        self.assertTrue(os.access(FAKE_CLAUDE, os.X_OK))

    def test_fixture_envelope_has_the_fields_the_plan_lists(self):
        envelope = json.loads((FIXTURES / "envelope-success.json").read_text())
        for key in ("result", "session_id", "total_cost_usd", "num_turns", "modelUsage",
                    "permission_denials"):
            self.assertIn(key, envelope)

    def test_success_envelope_has_the_same_keys_as_the_fixture(self):
        fixture = json.loads((FIXTURES / "envelope-success.json").read_text())
        proc = self.call("You are running unattended inside an automated task queue.")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        envelope = json.loads(proc.stdout)
        self.assertEqual(set(envelope), set(fixture))
        self.assertEqual(envelope["result"], "Done.")

    def test_can_exit_nonzero_with_a_chosen_message(self):
        self.sb.set_scenario(worker=[{"exit": 1, "stdout": "", "stderr": "boom: chosen message"}])
        proc = self.call("You are running unattended inside an automated task queue.")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("boom: chosen message", proc.stderr)

    def test_replays_the_real_auth_expired_output_and_exits_nonzero(self):
        self.sb.set_scenario(worker=[{"fixture": "auth-expired.txt", "exit": 1}])
        proc = self.call("You are running unattended inside an automated task queue.")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, (FIXTURES / "auth-expired.txt").read_text())
        envelope = json.loads(proc.stdout)
        self.assertTrue(envelope["is_error"])
        self.assertIn("Failed to authenticate", envelope["result"])
        self.assertIn("OAuth session expired", envelope["result"])

    def test_logs_every_call_with_its_role(self):
        self.call("You are running unattended inside an automated task queue.")
        self.call("You are a strict automated reviewer for a task queue.")
        self.call("hello")
        self.assertEqual([c["role"] for c in self.sb.claude_calls()], ["worker", "review", "other"])
        self.assertEqual(self.sb.claude_calls()[0]["model"], "fake-model")

    def test_refuses_to_run_without_a_scenario(self):
        env = self.sb.env()
        del env["FAKE_CLAUDE_SCENARIO"]
        proc = subprocess.run([str(FAKE_CLAUDE), "-p"], input="x", env=env,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 99)


class TelegramApiBaseTests(BaselineCase):
    """TELEGRAM_API_BASE: default https://api.telegram.org, overridable in .env."""

    SNIPPET = """
import sys, urllib.request
seen = []
def fake_urlopen(url, *args, **kwargs):
    seen.append(url if isinstance(url, str) else url.full_url)
    raise RuntimeError("stop before any network call")
urllib.request.urlopen = fake_urlopen
try:
    if sys.argv[1] == "dispatcher":
        import dispatcher
        dispatcher.send_telegram("hello")
    else:
        import coordinator_bot
        coordinator_bot.tg_api("getMe")
except RuntimeError:
    pass
print(seen[0])
"""

    def url_used_by(self, script):
        proc = self.sb.python("-c", self.SNIPPET, script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_dispatcher_defaults_to_the_real_api(self):
        self.sb.configure(TELEGRAM_API_BASE=None)
        self.assertEqual(
            self.url_used_by("dispatcher"), "https://api.telegram.org/bottest-token/sendMessage"
        )

    def test_coordinator_defaults_to_the_real_api(self):
        self.sb.configure(TELEGRAM_API_BASE=None)
        self.assertEqual(
            self.url_used_by("coordinator"), "https://api.telegram.org/bottest-token/getMe"
        )

    def test_dispatcher_uses_the_configured_base(self):
        self.sb.configure(TELEGRAM_API_BASE="http://127.0.0.1:9/")  # trailing slash tolerated
        self.assertEqual(self.url_used_by("dispatcher"), "http://127.0.0.1:9/bottest-token/sendMessage")

    def test_coordinator_uses_the_configured_base(self):
        self.sb.configure(TELEGRAM_API_BASE="http://127.0.0.1:9")
        self.assertEqual(self.url_used_by("coordinator"), "http://127.0.0.1:9/bottest-token/getMe")


class SandboxIsolationTests(BaselineCase):
    def test_nothing_from_the_real_environment_reaches_a_run(self):
        os.environ["TASKQ_LEAK_CHECK"] = "leaked"
        self.addCleanup(os.environ.pop, "TASKQ_LEAK_CHECK", None)
        proc = self.sb.python(
            "-c", "import os; print(os.environ.get('TASKQ_LEAK_CHECK'), os.environ['HOME'])"
        )
        self.assertEqual(proc.stdout.split(), ["None", str(self.sb.home)])

    def test_sandbox_has_only_its_own_env_file_and_no_repo_state(self):
        text = (self.sb.base / ".env").read_text()
        self.assertIn("TELEGRAM_BOT_TOKEN=test-token", text)
        self.assertEqual(
            sorted(p.name for p in self.sb.base.iterdir()),
            [".env", "coordinator_bot.py", "dispatcher.py"],
        )


if __name__ == "__main__":
    unittest.main()
