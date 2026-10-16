"""Phase 6: autonomy tiers, computed from a task's allowed_tools.

Tier 0 reads only, tier 1 writes inside the task's own workspace, tier 2 leaves
the box. The dispatcher computes the tier, so a task cannot be mislabelled, and
a few grants (gh api and friends) are refused outright.
"""

import json
import unittest

from test_baseline import Sandbox


class AutonomyCase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def snippet(self, code, *args):
        proc = self.sb.python("-c", code, *args)
        if proc.returncode != 0:
            raise AssertionError(f"snippet failed\n{proc.stderr}")
        return proc.stdout

    TIERS = """
import json, sys
import dispatcher
print(json.dumps([dispatcher.tier_of(m) for m in json.loads(sys.argv[1])]))
"""
    PROBLEMS = """
import json, sys
import dispatcher
print(json.dumps([dispatcher.autonomy_problems(m) for m in json.loads(sys.argv[1])]))
"""

    def tiers(self, *tool_lists):
        metas = [{"allowed_tools": tools} for tools in tool_lists]
        return json.loads(self.snippet(self.TIERS, json.dumps(metas)))

    def problems(self, *metas):
        return json.loads(self.snippet(self.PROBLEMS, json.dumps(metas)))


class TierClassificationTests(AutonomyCase):
    def test_read_only_tools_are_tier_0(self):
        self.assertEqual(
            self.tiers("Read,Glob,Grep", "Read,Glob,Grep,WebSearch,WebFetch", "Read,Bash(df *),Bash(uptime)"),
            [0, 0, 0],
        )

    def test_writing_and_test_commands_inside_the_workspace_are_tier_1(self):
        self.assertEqual(
            self.tiers(
                "Read,Glob,Grep,Edit,Write",
                "Read,Write(tasks/proposals/**)",
                "Read,Edit,Bash(python3 -m unittest*)",
                "Read,Edit,Bash(pytest*)",
                "Read,Edit,Bash(python3 *),Bash(git *)",  # lenient: wildcards over python3 and git stay tier 1
            ),
            [1, 1, 1, 1, 1],
        )

    def test_anything_that_leaves_the_box_is_tier_2(self):
        self.assertEqual(
            self.tiers(
                "Read,Bash(git push*)",
                "Read,Bash(git push origin task/x)",
                "Read,Bash(gh pr create*)",
                "Read,Bash(gh issue create*)",
                "Read,Bash(ssh pi@lab uptime)",
                "Read,Bash(scp *)",
                "Read,Bash(rsync *)",
                "Read,Bash(curl *)",
                "Read,Bash(./deploy.sh*)",
            ),
            [2] * 9,
        )

    def test_the_tier_is_the_highest_of_the_tools(self):
        self.assertEqual(self.tiers("Read,Write,Bash(git push*)"), [2])

    def test_unknown_tools_fail_closed_to_tier_2(self):
        self.assertEqual(self.tiers("Read,SomeNewTool", "Read,Bash(frobnicate *)"), [2, 2])

    def test_mcp_tools_are_tier_0_only_when_they_look_read_only(self):
        self.assertEqual(
            self.tiers("mcp__fetch__fetch", "mcp__notes__search", "mcp__calendar__list_events",
                       "mcp__github__create_issue", "mcp__x__delete_all"),
            [0, 0, 0, 2, 2],
        )

    def test_a_task_without_allowed_tools_gets_the_default_grant(self):
        (tier,) = json.loads(self.snippet(self.TIERS, json.dumps([{}])))
        self.assertEqual(tier, 1)  # DEFAULT_ALLOWED_TOOLS: Read,Glob,Grep,Edit,Write

    def test_a_task_with_a_repo_is_tier_2_whatever_it_grants(self):
        (tier,) = json.loads(self.snippet(self.TIERS, json.dumps([{"allowed_tools": "Read", "repo": "sideburn"}])))
        self.assertEqual(tier, 2)  # the dispatcher itself will push its branch

    def test_extra_tier_2_commands_come_from_the_settings(self):
        self.assertEqual(self.tiers("Read,Bash(make publish*)"), [2])  # unknown Bash already fails closed
        self.sb.configure(TIER2_BASH="make deploy")
        self.assertEqual(self.tiers("Read,Bash(make deploy*)"), [2])


class AutonomyProblemTests(AutonomyCase):
    HARD = (
        "Bash(gh api *)", "Bash(gh api repos/x/y)", "Bash(gh *)", "Bash(gh pr merge*)", "Bash(gh pr ready*)",
        "Bash(gh repo delete*)", "Bash(gh auth *)", "Bash(gh workflow run*)", "Bash(git push --force*)",
        "Bash(git push -f*)", "Bash(*)", "Bash",
    )

    def test_a_grant_of_gh_api_or_its_friends_is_rejected(self):
        for bad in self.HARD:
            (problems,) = self.problems({"allowed_tools": f"Read,{bad}"})
            self.assertTrue(any(bad in p for p in problems), (bad, problems))

    def test_the_rejection_survives_a_widened_allowlist(self):
        self.sb.configure(TASK_ALLOWED_TOOLS="Read,Bash(gh *)")
        self.sb.add_task("t", allowed_tools="Read,Bash(gh api x)")
        path = self.sb.base / "tasks" / "pending" / "t.md"
        proc = self.sb.python(str(self.sb.base / "dispatcher.py"), "check", str(path))

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("gh api", proc.stdout)
        self.sb.run()
        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertEqual(self.sb.claude_calls(), [])

    def test_ordinary_grants_are_not_rejected(self):
        ok = [
            "Read,Glob,Grep,Edit,Write", "Read,Bash(python3 *),Bash(git *)", "Read,Bash(gh issue create*)",
            "Read,Bash(gh pr create*)", "Read,Bash(df *),Bash(du -sh*)",
        ]
        self.assertEqual(self.problems(*[{"allowed_tools": tools} for tools in ok]), [[]] * len(ok))

    def test_a_declared_tier_must_match_the_computed_one(self):
        wrong, right = self.problems(
            {"allowed_tools": "Read,Write,Bash(git push*)", "tier": "0"},
            {"allowed_tools": "Read,Write,Bash(git push*)", "tier": "2"},
        )
        self.assertTrue(any("tier" in p for p in wrong))
        self.assertEqual(right, [])

    def test_a_repo_task_may_not_grant_its_own_push_or_pull_request(self):
        for tools in ("Read,Write,Bash(git push*)", "Read,Write,Bash(gh pr create*)", "Read,Write,Bash(gh issue *)"):
            (problems,) = self.problems({"allowed_tools": tools, "repo": "sideburn"})
            self.assertTrue(any("repo" in p for p in problems), (tools, problems))
        (problems,) = self.problems({"allowed_tools": "Read,Write,Bash(python3 *),Bash(git *)", "repo": "sideburn"})
        self.assertEqual(problems, [])

    def test_repo_and_cwd_are_mutually_exclusive(self):
        (problems,) = self.problems({"repo": "sideburn", "cwd": "/tmp"})
        self.assertTrue(any("cwd" in p and "repo" in p for p in problems))

    def test_the_new_frontmatter_keys_are_known(self):
        keys = json.loads(self.snippet("import json, dispatcher; print(json.dumps(sorted(dispatcher.FRONTMATTER_KEYS)))"))
        for key in ("tier", "repo", "base", "branch", "pr_url", "pending_publish", "publish_failures",
                    "proposed_by", "depth", "propose"):
            self.assertIn(key, keys)


if __name__ == "__main__":
    unittest.main()
