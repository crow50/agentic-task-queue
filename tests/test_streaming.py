"""Phase 6: watch a worker run.

The worker runs with --output-format stream-json --verbose. The dispatcher reads
the events as they arrive, keeps the raw stream in the attempt logs, and edits
one Telegram message (at most once per STATUS_EDIT_INTERVAL_SECONDS) with the
current tool, file, elapsed time and, at the end, the cost.
"""

import json
import subprocess
import time
import unittest

from test_baseline import FAKE_CLAUDE, PASS, Sandbox

EDIT = "editMessageText"
SAMPLE = [json.loads(line) for line in (FAKE_CLAUDE.parent / "fixtures" / "stream-sample.jsonl").read_text().splitlines()]


def tool(name, file, tokens=5):
    return {"tool": name, "input": {"file_path": file}, "tokens": tokens}


class StreamCase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def snippet(self, code, *args):
        proc = self.sb.python("-c", code, *args)
        if proc.returncode != 0:
            raise AssertionError(f"snippet failed\n{proc.stderr}")
        return proc.stdout

    def usage(self):
        path = self.sb.base / "logs" / "usage.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def edit_texts(self):
        return [r.params["text"] for r in self.sb.telegram.edits()]

    def gaps(self):
        """Seconds between consecutive status calls (the message and its edits)."""
        calls = [r for r in self.sb.telegram.requests if r.method in ("sendMessage", EDIT)]
        status = [r for r in calls if r.method == EDIT or str(r.params.get("text", "")).startswith("⚙️")]
        return [b.at - a.at for a, b in zip(status, status[1:])]


class FakeClaudeStreamTests(StreamCase):
    def run_fake(self, step, output_format):
        self.sb.set_scenario(worker=[step])
        proc = subprocess.run(
            [str(FAKE_CLAUDE), "-p", "--model", "m", "--output-format", output_format, "--verbose"],
            input="You are running unattended. do it", env=self.sb.env(), capture_output=True, text=True, timeout=30,
        )
        return [json.loads(line) for line in proc.stdout.splitlines()]

    def test_stream_json_prints_init_then_the_events_then_the_result_line(self):
        events = self.run_fake(
            {"result": "Done.", "stream": [tool("Edit", "a.py"), {"text": "hi"}]}, "stream-json"
        )
        self.assertEqual([e["type"] for e in events], ["system", "assistant", "assistant", "result"])
        self.assertEqual(events[0]["subtype"], "init")
        self.assertEqual(events[1]["message"]["content"][0]["name"], "Edit")
        self.assertEqual(events[-1]["result"], "Done.")
        self.assertIn("total_cost_usd", events[-1])

    def test_plain_json_is_still_one_envelope(self):
        (envelope,) = self.run_fake({"result": "Done.", "stream": [tool("Edit", "a.py")]}, "json")
        self.assertEqual(envelope["result"], "Done.")


class DescribeEventTests(StreamCase):
    DESCRIBE = """
import json, sys
import dispatcher
print(json.dumps([dispatcher.describe_event(e) for e in json.loads(sys.argv[1])]))
"""

    def describe(self, events):
        return json.loads(self.snippet(self.DESCRIBE, json.dumps(events)))

    def test_a_real_run_yields_the_tool_and_file_once(self):
        described = self.describe(SAMPLE)
        found = [d for d in described if d]
        self.assertEqual(len(found), 1)  # thinking, text, user, rate_limit_event and result describe nothing
        self.assertEqual(found[0]["tool"], "Read")
        self.assertEqual(found[0]["target"], "/work/a.txt")

    def test_the_target_is_the_file_the_path_the_pattern_or_the_start_of_the_command(self):
        def block(**inp):
            return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "X", "input": inp}]}}

        got = self.describe([
            block(file_path="a.py"), block(path="src"), block(pattern="*.md"),
            block(command="python3 -m unittest discover -s tests -v --lots-of-flags-that-go-on"), block(),
        ])
        self.assertEqual(got[0]["target"], "a.py")
        self.assertEqual(got[1]["target"], "src")
        self.assertEqual(got[2]["target"], "*.md")
        self.assertTrue(got[3]["target"].startswith("python3 -m unittest"))
        self.assertLessEqual(len(got[3]["target"]), 40)
        self.assertEqual(got[4]["target"], "")


