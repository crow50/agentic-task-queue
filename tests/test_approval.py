"""Phase 6: a task at or above APPROVAL_MIN_TIER waits for your 👍.

The dispatcher parks it in tasks/proposed/ and announces it on Telegram. A 👍
on that message (handled by the bridge in code, not by the LLM), or /approve,
moves it to pending/. A task that is not approved never reaches a claude call,
however it got into the queue.
"""

import json
import textwrap
import unittest

from test_baseline import DEFAULT_BODY, PASS, fail
from test_limits_coordinator import CHAT, USER, BotCase, message, reaction

READ_ONLY = "Read,Glob,Grep"


def reaction_to(message_id, emoji="👍", user=CHAT, chat=CHAT):
    update = reaction(emoji, user=user, chat=chat)
    update["message_reaction"]["message_id"] = message_id
    return update


class ApprovalCase(BotCase):
    def setUp(self):
        super().setUp()
        self.sb.configure(APPROVAL_MIN_TIER="1")

    def announcements(self):
        return [t for t in self.sb.texts() if "Approval needed" in t]

    def ledger(self, stem):
        path = self.sb.base / "state" / "approvals" / f"{stem}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def announced_id(self, stem):
        return self.ledger(stem)["message_id"]

    def approve_by_reaction(self, stem, emoji="👍", **kwargs):
        self.handle(reaction_to(self.announced_id(stem), emoji, **kwargs))

    def edit_task(self, state, stem, old, new):
        path = self.sb.base / "tasks" / state / f"{stem}.md"
        path.write_text(path.read_text().replace(old, new))


class GateTests(ApprovalCase):
    def test_the_gate_is_off_unless_it_is_switched_on(self):
        self.sb.configure(APPROVAL_MIN_TIER=None)
        self.sb.add_task("t")
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["t"])

    def test_a_tier_1_task_is_parked_and_announced_with_no_claude_call(self):
        self.sb.add_task("write-it", verify="python3 -m unittest")
        self.sb.run()

        self.assertEqual(self.sb.names("proposed"), ["write-it"])
        self.assertEqual(self.sb.names("pending") + self.sb.names("active") + self.sb.names("done"), [])
        self.assertEqual(self.sb.claude_calls(), [])
        (text,) = self.announcements()
        for wanted in ("write-it", "tier 1", "python3 -m unittest", "Acceptance"):
            self.assertIn(wanted, text)
        self.assertEqual(self.ledger("write-it")["status"], "awaiting")
        meta, _ = self.sb.read_task("proposed", "write-it")
        self.assertEqual(meta["tier"], "1")  # the dispatcher stamps the tier it computed

    def test_the_announcement_is_sent_once(self):
        self.sb.add_task("write-it")
        self.sb.run()
        self.sb.run()
        self.assertEqual(len(self.announcements()), 1)

    def test_a_read_only_task_needs_no_approval(self):
        self.sb.add_task("look", allowed_tools=READ_ONLY)
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["look"])
        self.assertEqual(self.announcements(), [])

    def test_the_threshold_is_configurable(self):
        self.sb.configure(APPROVAL_MIN_TIER="2")
        self.sb.add_task("writes", allowed_tools="Read,Write")
        self.sb.add_task("pushes", allowed_tools="Read,Write,Bash(git push*)")
        self.sb.run()
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["writes"])
        self.assertEqual(self.sb.names("proposed"), ["pushes"])

    def test_a_label_in_the_file_cannot_lower_the_tier(self):
        self.sb.add_task("liar", tier="0")  # writes (tier 1) but says 0
        self.sb.run()
        self.assertEqual(self.sb.names("failed"), ["liar"])  # rejected as invalid: no run, no approval
        self.assertEqual(self.sb.claude_calls(), [])

    def test_recurring_instances_are_exempt_because_you_installed_the_template(self):
        self.sb.add_task("nightly", state="recurring", schedule="every 1d")
        self.sb.run()
        self.assertEqual(len(self.sb.names("done")), 1)
        self.assertEqual(self.announcements(), [])

    def test_an_invalid_task_is_rejected_not_announced(self):
        self.sb.add_task("bad", model="")
        self.sb.run()
        self.assertEqual(self.sb.names("failed"), ["bad"])
        self.assertEqual(self.announcements(), [])

    def test_a_task_dropped_straight_into_pending_without_approval_never_runs(self):
        self.sb.add_task("forged", state="pending")  # the coordinator, or anything else, wrote it there
        self.sb.run()
        self.sb.run()
        self.assertEqual(self.sb.names("done"), [])
        self.assertEqual(self.sb.names("proposed"), ["forged"])
        self.assertEqual(self.sb.claude_calls(), [])

    def test_a_task_that_reaches_active_unapproved_is_put_back(self):
        self.sb.add_task("sneaky", state="active")  # e.g. a stale active file; recover_stale requeues it
        self.sb.run()
        self.sb.run()
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertEqual(self.sb.names("proposed"), ["sneaky"])


