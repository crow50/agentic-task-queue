"""Phase 6: a task with `repo:` ends as a draft pull request.

The worker runs in a git worktree on branch task/<id>. Only if `verify:` and the
review pass does the dispatcher itself commit, push and run `gh pr create
--draft`, then send the link. A task without `repo:` never touches git or gh.
Real git against a bare "origin" in a temp dir; gh is tests/fake_gh.py.
"""

import json
import os
import shlex
import subprocess
import sys
import time
import unittest
from pathlib import Path

from test_baseline import PASS, Sandbox, fail

FAKE_GH = Path(__file__).resolve().parent / "fake_gh.py"
URL = "https://github.com/example/queue/pull/123"
VERIFY_FILE = "bash scripts/verify-has-feature.sh"


class RepoSandbox(Sandbox):
    def __init__(self):
        super().__init__()
        self.gh_log = self.root / "gh.log"
        self.gh_script = self.root / "gh-script.json"
        self.origin = self.root / "origin.git"
        self.repo = self.base / "workspace" / "proj"
        self.configure(
            GH_BIN=f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_GH))}",
            PUBLISH_RETRY_MINUTES="0",  # the default waits minutes between tries; tests retry at once
        )
        self.gh_rules([])
        self.make_repo()

    def env(self):
        return {**super().env(), "FAKE_GH_LOG": str(self.gh_log), "FAKE_GH_SCRIPT": str(self.gh_script)}

    def git(self, cwd, *args, check=True):
        proc = subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@localhost", *args],
            cwd=cwd, env=self.env(), capture_output=True, text=True,
        )
        if check and proc.returncode != 0:
            raise AssertionError(f"git {args} failed: {proc.stderr}")
        return proc.stdout.strip()

    def make_repo(self):
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        scripts = self.repo / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "verify-ok.sh").write_text("exit 0\n")
        (scripts / "verify-bad.sh").write_text("echo nope\nexit 1\n")
        (scripts / "verify-has-feature.sh").write_text("test -f feature.txt\n")
        (self.repo / "README.md").write_text("proj\n")
        self.git(self.repo, "init", "-q", "-b", "main")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "init")
        self.git(self.repo, "remote", "add", "origin", str(self.origin))
        self.git(self.repo, "push", "-q", "-u", "origin", "main")

    def gh_rules(self, rules):
        """Scripted gh answers, first match wins. Without one, `pr list` reports no open pull requests."""
        self.gh_script.write_text(json.dumps(rules + [{"match": "pr list", "stdout": "0\n"}]))

    def gh_calls(self):
        if not self.gh_log.exists():
            return []
        return [json.loads(line) for line in self.gh_log.read_text().splitlines()]

    def pr_creates(self):
        return [c for c in self.gh_calls() if c["argv"][:2] == ["pr", "create"]]

    def origin_branches(self):
        return self.git(self.origin, "branch", "--list", "task/*").replace("*", "").split()

    def origin_file(self, branch, path):
        return self.git(self.origin, "show", f"{branch}:{path}", check=False)


class PrCase(unittest.TestCase):
    def setUp(self):
        self.sb = RepoSandbox()
        self.addCleanup(self.sb.close)

    def pr_task(self, name="t", **meta):
        meta.setdefault("repo", "proj")
        meta.setdefault("verify", VERIFY_FILE)
        self.sb.add_task(name, **meta)

    def writes_feature(self, content="hello\n", result="Added feature.txt"):
        return {"result": result, "write": {"path": "feature.txt", "content": content}}


