"""Phase 9 Part B item 5: a long task is resumed, not restarted, and you can steer it from the phone.

A timeout is not a failure of the work: when the worker was making progress, the next call resumes its session and no
attempt is used. /extend and /stop reach the running dispatcher through state/control/; a task that goes quiet raises
one alert with buttons. Slow workers are scripted with the fake claude's stream steps (a tool call, then a sleep).
"""

import json
import subprocess
import sys
import time
import unittest

from test_baseline import PASS, Sandbox

TOOL = {"tool": "Read", "input": {"file_path": "notes.txt"}}
SLEEP = {"sleep": 3.0}
TIMEOUT = "0.03"  # minutes: a second and a bit


class LongCase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def snippet(self, code):
        proc = self.sb.python("-c", "import dispatcher, json\n" + code)
        if proc.returncode != 0:
            raise AssertionError(f"snippet failed\n{proc.stderr}")
        return proc.stdout.strip()

    def start(self):
        proc = subprocess.Popen([sys.executable, str(self.sb.base / "dispatcher.py")], cwd=self.sb.base, env=self.sb.env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def reap():
            if proc.poll() is None:
                proc.kill()
            proc.communicate()
        self.addCleanup(reap)
        return proc

    def wait_for(self, check, seconds=15):
        end = time.time() + seconds
        while time.time() < end:
            if check():
                return True
            time.sleep(0.1)
        return False

    def worker_calls(self):
        return self.sb.claude_calls("worker")

    def attempts(self, state, name):
        return int(self.sb.read_task(state, name)[0]["attempts"])


class ResumeTests(LongCase):
    def test_a_timeout_with_progress_resumes_the_session_and_uses_no_attempt(self):
        self.sb.set_scenario(worker=[{"stream": [TOOL, SLEEP]}, {"result": "Finished the work."}], review=[PASS])
        self.sb.add_task("slow", timeout_minutes=TIMEOUT)
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["slow"])
        meta, _ = self.sb.read_task("pending", "slow")
        self.assertEqual(meta["attempts"], "0", "the timeout did not use an attempt")
        self.assertTrue(meta["resume_session"])

        self.sb.configure(DEFAULT_TIMEOUT_MINUTES="1")
        self.sb.run()
        first, second = self.worker_calls()
        self.assertNotIn("--resume", first["argv"])
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1], meta["resume_session"])
        self.assertIn("ran out of time", second["prompt"].lower())
        self.assertNotIn("Do the thing", second["prompt"], "a resumed session already has the task")
        self.assertEqual(self.sb.names("done"), ["slow"])
        self.assertEqual(self.attempts("done", "slow"), 1)
        self.assertNotIn("resume_session", self.sb.read_task("done", "slow")[0])

    def test_a_timeout_that_made_no_progress_uses_an_attempt_and_starts_fresh(self):
        self.sb.set_scenario(worker=[{"stream": [SLEEP]}, {"result": "Finished."}], review=[PASS])
        self.sb.add_task("stuck", timeout_minutes=TIMEOUT)
        self.sb.run()

        meta, _ = self.sb.read_task("pending", "stuck")
        self.assertEqual(meta["attempts"], "1")
        self.assertNotIn("resume_session", meta)
        self.sb.run()
        self.assertNotIn("--resume", self.worker_calls()[-1]["argv"])

    def test_only_a_few_free_resumes_are_given_to_one_task(self):
        self.sb.configure(MAX_RESUMES="1")
        self.sb.set_scenario(worker=[{"stream": [TOOL, SLEEP]}], review=[PASS])
        self.sb.add_task("endless", timeout_minutes=TIMEOUT)
        self.sb.run()
        self.assertEqual(self.sb.read_task("pending", "endless")[0]["attempts"], "0")
        self.sb.run()
        meta, _ = self.sb.read_task("pending", "endless")
        self.assertEqual(meta["attempts"], "1", "the second timeout is charged")

    def test_a_session_that_is_gone_falls_back_to_a_fresh_start_given_the_stream_tail(self):
        self.sb.set_scenario(worker=[
            {"stream": [TOOL, {"tool": "Edit", "input": {"file_path": "draft.md"}}, SLEEP]},
            {"exit": 1, "stdout": "", "stderr": "No conversation found with session ID: gone"},
            {"result": "Finished from the notes."},
        ], review=[PASS])
        self.sb.add_task("lost", timeout_minutes=TIMEOUT)
        self.sb.run()
        self.sb.configure(DEFAULT_TIMEOUT_MINUTES="1")
        self.sb.run()

        first, resumed, fresh = self.worker_calls()
        self.assertIn("--resume", resumed["argv"])
        self.assertNotIn("--resume", fresh["argv"])
        self.assertIn("draft.md", fresh["prompt"])
        self.assertIn("<external_input", fresh["prompt"])
        self.assertTrue(fresh["prompt"].startswith("You are running unattended"))
        self.assertEqual(self.sb.names("done"), ["lost"])
        self.assertEqual(self.attempts("done", "lost"), 1, "falling back did not use a second attempt")

    def test_a_usage_limit_during_the_worker_resumes_that_session_afterwards(self):
        init = json.dumps({"type": "system", "subtype": "init", "session_id": "sess-limited"}) + "\n"
        limited = {"exit": 1, "stdout": init, "stderr": "Claude AI usage limit reached"}
        self.sb.set_scenario(worker=[limited, {"result": "Done."}], review=[PASS])
        self.sb.add_task("limited")
        self.sb.run()
        meta, _ = self.sb.read_task("pending", "limited")
        self.assertEqual((meta["attempts"], meta.get("resume_session")), ("0", "sess-limited"))
        (self.sb.base / "state" / "paused_until").unlink()
        self.sb.run()
        self.assertEqual(self.worker_calls()[-1]["argv"][self.worker_calls()[-1]["argv"].index("--resume") + 1], "sess-limited")