class ReactionTests(ApprovalCase):
    def parked(self, name="write-it", **meta):
        self.sb.add_task(name, **meta)
        self.sb.run()

    def test_a_thumbs_up_on_the_announcement_queues_the_task_and_it_runs(self):
        self.parked()
        self.approve_by_reaction("write-it")

        self.assertEqual(self.sb.names("pending"), ["write-it"])
        self.assertEqual(self.sb.names("proposed"), [])
        self.assertEqual(self.ledger("write-it")["status"], "approved")
        self.assertTrue(any("write-it" in t and "pproved" in t for t in self.sb.texts()))
        self.assertEqual(self.others(), [])  # deterministic: the coordinator LLM was not asked

        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["write-it"])

    def test_a_thumbs_down_cancels_it(self):
        self.parked()
        self.approve_by_reaction("write-it", "👎")

        self.assertEqual(self.sb.names("cancelled"), ["write-it"])
        self.assertEqual(self.sb.names("proposed") + self.sb.names("pending"), [])
        self.assertEqual(self.ledger("write-it")["status"], "rejected")
        self.sb.run()
        self.assertEqual(self.sb.claude_calls(), [])

    def test_a_reaction_from_someone_else_does_nothing(self):
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        self.parked()
        self.approve_by_reaction("write-it", user=999)
        self.approve_by_reaction("write-it", chat=31337)

        self.assertEqual(self.sb.names("proposed"), ["write-it"])
        self.assertEqual(self.ledger("write-it")["status"], "awaiting")

    def test_other_emoji_and_other_messages_still_go_to_the_coordinator(self):
        self.parked()
        self.sb.set_scenario(other=[{"result": "ok"}])
        self.handle(reaction_to(99999))
        self.assertEqual(len(self.others()), 1)
        self.assertEqual(self.sb.names("proposed"), ["write-it"])

    def test_a_second_thumbs_up_is_harmless(self):
        self.parked()
        self.approve_by_reaction("write-it")
        self.approve_by_reaction("write-it")
        self.assertEqual(self.sb.names("pending"), ["write-it"])

    def test_a_spec_edited_after_the_announcement_is_not_approved_by_the_old_thumbs_up(self):
        self.parked(verify="python3 -m unittest")
        self.edit_task("proposed", "write-it", "python3 -m unittest", "python3 -m unittest -v")
        self.approve_by_reaction("write-it")

        self.assertEqual(self.sb.names("proposed"), ["write-it"])
        self.assertEqual(self.sb.names("pending"), [])
        self.sb.run()  # announces the changed spec afresh
        self.assertEqual(len(self.announcements()), 2)
        self.assertIn("-v", self.announcements()[1])
        self.approve_by_reaction("write-it")
        self.assertEqual(self.sb.names("pending"), ["write-it"])

    def test_a_spec_edited_after_approval_is_gated_again(self):
        self.parked()
        self.approve_by_reaction("write-it")
        path = self.sb.base / "tasks" / "pending" / "write-it.md"
        path.write_text(path.read_text().replace("attempts: 0", "attempts: 0\nallowed_tools: Read,Write,Bash(python3 *)"))
        self.sb.run()

        self.assertEqual(self.sb.claude_calls(), [])
        self.assertEqual(self.sb.names("proposed"), ["write-it"])

    def test_a_retry_after_a_failed_review_does_not_ask_again(self):
        self.sb.set_scenario(worker=[{"result": "first"}, {"result": "second"}], review=[fail("nope"), PASS])
        self.parked()
        self.approve_by_reaction("write-it")
        self.sb.run()
        self.assertEqual(self.sb.names("pending"), ["write-it"])  # failed review, requeued
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["write-it"])
        self.assertEqual(len(self.announcements()), 1)

    def test_a_task_that_was_retried_by_hand_keeps_its_approval(self):
        self.sb.set_scenario(worker=[{"result": "x"}], review=[fail("nope")])
        self.parked(max_attempts="1")
        self.approve_by_reaction("write-it")
        self.sb.run()
        self.assertEqual(self.sb.names("failed"), ["write-it"])
        self.handle(message("/retry write-it"))
        self.sb.run()
        self.assertEqual(len(self.announcements()), 1)
        self.assertEqual(len(self.sb.claude_calls("worker")), 2)


