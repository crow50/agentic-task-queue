"""Phase 7 items 7 and 8: coordinator rules and memory, and the installer's checks.

The template rules are pinned by checking the text is there. preferences.md is seeded into
coordinator/memory/ once and never overwritten. `install.py --check` probes publish access for
every repo a task targets, and its approval-gate line says what to set when it fails.
The coordinator's file sandbox is its working directory, so tasks/ and logs/ are added to it.
"""

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from test_baseline import Sandbox
from test_install import REPO, TESTS, InstallCase  # puts scripts/ on sys.path

FAKE_GH = TESTS / "fake_gh.py"
TEMPLATE = REPO / "coordinator" / "CLAUDE.md.template"
SEED = REPO / "coordinator" / "memory.seed" / "preferences.md"


def flat(path):
    return " ".join(path.read_text().split())


class TemplateRuleTests(unittest.TestCase):
    def setUp(self):
        self.text = flat(TEMPLATE)

    def test_research_tasks_get_countable_criteria(self):
        self.assertRegex(self.text, r"at least N candidates, or each exclusion explained")
        self.assertRegex(self.text, r"discover first, then do due diligence")

    def test_a_change_to_a_host_states_its_rollback_and_the_check_that_proves_recovery(self):
        self.assertRegex(self.text, r"rollback and the check that proves recovery")
        self.assertRegex(self.text, r"snapshot or backup (comes )?first")

    def test_a_task_is_queued_only_after_dispatcher_check_passes(self):
        self.assertRegex(self.text, r"dispatcher\.py check <file>`? and fix whatever it reports before telling the human the task is queued")

    def test_the_preferences_file_is_loaded_with_the_memory(self):
        self.assertIn("@memory/preferences.md", self.text)


class SeedContentTests(unittest.TestCase):
    def test_the_standing_rules_from_the_openclaw_chat_are_all_there(self):
        text = flat(SEED).lower()
        for needle in ("branch", "pull request", "squash", "never merge", "python3", "never `python`", "makefile",
                       "attachment", "ascii", "em-dash", "follow-up question", "long job"):
            self.assertIn(needle, text, needle)

    def test_the_seed_is_ascii_and_has_no_em_dash(self):
        raw = SEED.read_text()
        self.assertTrue(raw.isascii())


class SeedInstallTests(InstallCase):
    def setUp(self):
        super().setUp()
        seed = self.base / "coordinator" / "memory.seed" / "preferences.md"
        seed.parent.mkdir(parents=True)
        seed.write_text(SEED.read_text())
        self.target = self.base / "coordinator" / "memory" / "preferences.md"

    def test_the_first_install_copies_it(self):
        self.install_ok()
        self.assertEqual(self.target.read_text(), SEED.read_text())

    def test_an_edited_copy_is_never_overwritten(self):
        self.install_ok()
        self.target.write_text("my own rules\n")
        self.install_ok()
        self.assertEqual(self.target.read_text(), "my own rules\n")

    def test_a_missing_copy_is_restored(self):
        self.install_ok()
        self.target.unlink()
        self.install_ok()
        self.assertTrue(self.target.exists())

    def test_a_base_without_the_seed_still_installs(self):
        (self.base / "coordinator" / "memory.seed" / "preferences.md").unlink()
        self.install_ok()
        self.assertFalse(self.target.exists())

    def test_the_second_install_changes_nothing(self):
        self.install_ok()
        out = self.install_ok()
        self.assertIn("Nothing to change", out)


