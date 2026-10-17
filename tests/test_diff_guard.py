"""Phase 7 item 4: a diff guard runs before anything is committed.

For a `repo:` task, after the worker and before the commit, the dispatcher scans what
changed. A denied path, an added file over DIFF_GUARD_MAX_KB, or an added line that
looks like a secret means: no commit, no push, no gh call, and the attempt fails with
feedback that names the path and the kind of pattern but never the value.
"""

import unittest

from test_baseline import PASS
from test_worktree_pr import PrCase

FAKE_GITHUB = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FAKE_NOTION = "ntn_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def writes(*files, result="Done."):
    return {"result": result, "write": [{"path": p, "content": c} for p, c in files]}


class GuardCase(PrCase):
    def run_one(self, *files, **meta):
        """One attempt only, so the feedback ends up in failed/ where the test can read it."""
        meta.setdefault("max_attempts", "1")
        if "feature.txt" not in dict(files):  # the task's verify: needs it, so a refusal can only come from the guard
            files += (("feature.txt", "hello\n"),)
        self.sb.set_scenario(worker=[writes(*files)], review=[PASS])
        self.pr_task(**meta)
        self.sb.run()

    def assertRefused(self, *needles):
        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertEqual(self.sb.names("done"), [])
        self.assertEqual(self.sb.origin_branches(), [], "nothing is pushed")
        self.assertEqual(self.sb.gh_calls(), [], "gh is not called")
        self.assertEqual(self.sb.claude_calls("review"), [], "a refused change is not worth a review")
        worktree = self.sb.base / "worktrees" / "t"
        self.assertEqual(self.sb.git(worktree, "rev-list", "--count", "main..HEAD"), "0", "nothing is committed")
        _, body = self.sb.read_task("failed", "t")
        for needle in needles:
            self.assertIn(needle, body)
        return body


class RefusedPathTests(GuardCase):
    def test_a_venv_is_refused_and_named(self):
        self.run_one((".venv/x.py", "print(1)\n"), ("feature.txt", "hi\n"))
        self.assertRefused(".venv/x.py")

    def test_a_pycache_file_is_refused_wherever_it_sits(self):
        self.run_one(("pkg/__pycache__/a.pyc", "bytes"), ("feature.txt", "hi\n"))
        self.assertRefused("pkg/__pycache__/a.pyc")

    def test_an_env_file_is_refused(self):
        self.run_one((".env", "SOMETHING=1\n"))
        self.assertRefused(".env")

    def test_a_key_file_is_refused(self):
        self.run_one(("deploy/id_rsa_work", "not really a key\n"))
        self.assertRefused("deploy/id_rsa_work")

    def test_an_env_example_is_not_an_env_file(self):
        self.run_one((".env.example", "SOMETHING=\n"), ("feature.txt", "hi\n"))
        self.assertEqual(self.sb.names("done"), ["t"])

    def test_the_feedback_tells_the_worker_how_to_fix_it(self):
        self.run_one((".venv/x.py", "print(1)\n"))
        body = self.assertRefused()
        self.assertIn(".gitignore", body)

    def test_widening_the_deny_list_takes_effect(self):
        self.sb.configure(DIFF_GUARD_DENY=".venv/,*.pem,notes.private")
        self.run_one(("notes.private", "mine\n"))
        self.assertRefused("notes.private")

    def test_loosening_the_deny_list_takes_effect(self):
        self.sb.configure(DIFF_GUARD_DENY="*.pem")
        self.run_one((".venv/x.py", "print(1)\n"))
        self.assertEqual(self.sb.names("done"), ["t"])


class SizeTests(GuardCase):
    def test_an_added_file_over_the_limit_is_refused(self):
        self.run_one(("big.txt", "x" * (2 * 1024 * 1024)))
        self.assertRefused("big.txt", "1024")

    def test_the_limit_is_a_setting(self):
        self.sb.configure(DIFF_GUARD_MAX_KB="4096")
        self.run_one(("big.txt", "x" * (2 * 1024 * 1024)))
        self.assertEqual(self.sb.names("done"), ["t"])


class SecretTests(GuardCase):
    def test_an_added_token_is_refused_and_never_repeated(self):
        self.run_one(("config.py", f'KEY = "{FAKE_GITHUB}"\n'))
        body = self.assertRefused("config.py", "GitHub")
        self.assertNotIn(FAKE_GITHUB, body)
        self.assertNotIn(FAKE_GITHUB[:12], body)
        self.assertNotIn(FAKE_GITHUB, "\n".join(self.sb.telegram.texts()))

    def test_a_notion_token_and_a_private_key_header_are_recognised(self):
        self.run_one(("a.txt", f"{FAKE_NOTION}\n"))
        self.assertRefused("a.txt", "Notion")

    def test_a_pem_header_is_refused(self):
        self.run_one(("k.txt", "-----BEGIN RSA PRIVATE KEY-----\nabc\n"))
        self.assertRefused("k.txt", "private key")

    def test_a_generic_assignment_is_refused(self):
        self.run_one(("settings.py", 'password = "hunter2hunter2hunter2"\n'))
        self.assertRefused("settings.py")

    def test_a_deleted_line_that_held_a_token_is_ignored(self):
        self.sb.git(self.sb.repo, "checkout", "-q", "-b", "seed")
        (self.sb.repo / "old.txt").write_text(f"{FAKE_GITHUB}\nkeep\n")
        self.sb.git(self.sb.repo, "add", "-A")
        self.sb.git(self.sb.repo, "commit", "-q", "-m", "oops")
        self.sb.git(self.sb.repo, "checkout", "-q", "main")
        self.sb.git(self.sb.repo, "merge", "-q", "--ff-only", "seed")
        self.sb.git(self.sb.repo, "push", "-q", "origin", "main")
        self.run_one(("old.txt", "keep\n"))
        self.assertEqual(self.sb.names("done"), ["t"])

    def test_a_normal_change_passes_and_is_published(self):
        self.run_one(("feature.txt", "hello\n"), ("docs/notes.md", "# Notes\n\nNothing secret here.\n"))
        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(self.sb.origin_branches(), ["task/t"])
        self.assertEqual(len(self.sb.pr_creates()), 1)


class RetryTests(GuardCase):
    def test_a_refusal_before_the_last_attempt_requeues_with_the_feedback(self):
        self.sb.set_scenario(worker=[writes((".venv/x.py", "print(1)\n"), ("feature.txt", "hi\n"))], review=[PASS])
        self.pr_task()
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        meta, body = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "1")
        self.assertIn(".venv/x.py", body)
        self.assertEqual(self.sb.claude_calls("review"), [])
        self.assertEqual(self.sb.origin_branches(), [])


if __name__ == "__main__":
    unittest.main()
