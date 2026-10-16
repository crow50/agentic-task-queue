"""Phase 6: a worker may propose follow-up tasks, never queue them.

A task with `propose: true` may write task files into its own staging folder,
tasks/proposed/.incoming/<id>/. When the task passes, the dispatcher validates
each file, stamps proposed_by and depth itself, enforces the caps (depth 2,
5 per task) and moves the survivors to tasks/proposed/, where they wait for a
👍 like any task. A proposal never runs without that, with or without
APPROVAL_MIN_TIER.
"""

import unittest

from test_approval import ApprovalCase, reaction_to
from test_baseline import PASS, fail
from test_limits_coordinator import message

CRITERIA = "## Acceptance Criteria\n- it is done\n"


def proposal(title="Follow up", **meta):
    front = {"model": "fake-worker", "review_model": "fake-review", "max_attempts": "3"}
    front.update(meta)
    return "---\n" + "\n".join(f"{k}: {v}" for k, v in front.items()) + f"\n---\n{title}\n\n{CRITERIA}"


class ProposalCase(ApprovalCase):
    def setUp(self):
        super().setUp()
        self.sb.configure(APPROVAL_MIN_TIER=None)  # proposals must wait for approval even with the gate off

    def incoming(self, task="t"):
        return self.sb.base / "tasks" / "proposed" / ".incoming" / task

    def writes(self, *names, task="t", **meta):
        return [{"path": str(self.incoming(task) / f"{name}.md"), "content": proposal(name, **meta)} for name in names]

    def worker_writes(self, *names, result="Done.", task="t", **meta):
        return {"result": result, "write": self.writes(*names, task=task, **meta)}

    def proposed_meta(self, name):
        return self.sb.read_task("proposed", name)[0]

    def proposed(self):
        return self.sb.names("proposed")