class PublishProbeCheckTests(InstallCase):
    """`install.py --check` for every repo a task targets: gh repo view must show WRITE or better, and origin must answer."""

    def check(self, *extra):
        return self.run_main("--check", *extra, defaults=False)

    def setUp(self):
        super().setUp()
        self.install_ok()
        self.telegram.requests.clear()
        self.gh_script = self.root / "gh-script.json"
        self.gh_log = self.root / "gh.log"
        patcher = mock.patch.dict(os.environ, {"FAKE_GH_SCRIPT": str(self.gh_script), "FAKE_GH_LOG": str(self.gh_log)})
        patcher.start()
        self.addCleanup(patcher.stop)
        env = self.base / ".env"
        env.write_text(env.read_text() + f"GH_BIN={sys.executable} {FAKE_GH}\n")
        self.origin = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        repo = self.base / "workspace" / "proj"
        repo.mkdir(parents=True)
        for args in (["init", "-q", "-b", "main"], ["remote", "add", "origin", str(self.origin)]):
            subprocess.run(["git", "-C", str(repo), *args], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True)
        (self.base / "tasks" / "pending").mkdir(parents=True, exist_ok=True)
        (self.base / "tasks" / "pending" / "t.md").write_text("---\nmodel: m\nrepo: proj\n---\nbody\n")

    def permission(self, level):
        self.gh_script.write_text(json.dumps([{"match": "repo view", "stdout": json.dumps({"viewerPermission": level})}]))

    def test_a_token_with_write_passes(self):
        self.permission("WRITE")
        code, out, _ = self.check()
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"PASS +publish access: proj")

    def test_a_token_without_write_fails_and_says_what_to_set(self):
        self.permission("READ")
        code, out, _ = self.check("--notify")

        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"FAIL +publish access: proj")
        self.assertIn("Contents", out)
        self.assertIn("read and write", out)
        self.assertIn("GH_TOKEN", out)
        self.assertTrue(any("publish access" in m.params["text"] for m in self.telegram.calls("sendMessage")))

    def test_a_remote_that_does_not_answer_fails(self):
        self.permission("WRITE")
        subprocess.run(["git", "-C", str(self.base / "workspace" / "proj"), "remote", "set-url", "origin",
                        str(self.root / "gone.git")], check=True)
        code, out, _ = self.check()
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"FAIL +publish access: proj")

    def test_a_role_of_write_is_not_proof_the_token_can_push(self):
        """viewerPermission is the person's role on the repo; a read-only fine-grained token still reports it."""
        self.permission("ADMIN")
        subprocess.run(["git", "-C", str(self.base / "workspace" / "proj"), "remote", "set-url", "--push", "origin",
                        str(self.root / "refuses-pushes.git")], check=True)
        code, out, _ = self.check()
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"FAIL +publish access: proj")
        self.assertIn("GH_TOKEN", out)

    def test_without_any_repo_task_there_is_nothing_to_probe(self):
        (self.base / "tasks" / "pending" / "t.md").unlink()
        subprocess.run(["rm", "-rf", str(self.base / "workspace" / "proj")], check=True)
        _, out, _ = self.check()
        self.assertNotIn("publish access", out)

    def test_the_approval_gate_line_differs_between_pass_and_fail(self):
        self.permission("WRITE")
        _, passing, _ = self.check()
        env = self.base / ".env"
        env.write_text(env.read_text().replace("APPROVAL_MIN_TIER=1", "APPROVAL_MIN_TIER=0"))
        _, failing, _ = self.check()

        ok = next(line for line in passing.splitlines() if "approval gate" in line)
        bad = next(line for line in failing.splitlines() if "approval gate" in line)
        self.assertNotEqual(ok.split(None, 2)[2], bad.split(None, 2)[2])
        self.assertIn("set APPROVAL_MIN_TIER=1", bad)
        self.assertNotIn("without your", ok)


class CoordinatorSandboxTests(unittest.TestCase):
    """Claude only touches files under its working directory (coordinator/) and --add-dir folders."""

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def test_the_coordinator_call_adds_tasks_and_logs_to_its_sandbox(self):
        self.sb.set_scenario(other=[{"result": "hi"}])
        code = (
            "import coordinator_bot as bot\n"
            "bot.COORD_DIR.mkdir(parents=True, exist_ok=True)\n"
            "bot.run_coordinator('hello', None)\n"
        )
        proc = self.sb.python("-c", code)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        (call,) = self.sb.claude_calls("other")
        argv = call["argv"]
        added = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
        self.assertIn(str(self.sb.base / "tasks"), added)
        self.assertIn(str(self.sb.base / "logs"), added)
        self.assertNotIn(str(self.sb.base), added, "the whole queue directory (.env, state/) must not be opened up")


if __name__ == "__main__":
    unittest.main()
