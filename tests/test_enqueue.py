"""Phase 6: start on events, not on a 15-minute clock.

`dispatcher.py enqueue <file>` lets another program drop a task (validated,
atomically, and gated like any other). DISPATCH_DRAIN keeps a run going until
the queue is empty and DISPATCH_LOCK_WAIT_SECONDS lets a run started by a new
file wait for the one already running; both are off for cron.
"""

import fcntl
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from test_baseline import DEFAULT_BODY, PASS, Sandbox, fail


class EnqueueCase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.drop = self.sb.root / "drop"
        self.drop.mkdir()

    def task_file(self, name="idea", text=None, **meta):
        front = {"model": "fake-worker", "review_model": "fake-review", "max_attempts": "3"}
        front.update(meta)
        body = "---\n" + "\n".join(f"{k}: {v}" for k, v in front.items()) + "\n---\n" + DEFAULT_BODY
        path = self.drop / f"{name}.md"
        path.write_text(text if text is not None else body)
        return path

    def enqueue(self, path):
        return self.sb.python(str(self.sb.base / "dispatcher.py"), "enqueue", str(path))


class EnqueueTests(EnqueueCase):
    def test_a_valid_file_lands_in_pending_and_runs(self):
        proc = self.enqueue(self.task_file("idea"))

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Queued idea.md", proc.stdout)
        self.assertEqual(self.sb.names("pending"), ["idea"])
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["idea"])

    def test_it_arrives_whole_with_no_temporary_file_left_where_a_watcher_looks(self):
        self.enqueue(self.task_file("idea"))
        names = sorted(p.name for p in (self.sb.base / "tasks" / "pending").iterdir())
        self.assertEqual(names, ["idea.md"])

    def test_the_source_file_is_left_alone(self):
        source = self.task_file("idea")
        before = source.read_text()
        self.enqueue(source)
        self.assertEqual(source.read_text(), before)

    def test_an_invalid_file_is_refused_with_the_reasons_and_nothing_is_queued(self):
        proc = self.enqueue(self.task_file("bad", text="no frontmatter here"))

        self.assertEqual(proc.returncode, 1)
        self.assertIn("frontmatter", proc.stdout)
        self.assertEqual(self.sb.names("pending"), [])

    def test_a_grant_no_task_may_have_is_refused(self):
        proc = self.enqueue(self.task_file("evil", allowed_tools="Read,Bash(gh api *)"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("gh api", proc.stdout)

    def test_a_name_already_in_the_queue_is_refused_whatever_state_it_is_in(self):
        for state in ("pending", "done", "failed", "proposed", "cancelled"):
            self.sb.add_task(f"in-{state}", state=state)
            proc = self.enqueue(self.task_file(f"in-{state}"))
            self.assertEqual(proc.returncode, 1, state)
            self.assertIn("already", proc.stdout)

    def test_a_missing_file_is_an_error(self):
        proc = self.enqueue(self.drop / "nope.md")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no such file", proc.stdout)

    def test_an_unsafe_name_is_refused(self):
        proc = self.enqueue(self.task_file("has space"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("name", proc.stdout)

    def test_no_argument_prints_the_usage(self):
        proc = self.sb.python(str(self.sb.base / "dispatcher.py"), "enqueue")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("enqueue <file>", proc.stderr)

    def test_a_task_that_needs_approval_goes_to_proposed_and_is_announced(self):
        self.sb.configure(APPROVAL_MIN_TIER="1")
        proc = self.enqueue(self.task_file("writes"))

        self.assertEqual(proc.returncode, 0)
        self.assertIn("approval", proc.stdout)
        self.assertEqual(self.sb.names("proposed"), ["writes"])
        self.assertEqual(self.sb.names("pending"), [])
        self.assertTrue(any("Approval needed: writes" in t for t in self.sb.telegram.texts()))
        self.sb.run()
        self.assertEqual(self.sb.claude_calls(), [])

    def test_a_read_only_task_skips_approval(self):
        self.sb.configure(APPROVAL_MIN_TIER="1")
        self.enqueue(self.task_file("look", allowed_tools="Read,Glob,Grep"))
        self.assertEqual(self.sb.names("pending"), ["look"])


class DrainTests(EnqueueCase):
    def test_without_drain_a_run_does_one_task_as_cron_always_did(self):
        self.sb.add_task("a")
        self.sb.add_task("b")
        self.sb.run()
        self.assertEqual(len(self.sb.names("done")), 1)

    def test_with_drain_one_run_empties_the_queue(self):
        self.sb.configure(DISPATCH_DRAIN="1")
        for name in ("a", "b", "c"):
            self.sb.add_task(name)
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["a", "b", "c"])

    def test_a_task_that_needs_another_attempt_is_not_looped_within_one_run(self):
        self.sb.configure(DISPATCH_DRAIN="1")
        self.sb.set_scenario(review=[fail("not yet"), PASS])
        self.sb.add_task("a")
        self.sb.run()

        self.assertEqual(len(self.sb.claude_calls("worker")), 1)
        self.assertEqual(self.sb.names("pending"), ["a"])
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["a"])

    def test_drain_stops_when_the_queue_is_paused_by_a_usage_limit(self):
        self.sb.configure(DISPATCH_DRAIN="1")
        limit = {"exit": 1, "stdout": "", "stderr": "API Error: 429 rate_limit_error: slow down"}
        self.sb.set_scenario(worker=[limit])
        self.sb.add_task("a")
        self.sb.add_task("b")
        self.sb.run()

        self.assertEqual(len(self.sb.claude_calls("worker")), 1)
        self.assertEqual(self.sb.names("pending"), ["a", "b"])

    def test_drain_stops_at_the_daily_attempt_cap(self):
        self.sb.configure(DISPATCH_DRAIN="1", MAX_ATTEMPTS_PER_DAY="2")
        for name in ("a", "b", "c"):
            self.sb.add_task(name)
        self.sb.run()

        self.assertEqual(len(self.sb.names("done")), 2)
        self.assertEqual(len(self.sb.names("pending")), 1)

    def test_drain_runs_a_task_that_arrives_while_it_works(self):
        self.sb.configure(DISPATCH_DRAIN="1")
        self.sb.set_scenario(worker=[{"result": "slow", "stream": [{"sleep": 1.5}]}], review=[PASS])
        self.sb.add_task("first")
        proc = subprocess.Popen(
            [sys.executable, str(self.sb.base / "dispatcher.py")],
            cwd=self.sb.base, env=self.sb.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(0.7)
        self.enqueue(self.task_file("second"))
        proc.communicate(timeout=60)

        self.assertEqual(self.sb.names("done"), ["first", "second"])


class LockWaitTests(EnqueueCase):
    def hold_lock(self, seconds):
        handle = open(self.sb.base / "dispatcher.lock", "w")
        fcntl.flock(handle, fcntl.LOCK_EX)

        def release():
            time.sleep(seconds)
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

        thread = threading.Thread(target=release)
        thread.start()
        return thread

    def test_by_default_a_second_run_exits_at_once_when_one_is_active(self):
        thread = self.hold_lock(1.0)
        self.sb.add_task("a")
        proc = self.sb.run()
        thread.join()

        self.assertIn("Another dispatcher run is active", proc.stdout)
        self.assertEqual(self.sb.names("pending"), ["a"])

    def test_with_a_lock_wait_the_second_run_waits_its_turn_and_then_runs(self):
        self.sb.configure(DISPATCH_LOCK_WAIT_SECONDS="20")
        thread = self.hold_lock(1.0)
        self.sb.add_task("a")
        self.sb.run()
        thread.join()

        self.assertEqual(self.sb.names("done"), ["a"])

    def test_the_wait_has_a_limit(self):
        self.sb.configure(DISPATCH_LOCK_WAIT_SECONDS="1")
        thread = self.hold_lock(4.0)
        self.sb.add_task("a")
        started = time.monotonic()
        proc = self.sb.run()
        elapsed = time.monotonic() - started
        thread.join()

        self.assertLess(elapsed, 3.5)
        self.assertIn("Another dispatcher run is active", proc.stdout)
        self.assertEqual(self.sb.names("pending"), ["a"])


class HeldTaskTests(EnqueueCase):
    """A task held back (PR cap, failed publish) must not be re-run by the next event at once."""

    def test_a_task_held_until_later_is_left_alone_until_then(self):
        later = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.sb.add_task("held", held_until=later)
        self.sb.add_task("free")
        self.sb.run()
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["free"])
        self.assertEqual(self.sb.names("pending"), ["held"])

    def test_a_task_whose_hold_has_passed_runs(self):
        earlier = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.sb.add_task("held", held_until=earlier)
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["held"])


if __name__ == "__main__":
    unittest.main()