class ExtendAndStopTests(LongCase):
    def slow_task(self, name="slow", **meta):
        self.sb.set_scenario(worker=[{"stream": [TOOL, {"text": "Half done: found three items."}, {"sleep": 2.5}, TOOL],
                                      "result": "All done."}], review=[PASS])
        self.sb.add_task(name, timeout_minutes=meta.pop("timeout_minutes", TIMEOUT), **meta)
        proc = self.start()
        self.assertTrue(self.wait_for(lambda: (self.sb.base / "tasks" / "active" / f"{name}.md").exists()))
        return proc

    def test_extend_raises_the_running_tasks_limit_so_it_finishes_instead_of_timing_out(self):
        proc = self.slow_task()
        out = self.snippet("print(json.dumps(dispatcher.request_extend('slow', 1)))")
        self.assertTrue(json.loads(out)[0], out)
        proc.wait(timeout=30)
        self.assertEqual(self.sb.names("done"), ["slow"])
        self.assertEqual(self.attempts("done", "slow"), 1)

    def test_extend_only_touches_the_task_that_is_running(self):
        self.sb.add_task("other", state="done")
        proc = self.slow_task()
        ok, message = json.loads(self.snippet("print(json.dumps(dispatcher.request_extend('other', 5)))"))
        self.assertFalse(ok)
        self.assertIn("not running", message)
        proc.wait(timeout=30)
        self.assertFalse((self.sb.base / "state" / "control" / "other.json").exists())

    def test_extend_cannot_pass_the_maximum(self):
        self.sb.configure(MAX_TIMEOUT_MINUTES="2")
        proc = self.slow_task()
        ok, message = json.loads(self.snippet("print(json.dumps(dispatcher.request_extend('slow', 500)))"))
        self.assertTrue(ok)
        self.assertIn("2", message)
        proc.kill()

    def test_stop_ends_the_task_cleanly_and_keeps_what_it_had_said_as_a_result_to_continue(self):
        proc = self.slow_task(timeout_minutes="5")
        ok, message = json.loads(self.snippet("print(json.dumps(dispatcher.request_stop('slow')))"))
        self.assertTrue(ok, message)
        proc.wait(timeout=30)

        self.assertEqual(self.sb.names("cancelled"), ["slow"])
        self.assertEqual(self.sb.names("failed"), [])
        record = json.loads((self.sb.base / "state" / "outcomes" / "slow.json").read_text())
        self.assertEqual(record["status"], "cancelled")
        self.assertIn("Half done: found three items.", (self.sb.base / "state" / "outcomes" / "slow.report.md").read_text())
        ok, message = json.loads(self.snippet("print(json.dumps(dispatcher.create_followup('slow', 'Carry on from there.')))"))
        self.assertTrue(ok, message)
        self.assertEqual(self.sb.names("pending"), ["slow-f1"])

    def test_stop_on_a_task_that_is_not_running_is_refused(self):
        self.sb.add_task("idle")
        ok, message = json.loads(self.snippet("print(json.dumps(dispatcher.request_stop('idle')))"))
        self.assertFalse(ok)
        self.assertIn("not running", message)

    def test_the_live_message_carries_extend_and_stop_buttons(self):
        proc = self.slow_task(timeout_minutes="5")
        self.assertTrue(self.wait_for(lambda: self.sb.telegram.keyboards()))
        proc.kill()
        labels = [label for rows in self.sb.telegram.keyboards() for row in rows for label, _ in row]
        self.assertIn("Extend", labels)
        self.assertIn("Stop", labels)


class StallTests(LongCase):
    def test_a_task_that_goes_quiet_raises_one_alert_with_three_buttons(self):
        self.sb.configure(STALL_MINUTES="0.02")
        self.sb.set_scenario(worker=[{"stream": [TOOL, {"sleep": 3.5}], "result": "Done."}], review=[PASS])
        self.sb.add_task("quiet-one", timeout_minutes="5")
        self.sb.run()

        alerts = [t for t in self.sb.telegram.texts() if "no progress" in t.lower() or "has gone quiet" in t.lower()]
        self.assertEqual(len(alerts), 1, self.sb.telegram.texts())
        labels = [label for rows in self.sb.telegram.keyboards() for row in rows for label, _ in row]
        for wanted in ("Extend", "Stop", "Keep waiting"):
            self.assertIn(wanted, labels)

    def test_a_task_that_keeps_working_raises_none(self):
        self.sb.configure(STALL_MINUTES="0.05")
        self.sb.set_scenario(worker=[{"stream": [TOOL, {"sleep": 0.5}, TOOL, {"sleep": 0.5}, TOOL], "result": "Done."}], review=[PASS])
        self.sb.add_task("busy", timeout_minutes="5")
        self.sb.run()
        self.assertFalse([t for t in self.sb.telegram.texts() if "gone quiet" in t.lower()])


if __name__ == "__main__":
    unittest.main()