class DraftPullRequestTests(PrCase):
    def test_a_pass_pushes_task_t_and_opens_exactly_one_draft_pr_and_sends_the_link(self):
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.pr_task()
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(self.sb.origin_branches(), ["task/t"])
        self.assertEqual(self.sb.origin_file("task/t", "feature.txt"), "hello")
        (create,) = self.sb.pr_creates()
        argv = create["argv"]
        self.assertIn("--draft", argv)
        self.assertEqual(argv[argv.index("--head") + 1], "task/t")
        self.assertEqual(argv[argv.index("--base") + 1], "main")
        self.assertIn("/worktrees/t", create["cwd"])
        meta, _ = self.sb.read_task("done", "t")
        self.assertEqual((meta["pr_url"], meta["branch"], meta["tier"]), (URL, "task/t", "2"))
        report = "\n".join(self.sb.telegram.texts())
        self.assertIn(URL, report)
        self.assertIn("Draft PR", report)
        self.assertIn("bash scripts/verify-has-feature.sh", report)  # the verify that passed is named alongside the link

    def test_the_worker_runs_in_the_worktree_and_is_told_not_to_push(self):
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.pr_task()
        self.sb.run()

        (worker,) = self.sb.claude_calls("worker")
        argv = worker["argv"]
        disallowed = argv[argv.index("--disallowedTools") + 1]
        self.assertIn("Bash(git push*)", disallowed)
        self.assertIn("Bash(gh *)", disallowed)
        self.assertIn("task/t", worker["prompt"])
        self.assertTrue(worker["prompt"].startswith("You are running unattended"))
        self.assertFalse((self.sb.repo / "feature.txt").exists(), "the checkout itself is never touched")
        self.assertEqual(self.sb.git(self.sb.repo, "branch", "--show-current"), "main")

    def test_the_title_comes_from_the_first_line_of_the_task(self):
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.pr_task(body="# Add the feature file\n\nMake feature.txt.\n\n## Acceptance Criteria\n- feature.txt exists\n")
        self.sb.run()

        argv = self.sb.pr_creates()[0]["argv"]
        self.assertEqual(argv[argv.index("--title") + 1], "Add the feature file")

    def test_the_worktree_is_removed_after_a_pr_and_the_branch_stays_for_the_pr(self):
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.pr_task()
        self.sb.run()

        self.assertFalse((self.sb.base / "worktrees" / "t").exists())
        self.assertIn("task/t", self.sb.git(self.sb.repo, "branch", "--list", "task/*"))

    def test_no_gh_command_but_list_and_create_is_ever_run_and_nothing_merges(self):
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.pr_task()
        self.sb.run()

        for call in self.sb.gh_calls():
            self.assertEqual(call["argv"][0], "pr")
            self.assertIn(call["argv"][1], ("list", "create", "view"))  # view: Phase 7 checks the PR it just opened
            self.assertNotIn("merge", call["argv"])
            self.assertNotIn("ready", call["argv"])

    def test_a_task_without_a_repo_never_touches_git_or_gh(self):
        self.sb.add_task("plain")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["plain"])
        self.assertEqual(self.sb.gh_calls(), [])
        self.assertFalse((self.sb.base / "worktrees").exists())
        self.assertEqual(self.sb.origin_branches(), [])
        (worker,) = self.sb.claude_calls("worker")
        self.assertNotIn("--disallowedTools", worker["argv"])

    def test_the_base_branch_can_be_named(self):
        self.sb.git(self.sb.repo, "branch", "develop")
        self.sb.git(self.sb.repo, "push", "-q", "origin", "develop")
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.pr_task(base="develop")
        self.sb.run()

        argv = self.sb.pr_creates()[0]["argv"]
        self.assertEqual(argv[argv.index("--base") + 1], "develop")


class NoPullRequestTests(PrCase):
    def test_a_failing_verify_makes_no_push_and_no_gh_call(self):
        self.sb.set_scenario(worker=[self.writes_feature()])
        self.pr_task(verify="bash scripts/verify-bad.sh")
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        self.assertEqual(self.sb.origin_branches(), [])
        self.assertEqual(self.sb.gh_calls(), [])
        self.assertEqual(self.sb.claude_calls("review"), [])

    def test_the_retry_reuses_the_worktree_and_its_work_and_then_opens_the_pr(self):
        scratch = {"result": "tried", "write": {"path": "scratch.txt", "content": "from attempt 1\n"}}
        self.sb.set_scenario(worker=[scratch, self.writes_feature()], review=[PASS])
        self.pr_task()
        self.sb.run()
        self.assertEqual(self.sb.names("pending"), ["t"])  # no feature.txt yet: verify failed
        self.assertTrue((self.sb.base / "worktrees" / "t" / "scratch.txt").exists())

        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(self.sb.origin_file("task/t", "scratch.txt"), "from attempt 1")
        self.assertEqual(len(self.sb.pr_creates()), 1)

    def test_a_worker_that_changed_nothing_is_sent_round_again_with_no_pr(self):
        self.sb.set_scenario(worker=[{"result": "I did nothing"}], review=[PASS])
        self.pr_task(verify="bash scripts/verify-ok.sh")
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        meta, body = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "1")
        self.assertIn("no changes", body)
        self.assertEqual(self.sb.pr_creates(), [])
        self.assertEqual(self.sb.origin_branches(), [])

    def test_a_review_that_fails_opens_no_pr(self):
        self.sb.set_scenario(worker=[self.writes_feature()], review=[fail("not what was asked")])
        self.pr_task()
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        self.assertEqual(self.sb.gh_calls(), [])
        self.assertEqual(self.sb.origin_branches(), [])


