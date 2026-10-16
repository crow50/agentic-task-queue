"""Phase 7 item 5: a secret pasted into Telegram is not read, kept or forwarded.

The bridge runs the dispatcher's shared SECRET_PATTERNS on every incoming message before
anything is logged or stored. A hit never reaches the coordinator model, leaves only a
redacted line in logs/coordinator.log and coordinator/state.json, triggers one
deleteMessage on the original, and gets one reply. Text sent to Telegram is redacted too.
"""

import json
import signal
import subprocess
import sys
import time
import os
import unittest

from test_baseline import BaselineCase
from test_limits_coordinator import CHAT, USER, BotCase, message

NOTION = "ntn_" + "Zq9Xw8Vu7Ts6Rp5On4Ml3Kj2Ih1Gf0Ed9Cb8Aa7"
ANTHROPIC = "sk-ant-api03-" + "Zq9Xw8Vu7Ts6Rp5On4Ml3Kj2"
GITHUB = "ghp_" + "Zq9Xw8Vu7Ts6Rp5On4Ml3Kj2Ih1Gf0Ed9Cb8"


class SharedFunctionTests(BaselineCase):
    def kinds(self, text):
        code = "import dispatcher, sys; print(dispatcher.secret_kinds(sys.argv[1]))"
        return self.sb.python("-c", code, text).stdout.strip()

    def redact(self, text):
        code = "import dispatcher, sys; print(dispatcher.redact_secrets(sys.argv[1]))"
        return self.sb.python("-c", code, text).stdout.strip()

    def test_each_kind_in_the_plan_is_recognised(self):
        samples = {
            "GitHub token": GITHUB,
            "Anthropic key": ANTHROPIC,
            "Telegram bot token": "123456789:AAH" + "x" * 32,
            "Notion token": NOTION,
            "DigitalOcean token": "dop_v1_" + "a" * 40,
            "AWS access key": "AKIA" + "ABCDEFGHIJKLMNOP",
            "private key": "-----BEGIN OPENSSH PRIVATE KEY-----",
            "password or token": "api_key = 'abcdef0123456789abcdef'",
        }
        for kind, sample in samples.items():
            with self.subTest(kind):
                self.assertEqual(self.kinds(f"here: {sample}"), f"['{kind}']")

    def test_ordinary_text_is_not_a_secret(self):
        for text in ("please add a README mention", "the token limit is 4096", "https://github.com/crow50/x/pull/3",
                     "password = None", "call 123456789 now"):
            with self.subTest(text):
                self.assertEqual(self.kinds(text), "[]")

    def test_redaction_removes_the_value_and_says_what_it_was(self):
        out = self.redact(f"key {NOTION} and {GITHUB}")
        self.assertNotIn(NOTION, out)
        self.assertNotIn(GITHUB, out)
        self.assertIn("[redacted Notion token]", out)
        self.assertIn("[redacted GitHub token]", out)

    def test_a_generic_assignment_keeps_its_name(self):
        out = self.redact("password = 'hunter2hunter2hunter2'")
        self.assertTrue(out.startswith("password = '[redacted"), out)
        self.assertNotIn("hunter2", out)


class IncomingMessageTests(BotCase):
    def deletes(self):
        return self.sb.telegram.calls("deleteMessage")

    def test_a_token_is_not_sent_to_the_model_and_is_answered_once(self):
        self.handle(message(f"here is my notion key {NOTION} please use it"))

        self.assertEqual(self.sb.claude_calls(), [])
        (delete,) = self.deletes()
        self.assertEqual(delete.params["message_id"], "5")
        self.assertEqual(delete.params["chat_id"], str(CHAT))
        (reply,) = self.sent()
        self.assertIn("That looked like a Notion token.", reply)
        self.assertIn("I did not read or keep it", reply)
        self.assertIn(".env", reply)
        self.assertIn("rotate", reply)
        self.assertNotIn(NOTION, reply)

    def test_state_json_keeps_a_redacted_line_and_never_the_value(self):
        state = self.handle(message(f"{NOTION}"))
        self.assertNotIn(NOTION, json.dumps(state))
        self.assertIn("Notion token", json.dumps(state["redacted"]))
        self.assertEqual(state.get("held") or [], [])

    def test_a_failed_delete_is_ignored(self):
        self.sb.telegram.queue_response("deleteMessage", status=400)
        self.handle(message(f"{ANTHROPIC}"))
        self.assertEqual(len(self.sent()), 1)  # still told, once
        self.assertEqual(self.sb.claude_calls(), [])

    def test_a_secret_in_a_slash_command_is_caught_too(self):
        self.handle(message(f"/cancel {GITHUB}"))
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertIn("GitHub token", self.sent()[0])

    def test_an_ordinary_message_is_unaffected(self):
        self.sb.set_scenario(other=[{"result": "On it."}])
        self.handle(message("add a README mention"))
        self.assertEqual(self.deletes(), [])
        self.assertEqual(len(self.others()), 1)
        self.assertIn("On it.", self.sent())

    def test_a_secret_from_the_wrong_sender_is_ignored_like_any_message(self):
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        self.handle(message(f"{NOTION}", sender=999))
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.deletes(), [])


