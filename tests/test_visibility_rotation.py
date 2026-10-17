"""Phase 8 Part A item 4: the coordinator starts a fresh session before its context fills, and its memory stays bounded."""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from taskq_lib import memory  # noqa: E402
from test_limits_coordinator import USER, BotCase, message  # noqa: E402


def big(tokens=900000):
    return {"result": "Working on it.", "envelope": {"usage": {"iterations": [{"input_tokens": tokens}]}}}


class TrimTests(unittest.TestCase):
    def test_text_that_fits_is_left_alone(self):
        self.assertEqual(memory.trim_to_fit("a\nb\n", 100), "a\nb\n")

    def test_the_oldest_lines_go_first_and_only_whole_lines_stay(self):
        self.assertEqual(memory.trim_to_fit("one\ntwo\nthree\n", 10), "two\nthree\n")
        self.assertEqual(memory.trim_to_fit("one\ntwo\nthree\n", 9), "three\n")
        self.assertEqual(memory.trim_to_fit("é" * 10, 5), "")  # bytes, not characters

    def test_no_room_leaves_nothing(self):
        self.assertEqual(memory.trim_to_fit("abc", 0), "")


class RotationTests(BotCase):
    def setUp(self):
        super().setUp()
        self.sb.configure(TELEGRAM_USER_ID=str(USER), CONTEXT_ROTATE_PCT="85")

    def say(self, text="hello", update_id=1):
        return self.handle(message(text, sender=USER, update_id=update_id))

    def test_over_the_line_the_next_message_saves_memory_then_starts_a_fresh_session(self):
        self.sb.set_scenario(other=[big(), {"result": "saved"}, {"result": "Hello again."}])
        first = self.say("one")
        self.assertEqual(first["context_pct"], 90.0)
        self.say("two")

        calls = self.others()
        self.assertEqual(len(calls), 3)
        self.assertIn("--resume", calls[1]["argv"])  # the saving call is still in the old session
        self.assertIn("memory/session.md", calls[1]["prompt"])
        self.assertNotIn("--resume", calls[2]["argv"])  # then a fresh one
        self.assertEqual(calls[2]["prompt"], "two")
        texts = self.sent()
        self.assertIn("Started a fresh session (context was 90%).", texts[-1])
        self.assertIn("Hello again.", texts[-1])

    def test_exactly_one_fresh_session_per_crossing(self):
        self.sb.set_scenario(other=[big(), {"result": "saved"}, {"result": "ok"}, {"result": "ok"}])
        for n, text in enumerate(("one", "two", "three"), 1):
            self.say(text, update_id=n)
        resumed = ["--resume" in c["argv"] for c in self.others()]
        self.assertEqual(resumed, [False, True, False, True])  # message 3 resumes the fresh session; no second rotation
        self.assertEqual(len([t for t in self.sent() if "fresh session" in t]), 1)

    def test_below_the_line_or_with_the_setting_off_nothing_rotates(self):
        self.sb.set_scenario(other=[big(800000), {"result": "ok"}])
        self.say("one")
        self.say("two")
        self.assertEqual(len(self.others()), 2)
        self.sb.configure(CONTEXT_ROTATE_PCT=None)
        self.sb.set_scenario(other=[big(), {"result": "ok"}])
        self.say("three")
        self.say("four")
        self.assertEqual(len(self.others()), 4)

    def test_a_saving_call_that_fails_keeps_the_session_and_does_not_loop(self):
        self.sb.set_scenario(other=[big(), {"exit": 1, "stdout": "", "stderr": "boom"}, {"result": "still here"}])
        self.say("one")
        self.say("two")
        self.assertIn("still here", self.sent()[-1])
        self.assertNotIn("fresh session", self.sent()[-1])

    def test_new_forgets_the_context_reading(self):
        self.sb.set_scenario(other=[big(), {"result": "ok"}])
        self.say("one")
        state = self.say("/new", update_id=2)
        self.assertIsNone(state["session_id"])
        self.assertIsNone(state.get("context_pct"))

    def test_the_rotation_writes_are_capped_by_the_memory_limit(self):
        self.sb.configure(COORDINATOR_MEMORY_MAX_KB="1")
        memory_dir = self.sb.base / "coordinator" / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "MEMORY.md").write_text("m" * 300 + "\n")
        (memory_dir / "session.md").write_text("".join(f"line {n:04d} " + "x" * 40 + "\n" for n in range(100)))
        self.sb.set_scenario(other=[big(), {"result": "saved"}, {"result": "ok"}])
        self.say("one")
        self.say("two")
        total = sum(p.stat().st_size for p in memory_dir.iterdir())
        self.assertLessEqual(total, 1024)
        self.assertIn("line 0099", (memory_dir / "session.md").read_text())  # the newest lines survive
        self.assertNotIn("line 0000", (memory_dir / "session.md").read_text())
        self.assertEqual((memory_dir / "MEMORY.md").read_text(), "m" * 300 + "\n")  # the curated file is never trimmed


if __name__ == "__main__":
    unittest.main()
