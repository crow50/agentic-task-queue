"""Phase 8 Part B: taps on the buttons that alerts carry, the assistant's name, and the two example tasks the menu enqueues."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from test_baseline import PASS  # noqa: E402
from test_limits import AUTH_EXPIRED, BOOM  # noqa: E402
from test_learning import OpenerCase  # noqa: E402
from test_visibility_menu import MenuCase  # noqa: E402


class AlertTapTests(MenuCase):
    def test_approve_and_reject_buttons_do_what_the_reaction_does(self):
        self.sb.configure(APPROVAL_MIN_TIER="1")
        self.sb.add_task("writer", allowed_tools="Read,Write")
        self.sb.add_task("other", allowed_tools="Read,Write")
        self.sb.run()
        self.assertEqual(self.sb.names("proposed"), ["other", "writer"])
        self.tapped("act:approve:writer")
        self.tapped("act:reject:other")
        self.assertEqual(self.sb.names("pending"), ["writer"])
        self.assertEqual(self.sb.names("cancelled"), ["other"])
        self.assertIn("Approved writer", self.last_edit()["text"] + "".join(e.params["text"] for e in self.edits()))

    def test_tapping_approve_twice_says_it_was_already_done(self):
        self.sb.configure(APPROVAL_MIN_TIER="1")
        self.sb.add_task("writer", allowed_tools="Read,Write")
        self.sb.run()
        self.tapped("act:approve:writer")
        self.tapped("act:approve:writer")
        self.assertEqual(self.sb.names("pending"), ["writer"])
        self.assertIn("not waiting for approval", self.last_edit()["text"])

    def test_the_alert_text_stays_and_the_outcome_is_added_below_it(self):
        self.sb.add_task("doomed", state="failed")
        update = {"update_id": 5, "callback_query": {
            "id": "q", "from": {"id": 777}, "data": "act:retry:doomed",
            "message": {"message_id": 61, "chat": {"id": 4242}, "text": "❌ Task failed: doomed.md"}}}
        self.handle(update)
        self.assertEqual(self.sb.names("pending"), ["doomed"])
        text = self.last_edit()["text"]
        self.assertTrue(text.startswith("❌ Task failed: doomed.md"))
        self.assertIn("Requeued doomed.md", text)
        self.assertNotIn("reply_markup", self.last_edit(), "the buttons are gone once used")

    def test_cancel_and_retry_on_a_finished_but_unpublished_task(self):
        self.sb.add_task("held", pending_publish="1", held_until="2099-01-01T00:00:00Z")
        self.tapped("act:retry:held")
        meta, _ = self.sb.read_task("pending", "held")
        self.assertNotIn("held_until", meta)
        self.assertIn("pending_publish", meta, "the finished work is still there to publish")
        self.tapped("act:cancel:held")
        self.assertEqual(self.sb.names("cancelled"), ["held"])

    def test_open_sends_the_tasks_details_as_a_new_message_and_keeps_the_alert(self):
        self.sb.add_task("shipped", state="done")
        self.tapped("act:open:shipped")
        self.assertIn("shipped: done", self.sb.telegram.texts()[-1])
        self.assertEqual(self.edits(), [])

    def test_resume_clears_a_pause_and_checks_an_expired_login_with_one_cheap_call(self):
        from datetime import datetime, timedelta, timezone
        self.sb.write_pause(datetime.now(timezone.utc) + timedelta(hours=2))
        self.sb.write_state("auth_failed", "2026-10-16T00:00:00Z\nexpired\n")
        self.sb.set_scenario(other=[{"result": "ok"}])
        self.tapped("act:resume")
        self.assertFalse((self.sb.base / "state" / "paused_until").exists())
        self.assertFalse((self.sb.base / "state" / "auth_failed").exists())
        self.assertIn("login works again", self.last_edit()["text"])
        self.assertEqual(len(self.sb.claude_calls()), 1)

    def test_resume_with_the_login_still_bad_says_so_and_keeps_the_flag(self):
        self.sb.write_state("auth_failed", "2026-10-16T00:00:00Z\nexpired\n")
        self.sb.set_scenario(other=[AUTH_EXPIRED])
        self.tapped("act:resume")
        self.assertTrue((self.sb.base / "state" / "auth_failed").exists())
        self.assertIn("still fails", self.last_edit()["text"])


class AssistantNameTests(MenuCase):
    def setUp(self):
        super().setUp()
        (self.sb.base / "coordinator").mkdir(exist_ok=True)
        for rel in ("coordinator/identity.md", ".env.example"):
            (self.sb.base / rel).write_text((REPO / rel).read_text())

    def test_the_menu_and_usage_say_taskq_until_a_name_is_set(self):
        self.handle({"update_id": 1, "message": {"message_id": 5, "chat": {"id": 4242}, "from": {"id": 777}, "text": "/menu"}})
        self.assertEqual(self.sb.telegram.texts()[-1], "taskq menu")

    def test_with_a_name_set_the_menu_uses_it(self):
        self.sb.configure(ASSISTANT_NAME="Juno")
        self.handle({"update_id": 1, "message": {"message_id": 5, "chat": {"id": 4242}, "from": {"id": 777}, "text": "/menu"}})
        self.assertEqual(self.sb.telegram.texts()[-1], "Juno menu")

    def test_a_name_that_is_not_plain_is_ignored(self):
        self.sb.configure(ASSISTANT_NAME="<script>")
        self.handle({"update_id": 1, "message": {"message_id": 5, "chat": {"id": 4242}, "from": {"id": 777}, "text": "/menu"}})
        self.assertEqual(self.sb.telegram.texts()[-1], "taskq menu")

    def test_the_button_asks_for_three_names_and_shows_each_with_its_reason(self):
        self.sb.set_scenario(other=[{"result": "Juno | calm and short\nMira | easy to say aloud\nPip | small and quick"}])
        self.tapped("act:name")
        text = self.last_edit()["text"]
        for name in ("Juno", "Mira", "Pip", "calm and short"):
            self.assertIn(name, text)
        labels = [label for label, _ in self.buttons(self.edits()[-1])]
        self.assertEqual(labels, ["Juno", "Mira", "Pip", "More names", "Choose for me"])
        self.assertNotIn("--resume", self.sb.claude_calls()[0]["argv"], "a throwaway session, not the conversation")

    def test_more_names_asks_again_without_repeating_the_old_ones(self):
        self.sb.set_scenario(other=[{"result": "Juno | a\nMira | b\nPip | c"}, {"result": "Zed | d\nOla | e\nRex | f"}])
        self.tapped("act:name")
        self.tapped("act:name_more")
        self.assertIn("Juno, Mira, Pip", self.sb.claude_calls()[1]["prompt"])
        self.assertIn("Zed", self.last_edit()["text"])

    def test_picking_one_writes_a_patch_touching_only_identity_and_the_env_example_and_sets_the_name(self):
        self.sb.set_scenario(other=[{"result": "Juno | calm and short\nMira | easy to say aloud\nPip | small and quick"}])
        before = (self.sb.base / ".env").read_text()
        self.tapped("act:name")
        self.tapped("act:name_pick:Mira")
        patch = (self.sb.base / "tasks" / "proposals" / "assistant-name.patch").read_text()
        import re
        self.assertEqual(re.findall(r"^\+\+\+ b/(\S+)$", patch, re.M), ["coordinator/identity.md", ".env.example"])
        added = sorted(line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        self.assertEqual(added, ["ASSISTANT_NAME=Mira", "My name is **Mira**."])
        env = (self.sb.base / ".env").read_text()
        self.assertEqual(env, before + "ASSISTANT_NAME=Mira\n")
        self.assertEqual(oct((self.sb.base / ".env").stat().st_mode & 0o777), "0o600")
        self.assertIn("I am Mira", self.last_edit()["text"])

    def test_a_name_that_was_not_offered_is_refused(self):
        self.tapped("act:name_pick:Evil")
        self.assertFalse((self.sb.base / "tasks" / "proposals" / "assistant-name.patch").exists())
        self.assertNotIn("ASSISTANT_NAME", (self.sb.base / ".env").read_text())

    def test_choose_for_me_lets_the_coordinator_pick_and_records_it(self):
        self.sb.set_scenario(other=[{"result": "Ola | friendly"}])
        self.tapped("act:name_self")
        self.assertIn("ASSISTANT_NAME=Ola", (self.sb.base / ".env").read_text())

    def test_the_coordinator_is_told_who_it_is(self):
        template = (REPO / "coordinator" / "CLAUDE.md.template").read_text()
        self.assertIn("@identity.md", template)


def render(path, **replacements):
    text = path.read_text().replace("__QUEUE_DIR__", "/tmp/queue")
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


class ExampleTaskTests(OpenerCase):
    """The two tasks the menu enqueues are valid, quiet, and the patch a scripted worker writes for them is one the opener accepts."""

    def check(self, text, tmp_name):
        folder = self.root / ("recurring" if tmp_name == "weekly-improver.md" else "pending")  # check_file reads the folder name
        folder.mkdir(exist_ok=True)
        path = folder / tmp_name
        path.write_text(text)
        return subprocess.run([sys.executable, str(REPO / "dispatcher.py"), "check", str(path)],
                              capture_output=True, text=True, cwd=REPO)

    def test_both_example_tasks_pass_the_dispatcher_check(self):
        for name, replacements in (("weekly-improver.md", {}), ("skill-from-task.md", {"__TASK_ID__": "report-1"})):
            text = render(REPO / "tasks" / "examples" / name, **replacements)
            proc = self.check(text, name)
            self.assertEqual(proc.returncode, 0, f"{name}: {proc.stdout}{proc.stderr}")

    def test_both_are_quiet_so_a_run_with_nothing_to_say_says_nothing(self):
        for name in ("weekly-improver.md", "skill-from-task.md"):
            self.assertIn("quiet: true", (REPO / "tasks" / "examples" / name).read_text())

    def test_a_skill_patch_of_the_shape_the_task_asks_for_is_accepted_by_the_opener(self):
        patch = self.make_patch({".claude/skills/make-report/SKILL.md":
                                 "---\nname: make-report\ndescription: Use when a report must be written and delivered.\n---\n1. Do it.\n"})
        proc = self.open_pr(patch, name="skill-report-1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("opened skill-report-1.patch", proc.stdout)

    def test_the_task_tells_the_worker_to_write_the_patch_where_the_opener_expects_it(self):
        text = render(REPO / "tasks" / "examples" / "skill-from-task.md", **{"__TASK_ID__": "report-1"})
        self.assertIn("tasks/proposals/skill-report-1.patch", text)
        self.assertIn("python3 scripts/open_proposal_pr.py tasks/proposals/skill-report-1.patch", text)
        self.assertIn("tasks/done/report-1.md", text)


class QuietRunTests(MenuCase):
    def test_a_confirmed_learning_review_that_finds_nothing_sends_no_message(self):
        self.tapped("act:learn_ok")
        self.sb.telegram.requests.clear()
        self.sb.set_scenario(worker=[{"result": "no proposal"}], review=[PASS])
        (self.sb.base / "worker_preamble.md").exists()
        self.sb.run()
        self.assertEqual([n for n in self.sb.names("done") if n.startswith("weekly-improver")] != [], True)
        self.assertEqual(self.sb.telegram.texts(), [])

    def test_a_skill_task_that_fails_does_report(self):
        self.done_task("report-1")
        self.tapped("act:skill_ok:report-1")
        self.sb.set_scenario(worker=[BOOM], review=[PASS])
        self.sb.run()
        self.sb.run()
        self.sb.run()
        self.assertTrue(any("Task failed" in t for t in self.sb.texts()))


if __name__ == "__main__":
    unittest.main()