class PublishFailureTests(PrCase):
    GH_FAILS = [{"match": "pr create", "exit": 1, "stderr": "gh: HTTP 502"}]

    def test_a_gh_failure_costs_no_attempt_and_no_second_worker_or_review_call(self):
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.sb.gh_rules(self.GH_FAILS)
        self.pr_task()
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        meta, _ = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "0")
        self.assertTrue(meta["pending_publish"])
        self.assertEqual(self.sb.origin_branches(), [], "the pushed branch is deleted when gh fails")

        self.sb.gh_rules([])
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(len(self.sb.claude_calls("worker")), 1)
        self.assertEqual(len(self.sb.claude_calls("review")), 1)
        self.assertEqual(self.sb.origin_branches(), ["task/t"])
        self.assertEqual(self.sb.read_task("done", "t")[0]["pr_url"], URL)

    def test_three_failed_publishes_move_the_task_to_failed_and_keep_the_worktree(self):
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.sb.gh_rules(self.GH_FAILS)
        self.pr_task()
        for _ in range(3):
            self.sb.run()

        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertTrue((self.sb.base / "worktrees" / "t" / "feature.txt").exists())
        self.assertTrue(any("could not open a pull request" in t for t in self.sb.telegram.texts()))

    def test_a_failed_publish_waits_before_the_next_try(self):
        self.sb.configure(PUBLISH_RETRY_MINUTES="10")
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.sb.gh_rules(self.GH_FAILS)
        self.pr_task()
        self.sb.run()
        self.sb.run()

        self.assertEqual(len(self.sb.pr_creates()), 1)  # the second run left it alone
        meta, _ = self.sb.read_task("pending", "t")
        self.assertIn("held_until", meta)

    def test_a_missing_remote_is_a_publish_failure_not_a_crash(self):
        self.sb.git(self.sb.repo, "remote", "remove", "origin")
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.pr_task()
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        self.assertEqual(self.sb.pr_creates(), [])


class OpenPrCapTests(PrCase):
    def open_prs(self, count):
        self.sb.gh_rules([{"match": "pr list", "stdout": f"{count}\n"}])

    def test_at_the_cap_the_result_is_held_with_one_notice_and_no_pr(self):
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.open_prs(3)
        self.pr_task()
        self.sb.run()
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        self.assertEqual(self.sb.pr_creates(), [])
        notices = [t for t in self.sb.telegram.texts() if "held" in t.lower()]
        self.assertEqual(len(notices), 1)
        self.assertEqual(len(self.sb.claude_calls("worker")), 1)

    def test_a_held_result_is_not_rechecked_until_its_hold_passes(self):
        self.sb.configure(PUBLISH_RETRY_MINUTES="10")
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.open_prs(3)
        self.pr_task()
        self.sb.run()
        self.sb.run()

        lists = [c for c in self.sb.gh_calls() if c["argv"][:2] == ["pr", "list"]]
        self.assertEqual(len(lists), 1)

    def test_the_held_result_is_published_once_a_pr_is_closed(self):
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.open_prs(3)
        self.pr_task()
        self.sb.run()
        self.open_prs(2)
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(len(self.sb.pr_creates()), 1)

    def test_the_cap_is_configurable_and_zero_means_no_cap(self):
        self.sb.configure(MAX_OPEN_PRS="0")
        self.open_prs(50)
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.pr_task()
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])


class PruneWorktreeTests(PrCase):
    def add_worktree(self, name, age_days):
        path = self.sb.base / "worktrees" / name
        path.parent.mkdir(exist_ok=True)
        self.sb.git(self.sb.repo, "worktree", "add", "-q", "-b", f"task/{name}", str(path))
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_old_worktrees_of_finished_tasks_are_deleted_and_the_rest_kept(self):
        old = self.add_worktree("old", 40)
        fresh = self.add_worktree("fresh", 1)
        waiting = self.add_worktree("waiting", 40)
        self.sb.add_task("waiting", state="proposed")  # still has a task behind it
        self.sb.run()

        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(waiting.exists())
        self.assertNotIn("worktrees/old", self.sb.git(self.sb.repo, "worktree", "list"))

    def test_a_retention_of_zero_keeps_everything(self):
        self.sb.configure(LOG_RETENTION_DAYS="0")
        old = self.add_worktree("old", 400)
        self.sb.run()
        self.assertTrue(old.exists())


class RepoValidationTests(PrCase):
    def check(self, **meta):
        self.pr_task("v", **meta)
        proc = self.sb.python(str(self.sb.base / "dispatcher.py"), "check", str(self.sb.base / "tasks" / "pending" / "v.md"))
        return proc.returncode, proc.stdout

    def test_a_repo_that_is_a_git_checkout_is_accepted(self):
        self.assertEqual(self.check()[0], 0)

    def test_an_unknown_repo_is_rejected_by_name(self):
        code, out = self.check(repo="nope")
        self.assertNotEqual(code, 0)
        self.assertIn("nope", out)

    def test_a_directory_that_is_not_a_repo_is_rejected(self):
        (self.sb.base / "workspace" / "plain").mkdir()
        code, out = self.check(repo="plain")
        self.assertNotEqual(code, 0)
        self.assertIn("git", out)

    def test_a_relative_path_with_a_slash_is_rejected_but_an_absolute_path_is_fine(self):
        self.assertNotEqual(self.check(repo="../proj")[0], 0)
        self.assertEqual(self.check(repo=str(self.sb.repo))[0], 0)

    def test_repos_dir_decides_where_names_are_looked_up(self):
        self.sb.configure(REPOS_DIR=str(self.sb.repo.parent))
        self.assertEqual(self.check()[0], 0)
        self.sb.configure(REPOS_DIR=str(self.sb.root))
        self.assertNotEqual(self.check()[0], 0)


if __name__ == "__main__":
    unittest.main()
