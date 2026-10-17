"""Phase 8 Part B items 1 to 5 and 8: the /menu, button taps, confirmation of anything that spends, and the commands
that answer without a model. Taps reach the bridge as callback_query updates, through the same sender check as messages."""

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from test_limits import iso  # noqa: E402
from test_limits_coordinator import CHAT, USER, BotCase, message  # noqa: E402

UTC = timezone.utc


def tap(data, sender=USER, chat=CHAT, message_id=50, update_id=3):
    return {"update_id": update_id, "callback_query": {
        "id": "cb-1", "from": {"id": sender}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": chat}}}}


class MenuCase(BotCase):
    def setUp(self):
        super().setUp()
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        (self.sb.base / "tasks" / "examples").mkdir(parents=True)
        for name in ("weekly-improver.md", "skill-from-task.md"):
            (self.sb.base / "tasks" / "examples" / name).write_text((REPO / "tasks" / "examples" / name).read_text())

    def tapped(self, data, **kwargs):
        return self.handle(tap(data, **kwargs))

    def edits(self):
        return self.sb.telegram.edits()

    def last_edit(self):
        return self.edits()[-1].params

    def buttons(self, request):
        rows = json.loads(request.params["reply_markup"])["inline_keyboard"]
        return [(b["text"], b["callback_data"]) for row in rows for b in row]

    def pending(self, prefix):
        return [n for n in self.sb.names("pending") if n.startswith(prefix)]

    def done_task(self, name, minutes_ago=0):
        self.sb.add_task(name, state="done")
        import os
        stamp = (datetime.now() - timedelta(minutes=minutes_ago)).timestamp()
        os.utime(self.sb.base / "tasks" / "done" / f"{name}.md", (stamp, stamp))

    def usage_rows(self, rows):
        self.sb.write_usage([{"time": iso(datetime.now(UTC)), "label": "worker", "model": "m", "exit": 0, "status": "ok",
                              "is_error": False, "usage": {}, "modelUsage": {}, "total_cost_usd": cost, "cost_usd": cost,
                              "task": task} for task, cost in rows])


class MenuTests(MenuCase):
    def test_the_menu_is_one_message_with_buttons_and_no_model_call(self):
        self.handle(message("/menu", sender=USER))
        self.assertEqual(self.sb.claude_calls(), [])
        (menu,) = self.sb.telegram.calls("sendMessage")
        labels = [label for label, _ in self.buttons(menu)]
        for wanted in ("Status", "Usage", "Pause", "Run learning review", "Create skill from a task", "Name your assistant"):
            self.assertIn(wanted, labels)
        for _, data in self.buttons(menu):
            self.assertLessEqual(len(data.encode()), 64)
            self.assertTrue(data.startswith("act:"))

    def test_the_menu_shows_resume_instead_of_pause_while_the_queue_is_paused(self):
        self.sb.write_pause(datetime.now(UTC) + timedelta(hours=1))
        self.handle(message("/menu", sender=USER))
        labels = [label for label, _ in self.buttons(self.sb.telegram.calls("sendMessage")[0])]
        self.assertIn("Resume", labels)
        self.assertNotIn("Pause", labels)

    def test_callbacks_are_registered_with_the_long_poll_and_the_menu_with_setMyCommands(self):
        source = (REPO / "coordinator_bot.py").read_text()
        self.assertIn('"callback_query"', source)
        self.assertIn('"command": "menu"', source)
        self.assertIn('"command": "usage"', source)


class SenderTests(MenuCase):
    def test_a_tap_from_the_wrong_sender_does_nothing_at_all(self):
        self.tapped("act:learn_ok", sender=999)
        self.tapped("act:pause", sender=999)
        self.tapped("act:status", chat=-100999)
        self.assertEqual(self.sb.telegram.requests, [])
        self.assertEqual(self.pending("weekly-improver"), [])
        self.assertFalse((self.sb.base / "state" / "paused_until").exists())

    def test_every_tap_from_the_right_sender_is_answered_so_the_button_stops_spinning(self):
        for data in ("act:status", "act:usage", "act:nonsense", "garbage"):
            self.tapped(data)
        answers = self.sb.telegram.answers()
        self.assertEqual(len(answers), 4)
        self.assertTrue(all(a.params["callback_query_id"] == "cb-1" for a in answers))


class PlainActionTests(MenuCase):
    def test_status_edits_the_menu_message_with_the_counts_and_calls_no_model(self):
        self.sb.add_task("waiting")
        self.tapped("act:status")
        self.assertIn("pending: 1", self.last_edit()["text"])
        self.assertEqual(self.last_edit()["message_id"], "50")
        self.assertEqual(self.sb.claude_calls(), [])

    def test_pause_and_resume_write_and_clear_the_shared_pause(self):
        self.tapped("act:pause")
        self.assertTrue((self.sb.base / "state" / "paused_until").exists())
        self.assertIn("Resume", [label for label, _ in self.buttons(self.edits()[-1])])
        self.tapped("act:resume")
        self.assertFalse((self.sb.base / "state" / "paused_until").exists())

    def test_an_unknown_action_is_refused_politely_and_changes_nothing(self):
        self.tapped("act:rm_rf")
        self.assertIn("don't know", self.last_edit()["text"])
        self.assertEqual(self.sb.claude_calls(), [])


class SpendConfirmTests(MenuCase):
    def test_learning_review_shows_the_average_cost_and_needs_a_second_tap(self):
        self.usage_rows([("weekly-improver-20260101-060000", 0.30), ("weekly-improver-20260108-060000", 0.10), ("other", 5.0)])
        self.tapped("act:learn")
        prompt = self.last_edit()
        self.assertIn("$0.20", prompt["text"])
        self.assertEqual(self.pending("weekly-improver"), [], "the first tap creates nothing")
        self.assertIn("Confirm", [label for label, _ in self.buttons(self.edits()[-1])])

    def test_one_tap_plus_confirm_creates_one_quiet_task_and_two_confirms_still_one(self):
        self.tapped("act:learn")
        self.tapped("act:learn_ok")
        self.tapped("act:learn_ok")
        (name,) = self.pending("weekly-improver")
        meta, _ = self.sb.read_task("pending", name)
        self.assertEqual(meta["quiet"], "true")
        self.assertNotIn("schedule", meta)
        self.assertNotIn("__QUEUE_DIR__", (self.sb.base / "tasks" / "pending" / f"{name}.md").read_text())
        self.assertIn("already", self.last_edit()["text"])
        self.assertEqual(self.sb.claude_calls(), [], "a button enqueues; it does not call a model")

    def test_with_no_history_it_says_there_is_nothing_to_average(self):
        self.tapped("act:learn")
        self.assertIn("no earlier run", self.last_edit()["text"])

    def test_a_confirmed_task_is_not_held_for_a_second_approval(self):
        self.sb.configure(APPROVAL_MIN_TIER="1")
        self.tapped("act:learn_ok")
        self.assertEqual(len(self.pending("weekly-improver")), 1)
        self.assertEqual(self.sb.names("proposed"), [])

    def test_skill_from_task_lists_the_last_eight_done_tasks_newest_first(self):
        for n in range(10):
            self.done_task(f"job-{n}", minutes_ago=100 - n)
        self.tapped("act:skillpick")
        labels = [label for label, data in self.buttons(self.edits()[-1]) if data.startswith("act:skill:")]
        self.assertEqual(labels, [f"job-{n}" for n in range(9, 1, -1)])

    def test_picking_a_task_asks_for_confirmation_then_enqueues_a_skill_task_for_it(self):
        self.done_task("report-1")
        self.tapped("act:skill:report-1")
        self.assertIn("Confirm", [label for label, _ in self.buttons(self.edits()[-1])])
        self.assertEqual(self.pending("skill-from-task"), [])
        self.tapped("act:skill_ok:report-1")
        (name,) = self.pending("skill-from-task")
        text = (self.sb.base / "tasks" / "pending" / f"{name}.md").read_text()
        self.assertIn("tasks/done/report-1.md", text)
        self.assertNotIn("__", text)
        self.assertIn("quiet: true", text)

    def test_a_task_id_too_long_for_a_button_still_works(self):
        stem = "a-very-long-task-name-" + "x" * 70
        self.done_task(stem)
        self.tapped("act:skillpick")
        data = [d for label, d in self.buttons(self.edits()[-1]) if label.startswith("a-very-long")]
        self.assertEqual(len(data), 1)
        self.assertLessEqual(len(data[0].encode()), 64)
        self.tapped(data[0])
        confirm = [d for label, d in self.buttons(self.edits()[-1]) if label == "Confirm"]
        self.assertLessEqual(len(confirm[0].encode()), 64)
        self.tapped(confirm[0])
        self.assertEqual(len(self.pending("skill-from-task")), 1)

    def test_a_skill_for_a_task_that_is_not_done_is_refused(self):
        self.tapped("act:skill_ok:ghost")
        self.assertEqual(self.pending("skill-from-task"), [])
        self.assertIn("ghost", self.last_edit()["text"])


class CommandTests(MenuCase):
    def say(self, text):
        self.handle(message(text, sender=USER))
        return self.sb.telegram.texts()[-1]

    def test_status_with_an_id_shows_state_tier_attempts_and_outcome_without_a_model(self):
        self.sb.add_task("shipped", state="done", attempts="2", tier="1")
        path = self.sb.base / "tasks" / "done" / "shipped.md"
        path.write_text(path.read_text() + "\n## Result (attempt 2, 2026-10-16T10:00:00Z)\n\nWrote the report.\n")
        text = self.say("/status shipped")
        for part in ("shipped", "done", "tier 1", "attempts 2", "Wrote the report."):
            self.assertIn(part, text)
        self.assertEqual(self.sb.claude_calls(), [])

    def test_status_with_an_unknown_id_says_so(self):
        self.assertIn("No task named 'nope'", self.say("/status nope"))

    def test_status_alone_is_still_the_queue_counts(self):
        self.sb.add_task("a")
        self.assertIn("pending: 1", self.say("/status"))

    def test_usage_reports_today_from_the_log_and_calls_no_model(self):
        self.usage_rows([("t1", 0.25), ("t2", 0.50)])
        text = self.say("/usage")
        self.assertIn("$0.75", text)
        self.assertIn("2 calls", text)
        self.assertEqual(self.sb.claude_calls(), [])

    def test_usage_with_an_empty_log_says_so(self):
        self.assertIn("No usage", self.say("/usage"))


if __name__ == "__main__":
    unittest.main()
