"""Phase 7 items 2 and 3: publish permission is a setup problem, and a report never says more than was checked.

A 401 or 403 from `git push` or `gh` means the credentials cannot do the job; a retry every
15 minutes never helps. The finished branch and result are kept (`pending_publish`), no attempt
is used, one message names the fix, and the task resumes once a cheap probe succeeds.

"Draft PR ready" is sent only after `gh pr view` shows an open draft on the right branch and
the remote branch sits at the commit the dispatcher pushed. A `deliver:` file counts as sent
only when Telegram answers ok: true.
"""

import json
import shlex
import stat
import unittest

from test_baseline import PASS
from test_worktree_pr import URL, PrCase

DENIED_STDERR = (
    "remote: Permission to example/queue.git denied to crow50.\n"
    "fatal: unable to access 'https://github.com/example/queue.git/': The requested URL returned error: 403"
)
PROBE_READ = {"match": "repo view", "stdout": json.dumps({"viewerPermission": "READ"})}
PROBE_WRITE = {"match": "repo view", "stdout": json.dumps({"viewerPermission": "WRITE"})}


def view(url=URL, state="OPEN", draft=True, head="task/t"):
    return {"match": "pr view", "stdout": json.dumps({"url": url, "state": state, "isDraft": draft, "headRefName": head})}


class TrustCase(PrCase):
    def hook(self, name, script):
        path = self.sb.origin / "hooks" / name
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def deny_pushes(self):
        """GitHub's own words for a token without write access, as a pre-receive hook on the test origin."""
        lines = "".join(f"echo {shlex.quote(line)} >&2\n" for line in DENIED_STDERR.splitlines())
        self.hook("pre-receive", f'echo hit >> "{self.sb.origin}/denied.calls"\n{lines}exit 1\n')

    def allow_pushes(self):
        (self.sb.origin / "hooks" / "pre-receive").unlink()

    def denied_calls(self):
        path = self.sb.origin / "denied.calls"
        return len(path.read_text().split()) if path.exists() else 0

    def fix_messages(self):
        return [t for t in self.sb.telegram.texts() if "Contents" in t]

    def ready_messages(self):
        return [t for t in self.sb.telegram.texts() if "Draft PR ready" in t]

    def finished(self, **meta):
        self.sb.set_scenario(worker=[self.writes_feature()], review=[PASS])
        self.pr_task(**meta)


class ClassifyTests(TrustCase):
    def classify(self, text):
        code = "import dispatcher, sys; print(dispatcher.publish_auth_problem(sys.argv[1]))"
        return self.sb.python("-c", code, text).stdout.strip() == "True"

    def test_credential_failures_are_recognised(self):
        for text in (
            DENIED_STDERR,
            "git push failed: remote: Permission to crow50/x.git denied to crow50.",
            "remote: Invalid username or token. Password authentication is not supported",
            "fatal: Authentication failed for 'https://github.com/crow50/x.git/'",
            "gh: HTTP 403: Resource not accessible by personal access token (https://api.github.com/repos/x/pulls)",
            "gh: HTTP 401: Bad credentials",
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
            "git@github.com: Permission denied (publickey).",
        ):
            with self.subTest(text[:50]):
                self.assertTrue(self.classify(text))

    def test_other_failures_are_not(self):
        for text in (
            "gh: HTTP 502 Bad Gateway",
            "fatal: unable to access 'https://github.com/x.git/': Could not resolve host: github.com",
            "! [rejected] task/t -> task/t (non-fast-forward)",
            "error: failed to push some refs; pre-receive hook declined: line 4030 of 5403",
        ):
            with self.subTest(text[:50]):
                self.assertFalse(self.classify(text))