class LoopLogTests(BotCase):
    """The real main() loop: the log file is the thing that must stay clean."""

    def start_bot(self):
        out = open(self.sb.root / "bot.out", "w")
        self.addCleanup(out.close)
        proc = subprocess.Popen(
            [sys.executable, str(self.sb.base / "coordinator_bot.py")],
            cwd=self.sb.base, env=self.sb.env(), stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
        )

        def stop():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)

        self.addCleanup(stop)
        return proc

    def test_the_token_is_in_neither_log_file_nor_state_nor_output(self):
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        self.sb.set_scenario(other=[{"result": "Fine."}])
        self.sb.telegram.updates = [message(f"my key is {NOTION}", sender=USER, update_id=1)]
        proc = self.start_bot()
        deadline = time.time() + 20
        while time.time() < deadline and not self.sent():
            time.sleep(0.1)
        self.assertTrue(self.sent(), "no reply")
        self.sb.telegram.updates = [message("an ordinary message", sender=USER, update_id=2)]
        while time.time() < deadline and "Fine." not in self.sent():
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)

        for rel in ("logs/coordinator.log", "coordinator/state.json"):
            text = (self.sb.base / rel).read_text()
            self.assertNotIn(NOTION, text, rel)
            self.assertNotIn(NOTION[:14], text, rel)
        self.assertNotIn(NOTION, (self.sb.root / "bot.out").read_text())
        log = (self.sb.base / "logs" / "coordinator.log").read_text()
        self.assertIn("Notion token", log)  # a redacted line is there
        self.assertEqual(len(self.others()), 1)  # only the ordinary message reached the model


class OutgoingTests(BaselineCase):
    def sent_everything(self):
        parts = [r.params.get("text", "") for r in self.sb.telegram.calls("sendMessage")]
        parts += [r.body.decode("utf-8", "replace") for r in self.sb.telegram.calls("sendDocument")]
        return "\n".join(parts)

    def test_a_key_in_a_worker_report_does_not_reach_telegram(self):
        self.sb.set_scenario(worker=[{"result": f"Done. I used {ANTHROPIC} and it worked."}])
        self.sb.add_task("t")
        self.sb.run()

        sent = self.sent_everything()
        self.assertIn("Task done: t.md", sent)
        self.assertNotIn(ANTHROPIC, sent)
        self.assertIn("[redacted Anthropic key]", sent)

    def test_a_key_in_the_attachment_of_a_long_report_is_redacted_too(self):
        report = "line of findings\n" * 400 + f"and the key {GITHUB}\n"
        self.sb.set_scenario(worker=[{"result": report}])
        self.sb.add_task("t")
        self.sb.run()

        self.assertTrue(self.sb.telegram.calls("sendDocument"), "the long report should be an attachment")
        self.assertNotIn(GITHUB, self.sent_everything())

    def test_a_failure_report_is_redacted(self):
        self.sb.set_scenario(worker=[{"result": f"I could not finish, token {NOTION}"}],
                             review=[{"result": f"VERDICT: FAIL\n- leaked {NOTION}"}])
        self.sb.add_task("t", max_attempts="1")
        self.sb.run()
        self.assertNotIn(NOTION, self.sent_everything())


if __name__ == "__main__":
    unittest.main()