class WorkerStreamTests(StreamCase):
    def test_the_worker_runs_in_stream_json_mode_and_the_review_does_not(self):
        self.sb.add_task("t")
        self.sb.run()

        worker, review = self.sb.claude_calls("worker")[0], self.sb.claude_calls("review")[0]
        argv = worker["argv"]
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", argv)
        self.assertEqual(review["argv"][review["argv"].index("--output-format") + 1], "json")

    def test_a_streamed_pass_moves_to_done_and_logs_usage_from_the_result_line(self):
        self.sb.set_scenario(
            worker=[{"result": "Wrote it.", "envelope": {"total_cost_usd": 0.25}, "stream": [tool("Write", "a.txt")]}],
            review=[PASS],
        )
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        _, body = self.sb.read_task("done", "t")
        self.assertIn("Wrote it.", body)
        (worker_usage,) = [u for u in self.usage() if u["label"] == "worker"]
        self.assertEqual((worker_usage["status"], worker_usage["total_cost_usd"]), ("ok", 0.25))

    def test_the_raw_stream_is_kept_with_the_attempt_logs(self):
        self.sb.set_scenario(worker=[{"result": "ok", "stream": [tool("Write", "a.txt")]}], review=[PASS])
        self.sb.add_task("t")
        self.sb.run()

        lines = (self.sb.base / "logs" / "t.attempt-1.stream.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["type"] for line in lines], ["system", "assistant", "result"])

    def test_rate_limit_events_in_the_stream_are_not_mistaken_for_a_limit(self):
        limit_event = {"event": {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}}}
        self.sb.set_scenario(worker=[{"result": "ok", "stream": [limit_event]}], review=[PASS])
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertFalse((self.sb.base / "state" / "paused_until").exists())

    def test_a_usage_limit_in_the_result_line_still_pauses_and_charges_nothing(self):
        self.sb.set_scenario(worker=[{
            "exit": 1, "stream": [tool("Read", "a.txt")],
            "envelope": {"is_error": True, "result": "API Error: 429 rate_limit_error: slow down"},
        }])
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        meta, _ = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "0")
        self.assertTrue((self.sb.base / "state" / "paused_until").exists())

    def test_a_worker_that_prints_no_result_line_is_judged_on_what_it_printed(self):
        self.sb.set_scenario(worker=[{"exit": 1, "stdout": "", "stderr": "boom: not a limit"}])
        self.sb.add_task("t", max_attempts="1")
        self.sb.run()

        self.assertEqual(self.sb.names("failed"), ["t"])
        meta, body = self.sb.read_task("failed", "t")
        self.assertIn("boom: not a limit", body)

    def test_a_worker_past_its_timeout_is_killed_while_streaming(self):
        self.sb.set_scenario(worker=[{"result": "late", "stream": [tool("Read", "a.txt"), {"sleep": 30}]}])
        self.sb.add_task("t", timeout_minutes="0.03", max_attempts="1")
        started = time.monotonic()
        self.sb.run()

        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(self.sb.names("failed"), ["t"])
        (worker_usage,) = [u for u in self.usage() if u["label"] == "worker"]
        self.assertEqual(worker_usage["status"], "timeout")


class StatusEditorUnitTests(StreamCase):
    SCRIPT = """
import json, sys
import dispatcher

class Clock:
    now = 100.0
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds

clock, sent, edited = Clock(), [], []
editor = dispatcher.StatusEditor(
    "t.md",
    send=lambda text: (sent.append((clock(), text)), 7)[1],
    edit=lambda message_id, text: edited.append((clock(), message_id, text)),
    clock=clock, interval={interval}, sleep=clock.advance,
)
{body}
print(json.dumps({{"sent": sent, "edited": edited}}))
"""

    def drive(self, body, interval=10):
        return json.loads(self.snippet(self.SCRIPT.format(body=body, interval=interval)))

    def test_the_default_interval_is_ten_seconds(self):
        out = self.snippet("import dispatcher; print(dispatcher.StatusEditor('t', None, None).interval)")
        self.assertEqual(out.strip(), "10.0")

    def test_the_interval_comes_from_the_settings(self):
        self.sb.configure(STATUS_EDIT_INTERVAL_SECONDS="2.5")
        out = self.snippet("import dispatcher; print(dispatcher.StatusEditor('t', None, None).interval)")
        self.assertEqual(out.strip(), "2.5")

    def test_sixty_events_in_sixty_seconds_make_at_most_one_edit_per_ten_seconds(self):
        out = self.drive("""
for i in range(60):
    clock.advance(1)
    editor.event({"tool": "Edit", "target": f"f{i}.py", "tokens": i})
""")
        self.assertEqual(len(out["sent"]), 1)
        times = [out["sent"][0][0]] + [t for t, _, _ in out["edited"]]
        self.assertGreaterEqual(len(out["edited"]), 4)
        self.assertLessEqual(len(out["edited"]), 6)
        for earlier, later in zip(times, times[1:]):
            self.assertGreaterEqual(later - earlier, 10)

    def test_the_message_names_the_task_the_tool_the_file_and_the_elapsed_time(self):
        out = self.drive("""
editor.event({"tool": "Edit", "target": "src/app.py", "tokens": 12})
clock.advance(83)
editor.tick()
""")
        text = out["edited"][-1][2]
        for wanted in ("t.md", "Edit", "src/app.py", "1m23s"):
            self.assertIn(wanted, text)

    def test_finish_shows_the_outcome_and_the_exact_cost_even_inside_the_interval(self):
        out = self.drive("""
editor.event({"tool": "Edit", "target": "a.py", "tokens": 1})
clock.advance(2)
editor.finish("✅ done", cost=0.1234)
""")
        times = [out["sent"][0][0]] + [t for t, _, _ in out["edited"]]
        self.assertIn("✅ done", out["edited"][-1][2])
        self.assertIn("$0.12", out["edited"][-1][2])
        self.assertGreaterEqual(times[-1] - times[-2], 10)  # it waited out the interval instead of breaking it

    def test_a_task_that_never_used_a_tool_makes_no_status_message(self):
        out = self.drive("""
editor.tick()
editor.finish("✅ done", cost=0.1)
""")
        self.assertEqual((out["sent"], out["edited"]), ([], []))

    def test_a_failing_send_or_edit_never_raises(self):
        out = self.snippet("""
import dispatcher
def boom(*args):
    raise OSError("telegram is down")
editor = dispatcher.StatusEditor("t", send=boom, edit=boom, clock=lambda: 0.0, interval=0, sleep=lambda s: None)
editor.event({"tool": "Edit", "target": "a", "tokens": 1})
editor.event({"tool": "Edit", "target": "b", "tokens": 1})
editor.finish("done", cost=0)
print("fine")
""")
        self.assertEqual(out.strip(), "fine")