class CollectingTests(ProposalCase):
    def test_a_proposing_task_may_write_only_to_its_own_staging_folder_and_is_told_so(self):
        self.sb.set_scenario(worker=[{"result": "ok"}], review=[PASS])
        self.sb.add_task("t", propose="true")
        self.sb.add_task("plain")
        self.sb.run()
        self.sb.run()

        calls = self.sb.claude_calls("worker")
        (proposing,) = [c for c in calls if ".incoming" in c["allowed_tools"]]
        (plain,) = [c for c in calls if ".incoming" not in c["allowed_tools"]]
        self.assertIn(f"Write(/{self.incoming()}/**)", proposing["allowed_tools"])
        self.assertIn("proposal", proposing["prompt"].lower())
        self.assertNotIn(".incoming", plain["prompt"])

    def test_a_passing_task_hands_in_its_proposals_stamped_and_waiting(self):
        self.sb.set_scenario(worker=[self.worker_writes("followup")], review=[PASS])
        self.sb.add_task("t", propose="true")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(self.proposed(), ["followup"])
        meta = self.proposed_meta("followup")
        self.assertEqual((meta["proposed_by"], meta["depth"], meta["attempts"]), ("t", "1", "0"))
        self.assertFalse(self.incoming().exists())
        self.assertEqual(self.sb.names("pending"), [])
        self.assertTrue(any("followup" in t and "proposed" in t for t in self.sb.texts()))

    def test_a_worker_cannot_forge_who_proposed_it_or_how_deep_it_is(self):
        forged = self.worker_writes("sneaky", proposed_by="somebody-else", depth="0", attempts="2", tier="0")
        self.sb.set_scenario(worker=[forged], review=[PASS])
        self.sb.add_task("t", propose="true")
        self.sb.run()

        meta = self.proposed_meta("sneaky")
        self.assertEqual((meta["proposed_by"], meta["depth"], meta["attempts"]), ("t", "1", "0"))

    def test_a_proposal_never_runs_without_approval_whether_or_not_the_gate_is_on(self):
        for gate in (None, "1"):
            with self.subTest(gate=gate):
                self.sb.configure(APPROVAL_MIN_TIER=gate)
                self.sb.set_scenario(worker=[self.worker_writes("followup")], review=[PASS])
                self.sb.add_task("t", propose="true", allowed_tools="Read,Glob,Grep")  # tier 0: runs even with the gate on
                self.sb.run()
                calls = len(self.sb.claude_calls("worker"))
                self.assertEqual(self.sb.names("proposed")[-1:], ["followup"])
                for _ in range(3):
                    self.sb.run()
                self.assertEqual(len(self.sb.claude_calls("worker")), calls)  # nothing ran
                self.assertNotIn("followup", self.sb.names("pending") + self.sb.names("done"))
                (self.sb.base / "tasks" / "proposed" / "followup.md").unlink()
                (self.sb.base / "tasks" / "done" / "t.md").unlink()

    def test_a_proposal_runs_once_you_approve_it(self):
        self.sb.set_scenario(worker=[self.worker_writes("followup"), {"result": "did the follow-up"}], review=[PASS])
        self.sb.add_task("t", propose="true")
        self.sb.run()
        self.handle(reaction_to(self.announced_id("followup")))
        self.assertEqual(self.sb.names("pending"), ["followup"])
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["followup", "t"])

    def test_a_failed_attempt_hands_in_nothing_until_a_later_one_passes(self):
        first = self.worker_writes("early", result="first try")
        second = {"result": "second try"}
        self.sb.set_scenario(worker=[first, second], review=[fail("no"), PASS])
        self.sb.add_task("t", propose="true")
        self.sb.run()
        self.assertEqual(self.proposed(), [])
        self.sb.run()
        self.assertEqual(self.proposed(), ["early"])  # the first attempt's file counts once the task passes

    def test_a_task_that_fails_for_good_leaves_no_proposals_behind(self):
        self.sb.set_scenario(worker=[self.worker_writes("doomed")], review=[fail("no")])
        self.sb.add_task("t", propose="true", max_attempts="1")
        self.sb.run()

        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertEqual(self.proposed(), [])
        self.assertFalse(self.incoming().exists())

    def test_a_task_that_does_not_ask_to_propose_has_its_staging_folder_ignored(self):
        self.sb.set_scenario(worker=[self.worker_writes("uninvited")], review=[PASS])
        self.sb.add_task("t")
        self.sb.run()
        self.assertEqual(self.proposed(), [])


class CapTests(ProposalCase):
    def test_at_most_five_proposals_per_task_across_its_attempts(self):
        first = self.worker_writes("a", "b", "c", result="first")
        second = self.worker_writes("d", "e", "f", "g", result="second")
        self.sb.set_scenario(worker=[first, second], review=[fail("again"), PASS])
        self.sb.add_task("t", propose="true")
        self.sb.run()
        self.sb.run()

        self.assertEqual(len(self.proposed()), 5)
        self.assertTrue(any("dropped" in t.lower() for t in self.sb.texts()))

    def test_the_five_is_a_setting(self):
        self.sb.configure(MAX_PROPOSALS_PER_TASK="2")
        self.sb.set_scenario(worker=[self.worker_writes("a", "b", "c")], review=[PASS])
        self.sb.add_task("t", propose="true")
        self.sb.run()
        self.assertEqual(len(self.proposed()), 2)

    def test_a_task_already_two_deep_may_not_propose(self):
        self.sb.set_scenario(worker=[self.worker_writes("deeper")], review=[PASS])
        self.sb.add_task("t", propose="true", depth="2", proposed_by="parent")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(self.proposed(), [])
        self.assertTrue(any("depth" in t.lower() for t in self.sb.texts()))

    def test_depth_grows_by_one_per_generation_up_to_the_limit(self):
        self.sb.set_scenario(worker=[self.worker_writes("child", propose="true")], review=[PASS])
        self.sb.add_task("t", propose="true")
        self.sb.run()
        self.assertEqual(self.proposed_meta("child")["depth"], "1")
        self.handle(reaction_to(self.announced_id("child")))

        self.sb.set_scenario(worker=[self.worker_writes("grandchild", task="child", propose="true")], review=[PASS])
        self.sb.run()
        self.assertEqual(self.proposed_meta("grandchild")["depth"], "2")
        self.handle(reaction_to(self.announced_id("grandchild")))

        self.sb.set_scenario(worker=[self.worker_writes("great", task="grandchild")], review=[PASS])
        self.sb.run()
        self.assertEqual(self.proposed(), [])  # grandchild is at depth 2: no more

    def test_the_depth_limit_is_a_setting(self):
        self.sb.configure(MAX_PROPOSAL_DEPTH="1")
        self.sb.set_scenario(worker=[self.worker_writes("child")], review=[PASS])
        self.sb.add_task("t", propose="true", depth="1", proposed_by="p")
        self.sb.run()
        self.assertEqual(self.proposed(), [])


