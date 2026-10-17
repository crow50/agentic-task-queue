"""Every cancel, retry, approve and reject leaves a line in logs/actions.log, and the coordinator is told what `proposed/` means.

A task came back several times on the first real run and nothing recorded who had cancelled or retried it: the CLI commands
configure no logging. The coordinator also told the human that a safety check had "intercepted" a task that was only waiting
for the 👍 every tier 2 task waits for.
"""

import json
import re
import unittest

from test_limits_coordinator import CHAT, BotCase, message
from test_verifier import REPO, VerifyCase


def actions(sandbox):
    path = sandbox.base / "logs" / "actions.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class ActionLogTests(VerifyCase):
    def cli(self, *args):
        return self.sb.python(str(self.sb.base / "dispatcher.py"), *args)

    def test_a_cancel_and_a_retry_from_the_command_line_are_recorded_with_the_result(self):
        self.sb.add_task("t")
        self.cli("cancel", "t")
        self.cli("retry", "t")
        self.cli("cancel", "nope")
        first, second, third = actions(self.sb)
        self.assertEqual((first["action"], first["task"], first["ok"]), ("cancel", "t", True))
        self.assertEqual((second["action"], second["task"], second["ok"]), ("retry", "t", True))
        self.assertEqual((third["action"], third["task"], third["ok"]), ("cancel", "nope", False))
        for line in (first, second, third):
            self.assertTrue(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", line["time"]))
            self.assertIn("message", line)

    def test_the_line_says_where_the_command_came_from(self):
        self.sb.add_task("t")
        self.cli("cancel", "t")
        (line,) = actions(self.sb)
        self.assertEqual(line["source"], "cli, no terminal")  # the test has no tty, like the coordinator or cron

    def test_nothing_secret_is_in_the_line(self):
        self.sb.add_task("t")
        self.cli("cancel", "t")
        (line,) = actions(self.sb)
        self.assertEqual(sorted(line), ["action", "message", "ok", "source", "task", "time"])


class BotActionTests(BotCase):
    def test_commands_and_approvals_from_telegram_are_recorded_as_telegram(self):
        self.sb.add_task("waiting", state="failed")
        self.handle(message("/retry waiting", sender=CHAT))
        self.handle(message("/cancel waiting", sender=CHAT))
        self.handle(message("/approve ghost", sender=CHAT))
        lines = actions(self.sb)
        self.assertEqual([(x["action"], x["task"], x["ok"], x["source"]) for x in lines],
                         [("retry", "waiting", True, "telegram"), ("cancel", "waiting", True, "telegram"),
                          ("approve", "ghost", False, "telegram")])


class TemplateWordingTests(VerifyCase):
    def text(self):
        return " ".join((REPO / "coordinator" / "CLAUDE.md.template").read_text().split())

    def test_the_template_says_what_waiting_in_proposed_means(self):
        text = self.text()
        self.assertRegex(text, r"tasks/proposed/")
        self.assertRegex(text, r"waiting for your 👍")
        self.assertRegex(text, r"not a safety (check|gate)|nothing is wrong")

    def test_the_template_forbids_moving_a_task_out_of_proposed(self):
        self.assertRegex(self.text(), r"(?i)never move a task out of `proposed/`")

    def test_the_template_tells_it_to_check_before_saying_where_a_task_is(self):
        self.assertRegex(self.text(), r"Before you say where a task is[^.]*look in tasks/")


if __name__ == "__main__":
    unittest.main()