class DeniedPushTests(TrustCase):
    def test_a_403_parks_the_finished_work_without_using_an_attempt(self):
        self.finished()
        self.deny_pushes()
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        meta, _ = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "0")
        self.assertTrue(meta["pending_publish"])
        self.assertNotIn("publish_failures", meta)
        self.assertEqual(self.sb.origin_branches(), [])
        self.assertTrue((self.sb.base / "worktrees" / "t" / "feature.txt").exists())
        self.assertEqual(len(self.sb.claude_calls("worker")), 1)
        self.assertEqual(len(self.sb.claude_calls("review")), 1)

    def test_one_message_names_the_fix(self):
        self.finished()
        self.deny_pushes()
        self.sb.run()

        (message,) = self.fix_messages()
        self.assertIn("Contents", message)
        self.assertIn("read and write", message)
        self.assertIn("t.md", message)
        self.assertNotIn("Permission to example/queue.git denied to crow50.\nfatal", message)  # a fix, not a stack of git noise
        self.assertEqual([t for t in self.sb.telegram.texts() if "could not open a pull request" in t], [])

    def test_the_next_run_makes_one_probe_and_nothing_else(self):
        self.finished()
        self.deny_pushes()
        self.sb.gh_rules([PROBE_READ])
        self.sb.run()
        calls_before, pushes_before = len(self.sb.gh_calls()), self.denied_calls()
        messages_before = len(self.sb.telegram.texts())

        self.sb.run()
        self.sb.run()

        new = self.sb.gh_calls()[calls_before:]
        self.assertEqual([c["argv"][:2] for c in new], [["repo", "view"], ["repo", "view"]])  # one per run
        self.assertEqual(self.denied_calls(), pushes_before, "no push while the probe fails")
        self.assertEqual(len(self.sb.claude_calls("worker")), 1)
        self.assertEqual(len(self.sb.claude_calls("review")), 1)
        self.assertEqual(len(self.sb.telegram.texts()), messages_before, "silent while it is still broken")
        self.assertEqual(self.sb.names("pending"), ["t"])

    def test_once_the_probe_passes_the_branch_is_pushed_without_another_worker_call(self):
        self.finished()
        self.deny_pushes()
        self.sb.gh_rules([PROBE_READ])
        self.sb.run()

        self.allow_pushes()
        self.sb.gh_rules([PROBE_WRITE])
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(self.sb.origin_branches(), ["task/t"])
        self.assertEqual(len(self.sb.claude_calls("worker")), 1)
        self.assertEqual(len(self.sb.claude_calls("review")), 1)
        self.assertEqual(self.sb.read_task("done", "t")[0]["pr_url"], URL)
        self.assertEqual(len(self.ready_messages()), 1)

    def test_a_probe_that_cannot_reach_the_remote_does_not_publish(self):
        self.finished()
        self.deny_pushes()
        self.sb.run()
        self.sb.gh_rules([PROBE_WRITE])
        self.sb.git(self.sb.repo, "remote", "set-url", "origin", str(self.sb.root / "nowhere.git"))
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        self.assertEqual(self.sb.pr_creates(), [])

    def test_a_token_that_can_read_but_not_push_is_not_a_pass(self):
        """The repo role (viewerPermission) is the person's, not the token's: a fine-grained token with Contents
        read-only still sees ADMIN. Only the remote's answer to a push tells."""
        self.finished()
        self.deny_pushes()
        self.sb.run()
        pushes = self.denied_calls()

        self.sb.gh_rules([PROBE_WRITE])
        self.sb.git(self.sb.repo, "remote", "set-url", "--push", "origin", str(self.sb.root / "refuses-pushes.git"))
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        self.assertEqual(self.sb.pr_creates(), [])
        self.assertEqual(self.denied_calls(), pushes, "the real push is not tried while the probe says no")
        meta, _ = self.sb.read_task("pending", "t")
        self.assertNotIn("publish_failures", meta, "the probe holds the task; a failed push would have counted")

    def test_a_403_from_gh_pr_create_is_the_same_kind_of_problem(self):
        self.finished()
        self.sb.gh_rules([{"match": "pr create", "exit": 1,
                           "stderr": "gh: HTTP 403: Resource not accessible by personal access token"}])
        self.sb.run()

        meta, _ = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "0")
        self.assertNotIn("publish_failures", meta)
        self.assertEqual(len(self.fix_messages()), 1)
        self.assertIn("Pull requests", self.fix_messages()[0])

    def test_an_ordinary_publish_failure_still_counts_and_sends_no_fix_message(self):
        self.finished()
        self.sb.gh_rules([{"match": "pr create", "exit": 1, "stderr": "gh: HTTP 502"}])
        self.sb.run()

        meta, _ = self.sb.read_task("pending", "t")
        self.assertEqual(meta["publish_failures"], "1")
        self.assertEqual(self.fix_messages(), [])