class CommandTests(ApprovalCase):
    def test_approve_and_reject_commands_do_what_the_reactions_do(self):
        self.sb.add_task("a")
        self.sb.add_task("b")
        self.sb.run()
        self.sb.run()
        self.handle(message("/approve a"))
        self.handle(message("/reject b"))

        self.assertEqual(self.sb.names("pending"), ["a"])
        self.assertEqual(self.sb.names("cancelled"), ["b"])

    def test_approve_without_an_id_lists_what_is_waiting(self):
        self.sb.add_task("a")
        self.sb.run()
        self.handle(message("/approve"))
        self.assertIn("a", self.sb.texts()[-1])
        self.assertIn("Usage", self.sb.texts()[-1])

    def test_approve_of_an_unknown_task_says_so(self):
        self.handle(message("/approve ghost"))
        self.assertIn("ghost", self.sb.texts()[-1])

    def test_the_commands_ignore_other_senders(self):
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        self.sb.add_task("a")
        self.sb.run()
        self.handle(message("/approve a", sender=999))
        self.assertEqual(self.sb.names("proposed"), ["a"])

    def test_status_counts_what_is_waiting_for_approval(self):
        self.sb.add_task("a")
        self.sb.run()
        self.handle(message("/status"))
        self.assertIn("proposed: 1 — a", self.sb.texts()[-1])

    def test_cancel_reaches_a_proposed_task(self):
        self.sb.add_task("a")
        self.sb.run()
        self.handle(message("/cancel a"))
        self.assertEqual(self.sb.names("cancelled"), ["a"])

    def test_the_bridge_announces_a_proposal_the_coordinator_wrote(self):
        path = self.sb.base / "tasks" / "proposed"
        path.mkdir(parents=True, exist_ok=True)
        self.sb.add_task("idea", state="proposed", body=DEFAULT_BODY)
        self.drive("bot.dispatcher.announce_proposals()")
        self.assertEqual(len(self.announcements()), 1)
        self.drive("bot.dispatcher.announce_proposals()")
        self.assertEqual(len(self.announcements()), 1)

    def test_the_new_commands_are_registered_in_the_menu(self):
        source = (self.sb.base / "coordinator_bot.py").read_text()
        self.assertIn('"command": "approve"', textwrap.dedent(source))
        self.assertIn('"command": "reject"', source)


if __name__ == "__main__":
    unittest.main()
