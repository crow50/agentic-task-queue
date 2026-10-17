"""Phase 9 Part B items 5 and 7, from the phone: /extend, /stop, /backlog and the buttons on the live message, the stall
alert and the revisit nudge. All of them are code, not model calls, and all pass the same sender check."""

import json
import unittest

from test_limits_coordinator import USER, BotCase, message
from test_visibility_menu import tap


class ControlCase(BotCase):
    def setUp(self):
        super().setUp()
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        self.sb.add_task("slow", state="active")

    def control(self, stem="slow"):
        path = self.sb.base / "state" / "control" / f"{stem}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def say(self, text, sender=USER):
        return self.handle(message(text, sender=sender))

    def replies(self):
        return self.sb.texts()


class ExtendCommandTests(ControlCase):
    def test_extend_asks_the_running_dispatcher_for_more_time_and_calls_no_model(self):
        self.say("/extend slow 20")
        self.assertEqual(self.control()["extend_s"], 20 * 60)
        self.assertTrue(any("20" in text and "slow" in text for text in self.replies()), self.replies())
        self.assertEqual(self.sb.claude_calls(), [])

    def test_extend_adds_up(self):
        self.say("/extend slow 10")
        self.say("/extend slow 5")
        self.assertEqual(self.control()["extend_s"], 15 * 60)

    def test_extend_without_the_numbers_says_how(self):
        self.say("/extend")
        self.say("/extend slow")
        self.say("/extend slow lots")
        self.assertIsNone(self.control())
        self.assertEqual(len([t for t in self.replies() if "Usage: /extend" in t]), 3)

    def test_extend_on_a_task_that_is_not_running_changes_nothing(self):
        self.sb.add_task("idle")
        self.say("/extend idle 10")
        self.assertIsNone(self.control("idle"))
        self.assertTrue(any("not running" in text for text in self.replies()))

    def test_the_wrong_sender_cannot_extend(self):
        self.say("/extend slow 10", sender=999)
        self.assertIsNone(self.control())
        self.assertEqual(self.sb.telegram.requests, [])


class StopCommandTests(ControlCase):
    def test_stop_marks_the_running_task_for_a_clean_stop(self):
        self.say("/stop slow")
        self.assertTrue(self.control()["stop"])
        self.assertEqual(self.sb.claude_calls(), [])

    def test_stop_on_a_task_that_is_not_running_is_refused(self):
        self.say("/stop nothing")
        self.assertIsNone(self.control("nothing"))
        self.assertTrue(any("not running" in text for text in self.replies()))

    def test_the_wrong_sender_cannot_stop(self):
        self.say("/stop slow", sender=999)
        self.assertIsNone(self.control())


class ButtonTests(ControlCase):
    def test_the_extend_stop_and_wait_buttons_do_what_the_commands_do(self):
        self.handle(tap("act:extend:slow"))
        self.assertEqual(self.control()["extend_s"], 15 * 60)
        self.handle(tap("act:wait:slow"))
        self.assertTrue(self.control()["snooze_at"])
        self.handle(tap("act:stop:slow"))
        self.assertTrue(self.control()["stop"])
        self.assertEqual(len(self.sb.telegram.answers()), 3)

    def test_a_tap_from_the_wrong_sender_does_nothing(self):
        for data in ("act:extend:slow", "act:stop:slow", "act:wait:slow"):
            self.handle(tap(data, sender=999))
        self.assertIsNone(self.control())

    def test_the_revisit_buttons_make_a_proposal_or_end_the_nudge(self):
        self.sb.add_task("price", state="done")
        self.handle(tap("act:revisit:price"))
        (name,) = self.sb.names("proposed")
        self.assertEqual(self.sb.read_task("proposed", name)[0]["follows"], "price")
        self.handle(tap("act:revisit_done:price"))
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertEqual(self.sb.names("proposed"), [name])


class BacklogCommandTests(ControlCase):
    def test_backlog_lists_what_is_deferred_and_what_is_waiting_without_a_model(self):
        outcomes = self.sb.base / "state" / "outcomes"
        outcomes.mkdir(parents=True)
        (outcomes / "t-f1.json").write_text(json.dumps({
            "task": "t-f1", "status": "done", "kinds": ["pr", "deferred"],
            "refs": {"pr": {"url": "https://github.com/x/y/pull/1", "branch": "task/t"},
                     "deferred": {"file": "ROADMAP.md", "commit": "abcdef1234567", "items": ["- Rename the file"]}},
            "summary": "x", "finished": "2026-10-17T00:00:00Z", "message_ids": []}))
        self.say("/backlog")
        text = "\n".join(self.replies())
        self.assertIn("ROADMAP.md", text)
        self.assertIn("abcdef1", text)
        self.assertIn("Rename the file", text)
        self.assertEqual(self.sb.claude_calls(), [])

    def test_the_wrong_sender_gets_nothing(self):
        self.say("/backlog", sender=999)
        self.assertEqual(self.sb.telegram.requests, [])

    def test_the_new_commands_are_registered_with_telegram(self):
        source = (self.sb.base / "coordinator_bot.py").read_text()
        for command in ("extend", "stop", "backlog"):
            self.assertIn(f'"command": "{command}"', source)


if __name__ == "__main__":
    unittest.main()