class StatusMessageTests(StreamCase):
    FAST = "0.5"

    def setUp(self):
        super().setUp()
        self.sb.configure(STATUS_EDIT_INTERVAL_SECONDS=self.FAST)

    def busy_worker(self, events=10, pause=0.3, result="Wrote it."):
        stream = []
        for i in range(events):
            stream += [tool("Edit", f"src/file{i}.py"), {"sleep": pause}]
        self.sb.set_scenario(worker=[{"result": result, "envelope": {"total_cost_usd": 0.5}, "stream": stream}], review=[PASS])

    def test_a_busy_run_edits_one_message_no_faster_than_the_interval(self):
        self.busy_worker()
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        edits = self.sb.telegram.edits()
        self.assertGreaterEqual(len(edits), 2)
        self.assertLess(len(edits), 10)  # ten events, fewer edits
        self.assertEqual({r.params["message_id"] for r in edits}, {edits[0].params["message_id"]})
        for gap in self.gaps():
            self.assertGreaterEqual(gap, float(self.FAST) - 0.05)

    def test_the_status_message_ends_with_the_result_and_cost_and_the_report_comes_separately(self):
        self.busy_worker()
        self.sb.add_task("t")
        self.sb.run()

        final = self.edit_texts()[-1]
        self.assertIn("t.md", final)
        self.assertIn("$0.50", final)
        self.assertTrue(any("Task done: t.md" in text for text in self.sb.telegram.texts()))
        self.assertNotIn("parse_mode", self.sb.telegram.edits()[0].params)

    def test_a_silent_stretch_still_refreshes_the_elapsed_time(self):
        self.sb.set_scenario(
            worker=[{"result": "ok", "stream": [tool("Read", "big.txt"), {"sleep": 2.6}]}], review=[PASS]
        )
        self.sb.add_task("t")
        self.sb.run()

        self.assertGreaterEqual(len(self.sb.telegram.edits()), 2)

    def test_a_failed_attempt_ends_the_status_message_as_failed(self):
        self.sb.set_scenario(worker=[{"result": "meh", "stream": [tool("Edit", "a.py")]}],
                             review=[{"result": "VERDICT: FAIL\n- no"}])
        self.sb.add_task("t", max_attempts="1")
        self.sb.run()

        self.assertIn("❌", self.edit_texts()[-1])

    def test_a_task_that_uses_no_tool_gets_no_status_message(self):
        self.sb.add_task("t")  # the fake's default worker streams only init and result
        self.sb.run()

        self.assertEqual(self.sb.telegram.edits(), [])
        (request,) = self.sb.telegram.calls("sendMessage")
        self.assertIn("Task done: t.md", request.params["text"])

    def test_quiet_tasks_get_no_status_message(self):
        self.busy_worker(events=3)
        self.sb.add_task("t", quiet="true")
        self.sb.run()

        self.assertEqual((self.sb.telegram.edits(), self.sb.telegram.texts()), ([], []))

    def test_telegram_errors_on_the_status_message_never_fail_the_task(self):
        self.busy_worker(events=6)
        for status in (400, 429, 500):
            self.sb.telegram.queue_response(EDIT, status)
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])

    def test_a_status_message_that_could_not_be_sent_is_not_edited(self):
        self.busy_worker(events=4)
        self.sb.telegram.queue_response("sendMessage", 500)
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(self.sb.telegram.edits(), [])


if __name__ == "__main__":
    unittest.main()
