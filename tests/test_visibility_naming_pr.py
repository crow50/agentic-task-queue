"""Phase 8 Part B item 6: the pull request that records the assistant's name goes through the same opener as the weekly
improver, which refuses anything but the two files unless it is told, by the bridge, that this is the naming flow."""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from taskq_lib import naming  # noqa: E402
from test_learning import OpenerCase  # noqa: E402

IDENTITY = (REPO / "coordinator" / "identity.md").read_text()
ENV_EXAMPLE = (REPO / ".env.example").read_text()


class NamingPrTests(OpenerCase):
    def setUp(self):
        super().setUp()
        self.put("coordinator/identity.md", IDENTITY)
        self.put(".env.example", ENV_EXAMPLE)
        self.commit("identity and env example")
        self.run_git("push", "-q", "origin", "main")
        self.patch = naming.name_patch(IDENTITY, ENV_EXAMPLE, "Juno")

    def open(self, *flags, env=None, name="assistant-name"):
        import shlex
        import subprocess

        from test_learning import FAKE_GH
        target = self.repo / "tasks" / "proposals" / f"{name}.patch"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.patch)
        gh = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_GH))}"
        return subprocess.run(
            [sys.executable, "scripts/open_proposal_pr.py", f"tasks/proposals/{name}.patch", "--test-cmd", "true", "--gh", gh, *flags],
            cwd=self.repo, env=self.env(**(env or {})), capture_output=True, text=True, timeout=120)

    ALLOW = ("--allow", "coordinator/identity.md", "--allow", ".env.example")

    def test_the_naming_flow_opens_a_pull_request_for_exactly_the_two_files(self):
        proc = self.open(*self.ALLOW, env={"TASKQ_NAME_PR": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("opened assistant-name.patch", proc.stdout)
        self.assertEqual(len(self.remote_branches()), 1)
        changed = self.run_git("diff", "--name-only", "origin/main", "origin/" + self.remote_branches()[0]).split()
        self.assertEqual(sorted(changed), [".env.example", "coordinator/identity.md"])

    def test_without_the_flag_the_improver_cannot_touch_either_file(self):
        proc = self.open(env={"TASKQ_NAME_PR": "1"})
        self.assert_rejected(proc, name="assistant-name")

    def test_the_flag_without_the_naming_environment_is_refused_outright(self):
        proc = self.open(*self.ALLOW)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.gh_calls(), [])
        self.assertEqual(self.remote_branches(), [])

    def test_the_flag_cannot_open_other_paths_even_for_the_bridge(self):
        proc = self.open("--allow", "dispatcher.py", env={"TASKQ_NAME_PR": "1"})
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.remote_branches(), [])

    def test_a_patch_that_also_touches_dispatcher_is_still_refused(self):
        self.patch += "--- a/dispatcher.py\n+++ b/dispatcher.py\n@@ -1 +1 @@\n-# dispatcher\n+# changed\n"
        proc = self.open(*self.ALLOW, env={"TASKQ_NAME_PR": "1"})
        self.assert_rejected(proc, name="assistant-name")


if __name__ == "__main__":
    unittest.main()