class ValidationTests(ProposalCase):
    def test_an_invalid_proposal_is_dropped_with_a_note_and_the_valid_ones_survive(self):
        files = self.writes("good") + [{"path": str(self.incoming() / "bad.md"), "content": "no frontmatter at all"}]
        self.sb.set_scenario(worker=[{"result": "ok", "write": files}], review=[PASS])
        self.sb.add_task("t", propose="true")
        self.sb.run()

        self.assertEqual(self.proposed(), ["good"])
        self.assertTrue(any("bad" in t and "dropped" in t.lower() for t in self.sb.texts()))

    def test_a_proposal_cannot_grant_what_no_task_may_have(self):
        self.sb.set_scenario(worker=[self.worker_writes("evil", allowed_tools="Read,Bash(gh api *)")], review=[PASS])
        self.sb.add_task("t", propose="true")
        self.sb.run()
        self.assertEqual(self.proposed(), [])

    def test_a_name_that_is_taken_gets_a_suffix_instead_of_overwriting(self):
        self.sb.add_task("followup", state="done")
        self.sb.set_scenario(worker=[self.worker_writes("followup")], review=[PASS])
        self.sb.add_task("t", propose="true")
        self.sb.run()

        self.assertEqual(self.proposed(), ["followup-2"])
        self.assertEqual(self.sb.names("done"), ["followup", "t"])


class ApprovalOfProposalsTests(ProposalCase):
    def propose_one(self):
        self.sb.set_scenario(worker=[self.worker_writes("child")], review=[PASS])
        self.sb.add_task("t", propose="true")
        self.sb.run()

    def test_a_proposal_whose_parent_is_gone_cannot_be_approved(self):
        self.propose_one()
        (self.sb.base / "tasks" / "done" / "t.md").unlink()
        self.handle(message("/approve child"))

        self.assertEqual(self.proposed(), ["child"])
        self.assertIn("parent", self.sb.texts()[-1].lower())

    def test_a_proposal_whose_depth_does_not_match_its_parent_cannot_be_approved(self):
        self.propose_one()
        self.edit_task("proposed", "child", "depth: 1", "depth: 0")
        self.handle(message("/approve child"))
        self.assertEqual(self.proposed(), ["child"])

    def test_a_proposal_deeper_than_the_limit_cannot_be_approved(self):
        self.propose_one()
        self.edit_task("proposed", "child", "depth: 1", "depth: 3")
        self.edit_task("done", "t", "propose: true", "propose: true\ndepth: 2")
        self.handle(message("/approve child"))
        self.assertEqual(self.proposed(), ["child"])

    def test_the_daily_attempt_cap_still_applies_to_approved_proposals(self):
        self.sb.configure(MAX_ATTEMPTS_PER_DAY="1")
        self.sb.add_task("one", state="pending", propose="false")
        self.sb.add_task("two", state="pending", propose="false")
        self.sb.run()
        self.sb.run()

        self.assertEqual(len(self.sb.claude_calls("worker")), 1)
        self.assertEqual(self.sb.names("pending"), ["two"])
        self.assertTrue((self.sb.base / "state" / "paused_until").exists())


if __name__ == "__main__":
    unittest.main()
