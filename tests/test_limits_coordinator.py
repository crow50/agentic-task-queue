"""Phase 1 (PLAN.md), coordinator side: share the pause, keep failed messages, check the sender.

The bot is driven two ways: handle_update()/replay_held() called from a
snippet in the sandbox (the same thing main()'s loop does, minus the
long-poll), and the real main() loop as a subprocess against the fake
Telegram. Stdlib only.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import unittest
from datetime import datetime, timedelta

from test_limits import AUTH_EXPIRED, UTC, LimitsSandbox, usage_limit

CHAT = 4242
USER = 777

PRELUDE = """
import json, sys
import coordinator_bot as bot
bot.COORD_DIR.mkdir(parents=True, exist_ok=True)
state = bot.load_state()
"""
POSTLUDE = """
bot.save_state(state)
print(json.dumps(state))
"""


def message(text, sender=CHAT, chat=CHAT, update_id=1):
    return {"update_id": update_id,
            "message": {"message_id": 5, "chat": {"id": chat}, "from": {"id": sender}, "text": text}}


def reaction(emoji="👍", user=CHAT, chat=CHAT):
    return {"update_id": 2, "message_reaction": {
        "chat": {"id": chat}, "user": {"id": user}, "message_id": 5,
        "new_reaction": [{"type": "emoji", "emoji": emoji}]}}


class BotCase(unittest.TestCase):
    def setUp(self):
        self.sb = LimitsSandbox()
        self.addCleanup(self.sb.close)

    def drive(self, body):
        code = PRELUDE + textwrap.dedent(body) + POSTLUDE
        out = self.sb.snippet(code)
        return json.loads(out.strip().splitlines()[-1])

    def handle(self, update):
        return self.drive(f"bot.handle_update(json.loads({json.dumps(json.dumps(update))}), state)")

    def replay(self):
        return self.drive("bot.replay_held(state)")

    def sent(self):
        return self.sb.texts()

    def others(self):
        return self.sb.claude_calls("other")

    def future(self, minutes=30):
        return datetime.now(UTC) + timedelta(minutes=minutes)


# ---------------------------------------------------------------- the sender check


class SenderCheckTests(BotCase):
    def test_the_right_chat_with_the_wrong_sender_gets_no_reply_and_no_claude_call(self):
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        self.handle(message("do the thing", sender=999))
        self.handle(message("/status", sender=999))
        self.handle(message("/cancel everything", sender=999))

        self.assertEqual(self.sent(), [])
        self.assertEqual(self.sb.claude_calls(), [])

    def test_the_right_sender_is_answered(self):
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        self.sb.set_scenario(other=[{"result": "On it."}])
        self.handle(message("do the thing", sender=USER))

        self.assertEqual(len(self.others()), 1)
        self.assertIn("On it.", self.sent())

    def test_a_reaction_from_the_wrong_user_is_ignored(self):
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        self.handle(reaction(user=999))
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertEqual(self.sent(), [])

    def test_an_anonymous_reaction_is_ignored(self):
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        update = reaction()
        del update["message_reaction"]["user"]
        update["message_reaction"]["actor_chat"] = {"id": CHAT}
        self.handle(update)
        self.assertEqual(self.sb.claude_calls(), [])

    def test_a_reaction_from_the_right_user_reaches_the_coordinator(self):
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        self.handle(reaction(user=USER))
        self.assertEqual(len(self.others()), 1)
        self.assertIn("[Reaction]", self.others()[0]["prompt"])

    def test_the_wrong_chat_is_still_ignored(self):
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        self.handle(message("hi", sender=USER, chat=-100999))
        self.assertEqual(self.sb.claude_calls(), [])

    def test_in_a_private_chat_the_user_defaults_to_the_chat_id(self):
        self.sb.configure(TELEGRAM_USER_ID=None)  # TELEGRAM_CHAT_ID is 4242, a private chat
        self.handle(message("hi", sender=999))
        self.assertEqual(self.sb.claude_calls(), [])
        self.handle(message("hi", sender=CHAT))
        self.assertEqual(len(self.others()), 1)

    def test_a_group_chat_without_a_user_id_answers_nobody(self):
        self.sb.configure(TELEGRAM_CHAT_ID="-100123", TELEGRAM_USER_ID=None)
        self.handle(message("hi", sender=USER, chat=-100123))
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertEqual(self.sent(), [])

    def test_a_group_chat_without_a_user_id_stops_the_bot_at_startup(self):
        self.sb.configure(TELEGRAM_CHAT_ID="-100123", TELEGRAM_USER_ID=None)
        proc = self.sb.python(str(self.sb.base / "coordinator_bot.py"), timeout=20)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("TELEGRAM_USER_ID", proc.stdout + proc.stderr)


# ---------------------------------------------------------------- sharing the pause


class SharedPauseTests(BotCase):
    def test_while_paused_it_says_so_keeps_the_message_and_calls_nothing(self):
        until = self.future(90)
        self.sb.write_pause(until)
        state = self.handle(message("build me a thing"))

        self.assertEqual(self.sb.claude_calls(), [])
        (reply,) = self.sent()
        self.assertIn("paused until", reply.lower())
        self.assertIn(until.astimezone().strftime("%H:%M"), reply)
        self.assertEqual([h["text"] for h in state["held"]], ["build me a thing"])

    def test_while_the_login_is_expired_it_says_so_and_keeps_the_message(self):
        self.sb.write_state("auth_failed", "2026-10-04T10:00:00Z\nFailed to authenticate\n")
        state = self.handle(message("build me a thing"))

        self.assertEqual(self.sb.claude_calls(), [])
        (reply,) = self.sent()
        self.assertIn("login expired", reply.lower())
        self.assertEqual([h["text"] for h in state["held"]], ["build me a thing"])

    def test_built_in_commands_still_work_while_paused(self):
        self.sb.write_pause(self.future())
        state = self.handle(message("/status"))
        (reply,) = self.sent()
        self.assertIn("Queue status", reply)
        self.assertEqual(state.get("held", []), [])

    def test_a_reaction_while_paused_is_held_too(self):
        self.sb.write_pause(self.future())
        state = self.handle(reaction())
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertEqual(len(state["held"]), 1)
        self.assertIn("[Reaction]", state["held"][0]["text"])

    def test_a_held_message_is_replayed_once_the_pause_is_over(self):
        self.sb.write_pause(self.future())
        self.handle(message("build me a thing"))
        self.sb.set_scenario(other=[{"result": "Built it."}])

        still = self.replay()
        self.assertEqual(len(still["held"]), 1, "still paused: the message waits")
        self.assertEqual(self.sb.claude_calls(), [])

        self.sb.write_pause(datetime.now(UTC) - timedelta(minutes=1))
        done = self.replay()

        self.assertEqual(done["held"], [])
        (call,) = self.others()
        self.assertIn("build me a thing", call["prompt"])
        self.assertTrue(any("Built it." in t for t in self.sent()), self.sent())

    def test_held_messages_replay_in_order(self):
        self.sb.write_pause(self.future())
        self.handle(message("first", update_id=1))
        self.handle(message("second", update_id=2))
        self.sb.write_pause(datetime.now(UTC) - timedelta(minutes=1))
        self.replay()
        self.assertEqual([c["prompt"].strip().splitlines()[-1] for c in self.others()][:2],
                         ["first", "second"])

    def test_a_limit_hit_by_the_bot_itself_pauses_the_queue_and_keeps_the_message(self):
        self.sb.set_scenario(other=[usage_limit(2 * 3600)])
        state = self.handle(message("build me a thing"))

        self.assertEqual([h["text"] for h in state["held"]], ["build me a thing"])
        until = self.sb.read_pause()
        self.assertIsNotNone(until, "the limit is account-wide, so the dispatcher must stop too")
        self.assertGreater(until, self.future(100))
        (reply,) = self.sent()  # the reply to the user, not a second notice
        self.assertIn("paused until", reply.lower())

    def test_a_login_failure_hit_by_the_bot_itself_stops_the_queue_and_keeps_the_message(self):
        self.sb.set_scenario(other=[AUTH_EXPIRED])
        state = self.handle(message("build me a thing"))

        self.assertEqual(len(state["held"]), 1)
        self.assertTrue(self.sb.state_path("auth_failed").exists())
        (reply,) = self.sent()
        self.assertIn("login expired", reply.lower())

    def test_other_failures_are_not_held(self):
        self.sb.set_scenario(other=[{"exit": 1, "stdout": "", "stderr": "boom"}])
        state = self.handle(message("build me a thing"))
        self.assertEqual(state.get("held", []), [])
        self.assertTrue(any("Coordinator error" in t for t in self.sent()))

    def test_coordinator_calls_are_logged_to_usage_jsonl(self):
        self.handle(message("hello"))
        lines = self.sb.usage()
        self.assertEqual([u["label"] for u in lines], ["coordinator"])
        self.assertEqual(lines[0]["exit"], 0)
        self.assertEqual(lines[0]["model"], "claude-sonnet-5")

    def test_failed_coordinator_calls_are_logged_to_failed_calls_log(self):
        self.sb.set_scenario(other=[{"exit": 1, "stdout": "", "stderr": "boom stderr"}])
        self.handle(message("hello"))
        text = (self.sb.base / "logs" / "failed-calls.log").read_text()
        self.assertIn("boom stderr", text)
        self.assertIn("coordinator", text)


# ---------------------------------------------------------------- the real loop


class MainLoopTests(BotCase):
    def start_bot(self):
        log = open(self.sb.root / "bot.out", "w")
        self.addCleanup(log.close)
        proc = subprocess.Popen(
            [sys.executable, str(self.sb.base / "coordinator_bot.py")],
            cwd=self.sb.base, env=self.sb.env(), stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        def stop():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)

        self.addCleanup(stop)
        return proc

    def wait_for(self, condition, what, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return
            time.sleep(0.1)
        out = (self.sb.root / "bot.out").read_text() if (self.sb.root / "bot.out").exists() else ""
        self.fail(f"timed out waiting for {what}\n--- bot output\n{out}")

    def test_a_message_that_arrives_while_paused_is_answered_after_the_pause(self):
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        self.sb.set_scenario(other=[{"result": "Built it."}])
        self.sb.write_pause(self.future(60))
        self.sb.telegram.updates = [message("build me a thing", sender=USER)]
        proc = self.start_bot()

        self.wait_for(lambda: any("paused until" in t.lower() for t in self.sent()), "the paused reply")
        self.assertEqual(self.sb.claude_calls(), [])

        self.sb.write_pause(datetime.now(UTC) - timedelta(minutes=1))
        self.wait_for(lambda: any("Built it." in t for t in self.sent()), "the replayed answer")
        self.assertEqual(len(self.others()), 1)

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
        state = json.loads((self.sb.base / "coordinator" / "state.json").read_text())
        self.assertEqual(state["held"], [])
        self.assertEqual(state["offset"], 1)


if __name__ == "__main__":
    unittest.main()