class ConfirmBeforeClaimingTests(TrustCase):
    def assertNotClaimed(self):
        self.assertEqual(self.ready_messages(), [])
        self.assertEqual(self.sb.names("done"), [])

    def test_a_matching_pull_request_is_claimed(self):
        self.finished()
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        (ready,) = self.ready_messages()
        self.assertIn(URL, ready)
        views = [c["argv"] for c in self.sb.gh_calls() if c["argv"][:2] == ["pr", "view"]]
        self.assertEqual(len(views), 1)
        self.assertIn("url,state,isDraft,headRefName", views[0])

    def test_a_pull_request_gh_cannot_find_is_not_claimed(self):
        self.finished()
        self.sb.gh_rules([{"match": "pr view", "exit": 1, "stderr": "no pull requests found"}])
        self.sb.run()

        self.assertNotClaimed()
        self.assertEqual(self.sb.names("pending"), ["t"])
        meta, _ = self.sb.read_task("pending", "t")
        self.assertTrue(meta["pending_publish"])

    def test_a_pull_request_that_is_not_a_draft_is_not_claimed(self):
        self.finished()
        self.sb.gh_rules([view(draft=False)])
        self.sb.run()
        self.assertNotClaimed()

    def test_a_closed_pull_request_is_not_claimed(self):
        self.finished()
        self.sb.gh_rules([view(state="CLOSED")])
        self.sb.run()
        self.assertNotClaimed()

    def test_a_pull_request_from_the_wrong_branch_is_not_claimed(self):
        self.finished()
        self.sb.gh_rules([view(head="somebody/else")])
        self.sb.run()
        self.assertNotClaimed()

    def test_a_remote_branch_that_is_not_at_the_pushed_commit_is_not_claimed(self):
        self.finished()
        self.hook("post-receive", "git update-ref refs/heads/task/t refs/heads/main\n")
        self.sb.run()
        self.assertNotClaimed()

    def test_three_failed_checks_end_in_failed_with_the_worktree_kept(self):
        self.finished()
        self.sb.gh_rules([view(draft=False)])
        for _ in range(3):
            self.sb.run()

        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertTrue((self.sb.base / "worktrees" / "t" / "feature.txt").exists())
        self.assertEqual(self.ready_messages(), [])
        self.assertEqual(len(self.sb.claude_calls("worker")), 1)
        (failure,) = [t for t in self.sb.telegram.texts() if "could not open a pull request" in t]
        self.assertIn("not a draft", failure)  # the report says what the check found

    def test_a_retry_after_a_flaky_view_adopts_the_pull_request_that_already_exists(self):
        self.finished()
        self.sb.gh_rules([{"match": "pr view", "exit": 1, "stderr": "HTTP 502"}])
        self.sb.run()
        self.assertEqual(self.sb.names("pending"), ["t"])

        # the PR was created on the first run, so gh now refuses to create a second one
        self.sb.gh_rules([
            {"match": "pr create", "exit": 1, "stderr": "a pull request for branch \"task/t\" already exists"},
            view(),
        ])
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(self.sb.origin_branches(), ["task/t"], "the branch behind a live PR is not deleted")
        self.assertEqual(len(self.ready_messages()), 1)


class DeliveryConfirmationTests(TrustCase):
    def test_a_file_telegram_did_not_accept_is_named_as_not_delivered(self):
        (self.sb.base / "workspace" / "out").mkdir(parents=True)
        (self.sb.base / "workspace" / "out" / "data.csv").write_text("a,b\n1,2\n")
        (self.sb.base / "workspace" / "out" / "ok.txt").write_text("fine\n")
        self.sb.telegram.queue_response("sendDocument", 200, {"ok": False, "description": "Bad Request: file rejected"})
        self.sb.set_scenario(worker=[{"result": "Wrote them."}], review=[PASS])
        self.sb.add_task("t", deliver="out/data.csv, out/ok.txt")
        self.sb.run()

        self.assertEqual(self.sb.names("done"), ["t"])
        texts = self.sb.telegram.texts()
        self.assertTrue(any("not delivered: out/data.csv" in t for t in texts), texts)
        self.assertFalse(any("not delivered: out/ok.txt" in t for t in texts), texts)

    def test_a_delivered_file_is_not_reported_as_missing(self):
        (self.sb.base / "workspace").mkdir(parents=True, exist_ok=True)
        (self.sb.base / "workspace" / "a.md").write_text("x\n")
        self.sb.set_scenario(worker=[{"result": "Wrote it."}], review=[PASS])
        self.sb.add_task("t", deliver="a.md")
        self.sb.run()

        self.assertFalse(any("not delivered" in t.lower() for t in self.sb.telegram.texts()))


if __name__ == "__main__":
    unittest.main()
