"""Phase 9 Part A item 2 and 7, from the phone: reply to a result, /continue, 👎 on a result, /tasks.

Each of these is code, not a model call: the bridge looks the result message up in state/outcomes/ and the new task
is written by the dispatcher's own function. The sender check applies to all of them."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from test_limits_coordinator import CHAT, USER, BotCase, message, reaction

RESULT_ID = 321


def reply_to(text, to=RESULT_ID, sender=USER, update_id=7):
    return {"update_id": update_id, "message": {
        "message_id": 9, "chat": {"id": CHAT}, "from": {"id": sender}, "text": text,
        "reply_to_message": {"message_id": to, "chat": {"id": CHAT}}}}


def thumbs_down(message_id=RESULT_ID, user=USER):
    update = reaction("👎", user=user)
    update["message_reaction"]["message_id"] = message_id
    return update


class FollowCase(BotCase):
    def setUp(self):
        super().setUp()
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        self.sb.add_task("r", state="done")
        outcomes = self.sb.base / "state" / "outcomes"
        outcomes.mkdir(parents=True)
        (outcomes / "r.json").write_text(json.dumps({
            "task": "r", "status": "done", "kinds": ["report"], "refs": {"report": {"chars": 40}},
            "summary": "Three options.", "session_id": "s-1", "cwd": str(self.sb.base / "workspace"), "worktree": None,
            "cost_usd": 0.1, "finished": "2026-10-17T00:00:00Z", "message_ids": [RESULT_ID]}))
        (outcomes / "r.report.md").write_text("Option one, option two, option three.")

    def pending(self):
        return self.sb.names("pending")

    def replies(self):
        return self.sb.texts()


class ReplyToAResultTests(FollowCase):
    def test_a_text_reply_to_a_result_makes_exactly_one_followup_and_calls_no_model(self):
        self.handle(reply_to("dig into the second option"))

        self.assertEqual(self.pending(), ["r-f1"])
        meta, body = self.sb.read_task("pending", "r-f1")
        self.assertEqual(meta["follows"], "r")
        self.assertIn("dig into the second option", body)
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertTrue(any("r-f1" in text for text in self.replies()))

    def test_a_reply_to_some_other_message_is_an_ordinary_message(self):
        self.sb.set_scenario(other=[{"result": "Sure."}])
        self.handle(reply_to("dig into the second option", to=999))

        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.others()), 1)

    def test_a_reply_from_the_wrong_sender_does_nothing(self):
        self.handle(reply_to("dig deeper", sender=999))
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.sb.telegram.requests, [])

    def test_a_reply_that_holds_a_secret_is_dropped_before_anything_is_queued(self):
        self.handle(reply_to("use ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2))
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.sb.claude_calls(), [])

    def test_a_reply_to_a_result_whose_task_is_gone_says_so_instead_of_guessing(self):
        (self.sb.base / "tasks" / "done" / "r.md").unlink()
        self.handle(reply_to("dig deeper"))
        self.assertEqual(self.pending(), [])
        self.assertTrue(any("r" in text and "no longer" in text for text in self.replies()), self.replies())

    def test_a_command_in_a_reply_is_still_a_command(self):
        self.handle(reply_to("/status"))
        self.assertEqual(self.pending(), [])
        self.assertTrue(any("pending" in text for text in self.replies()))


class ContinueCommandTests(FollowCase):
    def test_continue_with_an_id_and_text_makes_a_followup(self):
        self.handle(message("/continue r dig into the second option", sender=USER))
        self.assertEqual(self.pending(), ["r-f1"])
        self.assertEqual(self.sb.claude_calls(), [])

    def test_continue_with_no_id_lists_the_finished_tasks(self):
        self.handle(message("/continue", sender=USER))
        self.assertEqual(self.pending(), [])
        text = "\n".join(self.replies())
        self.assertIn("r", text)
        self.assertIn("/continue", text)

    def test_continue_with_an_id_and_no_text_asks_what_to_change(self):
        self.handle(message("/continue r", sender=USER))
        self.assertEqual(self.pending(), [])
        self.assertTrue(any("What should change" in text for text in self.replies()), self.replies())

    def test_continue_on_a_task_that_does_not_exist_says_so(self):
        self.handle(message("/continue ghost more please", sender=USER))
        self.assertEqual(self.pending(), [])
        self.assertTrue(any("ghost" in text for text in self.replies()))

    def test_the_wrong_sender_cannot_continue_anything(self):
        self.handle(message("/continue r more", sender=999))
        self.assertEqual(self.pending(), [])

    def test_tasks_lists_the_last_finished_tasks_with_what_each_made(self):
        self.sb.add_task("failed-one", state="failed")
        self.handle(message("/tasks", sender=USER))
        text = "\n".join(self.replies())
        self.assertIn("r", text)
        self.assertIn("a report", text)
        self.assertIn("failed-one", text)
        self.assertEqual(self.sb.claude_calls(), [])

    def test_the_new_commands_are_registered_with_telegram(self):
        source = (self.sb.base / "coordinator_bot.py").read_text()
        self.assertIn('"command": "tasks"', source)
        self.assertIn('"command": "continue"', source)


class ThumbsDownTests(FollowCase):
    def test_a_thumbs_down_on_a_result_asks_what_was_wrong_and_calls_no_model(self):
        self.handle(thumbs_down())
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertEqual(self.pending(), [])
        self.assertTrue(any("What was wrong" in text for text in self.replies()), self.replies())

    def test_the_next_message_becomes_the_followup(self):
        self.handle(thumbs_down())
        state = self.handle(message("it missed the second option", sender=USER))

        self.assertEqual(self.pending(), ["r-f1"])
        self.assertIn("it missed the second option", self.sb.read_task("pending", "r-f1")[1])
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertIsNone(state.get("awaiting_followup"))

    def test_skip_drops_the_question_and_the_next_message_is_ordinary(self):
        self.sb.set_scenario(other=[{"result": "Hi."}])
        self.handle(thumbs_down())
        self.handle(message("/skip", sender=USER))
        self.handle(message("hello", sender=USER))
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.others()), 1)

    def test_an_answer_that_comes_an_hour_later_is_not_taken_for_one(self):
        self.sb.set_scenario(other=[{"result": "Hi."}])
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.drive(f'state["awaiting_followup"] = {{"task": "r", "at": "{old}"}}')
        self.handle(message("unrelated", sender=USER))
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.others()), 1)

    def test_a_thumbs_down_on_some_other_message_still_goes_to_the_coordinator(self):
        self.handle(thumbs_down(message_id=999))
        self.assertEqual(len(self.others()), 1)
        self.assertIn("[Reaction]", self.others()[0]["prompt"])

    def test_a_thumbs_down_from_the_wrong_user_does_nothing(self):
        self.handle(thumbs_down(user=999))
        self.assertEqual(self.sb.telegram.requests, [])


if __name__ == "__main__":
    unittest.main()
