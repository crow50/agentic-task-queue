"""Phase 5 (PLAN.md): learning loop.

Written before the code. The queue records every failed attempt in
logs/lessons.jsonl; a weekly recurring task turns those lessons into a patch
file in tasks/proposals/; scripts/open_proposal_pr.py checks the patch, runs the
tests on it and opens a pull request only if everything passes. Nothing here
applies a change to the running queue, and nothing may touch dispatcher.py.

Dispatcher tests run dispatcher.py in the Sandbox the baseline tests use. The
opener tests run the real script against a throwaway git repo with a local bare
repo as "origin" and tests/fake_gh.py in place of `gh`.
"""

import difflib
import importlib.util
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_baseline import PASS, REPO, TESTS, Sandbox, fail
from test_codeowners import rules

FAKE_GH = TESTS / "fake_gh.py"
OPENER = REPO / "scripts" / "open_proposal_pr.py"
TEMPLATE = REPO / "tasks" / "examples" / "weekly-improver.md"
ISO_Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
NO_LIMIT_STEP = {"exit": 1, "stdout": "", "stderr": "Claude AI usage limit reached"}


def read_lessons(sb):
    path = sb.base / "logs" / "lessons.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def frontmatter(path):
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", Path(path).read_text(), re.S)
    return dict(line.split(": ", 1) for line in match.group(1).splitlines()), match.group(2)


# ---------------------------------------------------------------- 1. the lessons log


class LessonsTests(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def test_a_failed_review_appends_one_lesson(self):
        self.sb.set_scenario(review=[fail("missing the report")])
        self.sb.add_task("t")
        self.sb.run()
        (lesson,) = read_lessons(self.sb)
        self.assertEqual(sorted(lesson), ["attempt", "feedback", "task", "time"])
        self.assertEqual((lesson["task"], lesson["attempt"]), ("t", 1))
        self.assertIn("missing the report", lesson["feedback"])
        self.assertRegex(lesson["time"], ISO_Z)

    def test_every_failed_attempt_is_recorded(self):
        self.sb.set_scenario(review=[fail("first problem"), fail("second problem")])
        self.sb.add_task("t", max_attempts="2")
        self.sb.run()
        self.sb.run()
        lessons = read_lessons(self.sb)
        self.assertEqual([(x["task"], x["attempt"]) for x in lessons], [("t", 1), ("t", 2)])
        self.assertIn("second problem", lessons[1]["feedback"])

    def test_a_worker_error_is_recorded(self):
        self.sb.set_scenario(worker=[{"exit": 1, "stdout": "", "stderr": "boom: chosen message"}])
        self.sb.add_task("t")
        self.sb.run()
        (lesson,) = read_lessons(self.sb)
        self.assertIn("Worker invocation failed", lesson["feedback"])
        self.assertIn("boom: chosen message", lesson["feedback"])

    def test_an_unparseable_verdict_is_recorded(self):
        self.sb.set_scenario(review=[{"result": "Looks fine to me."}])
        self.sb.add_task("t")
        self.sb.run()
        (lesson,) = read_lessons(self.sb)
        self.assertIn("no parseable verdict", lesson["feedback"])

    def test_a_pass_records_nothing(self):
        self.sb.set_scenario(review=[PASS])
        self.sb.add_task("t")
        self.sb.run()
        self.assertEqual(read_lessons(self.sb), [])

    def test_a_usage_limit_is_not_a_lesson(self):
        self.sb.set_scenario(worker=[NO_LIMIT_STEP])
        self.sb.add_task("t")
        self.sb.run()
        self.assertEqual(read_lessons(self.sb), [])

    def test_long_feedback_is_capped(self):
        self.sb.set_scenario(review=[fail("x" * 5000)])
        self.sb.add_task("t")
        self.sb.run()
        (lesson,) = read_lessons(self.sb)
        self.assertLessEqual(len(lesson["feedback"]), 2000)

    def test_an_unwritable_log_never_fails_the_task(self):
        (self.sb.base / "logs" / "lessons.jsonl").mkdir(parents=True)  # appending to a directory raises OSError
        self.sb.set_scenario(review=[fail("still reported")])
        self.sb.add_task("t")
        self.sb.run()
        self.assertEqual(self.sb.names("pending"), ["t"])
        self.assertIn("still reported", self.sb.read_task("pending", "t")[1])


# ---------------------------------------------------------------- 2. the preamble lives in a file


class PreambleFileTests(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.preamble = self.sb.base / "worker_preamble.md"

    def test_the_repo_ships_the_preamble_file(self):
        text = (REPO / "worker_preamble.md").read_text()
        self.assertTrue(text.startswith("You are running unattended inside an automated task queue."))
        self.assertIn("## Attempt N Feedback", text)
        self.assertIn("`deliver:`", text)

    def test_the_dispatcher_no_longer_defines_it(self):
        self.assertNotIn("You are running unattended", (REPO / "dispatcher.py").read_text())

    def test_the_worker_prompt_is_the_file_then_the_task(self):
        marker = "You are running unattended; custom marker 7f3a.\n"
        self.preamble.write_text(marker)
        self.sb.add_task("t")
        self.sb.run()
        (call,) = self.sb.claude_calls("worker")
        self.assertTrue(call["prompt"].startswith(marker + "\n\n# Task"), call["prompt"][:200])

    def test_a_missing_file_stops_the_dispatcher_with_a_clear_message(self):
        self.preamble.unlink()
        self.sb.add_task("t")
        proc = self.sb.python(str(self.sb.base / "dispatcher.py"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("worker_preamble.md", proc.stderr)
        self.assertEqual(self.sb.names("pending"), ["t"])
        self.assertEqual(self.sb.claude_calls(), [])


# ---------------------------------------------------------------- 3. quiet tasks


class QuietTaskTests(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def sent(self):
        return self.sb.telegram.calls("sendMessage") + self.sb.telegram.calls("sendDocument")

    def test_a_normal_pass_still_reports(self):
        self.sb.add_task("t")
        self.sb.run()
        self.assertTrue(any("Task done" in (text or "") for text in self.sb.telegram.texts()))

    def test_a_quiet_pass_sends_nothing(self):
        self.sb.add_task("t", quiet="true")
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(self.sent(), [])

    def test_a_quiet_task_that_fails_for_good_still_reports(self):
        self.sb.set_scenario(review=[fail("nope")])
        self.sb.add_task("t", quiet="true", max_attempts="1")
        self.sb.run()
        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertTrue(any("Task failed" in (text or "") for text in self.sb.telegram.texts()))


# ---------------------------------------------------------------- 4. the loop, end to end

SEED_FAILURES = [
    ("fetch-prices", 1, "- The worker said 'see report.md' but the reader only sees Telegram."),
    ("fetch-prices", 2, "- The worker said 'see report.md' but the reader only sees Telegram."),
    ("file-issue", 1, "- A denied Bash(gh issue create) command was retried instead of reported."),
    ("file-issue", 2, "- A denied Bash(gh issue create) command was retried instead of reported."),
    ("summarize-inbox", 1, "- Criterion 'the summary is good' cannot be checked from the files."),
]


class LoopSandbox(Sandbox):
    """The Sandbox plus the files the improver reads, committed to a git repo."""

    EXTRAS = (
        "worker_preamble.md", "coordinator/CLAUDE.md.template", ".gitignore",
        "scripts/open_proposal_pr.py", "tasks/examples/weekly-improver.md",
    )

    def __init__(self):
        super().__init__()
        for rel in self.EXTRAS:
            target = self.base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, target)
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "baseline")

    def git(self, *args, check=True):
        proc = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=self.base, env=self.env(), capture_output=True, text=True,
        )
        if check and proc.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
        return proc


class ImproverLoopTests(unittest.TestCase):
    def setUp(self):
        self.sb = LoopSandbox()
        self.addCleanup(self.sb.close)
        logs = self.sb.base / "logs"
        logs.mkdir()
        (logs / "lessons.jsonl").write_text("".join(
            json.dumps({"task": task, "attempt": attempt, "feedback": feedback, "time": "2026-10-06T07:00:00Z"}) + "\n"
            for task, attempt, feedback in SEED_FAILURES
        ))
        self.install_template()

    def install_template(self):
        text = TEMPLATE.read_text().replace("__QUEUE_DIR__", str(self.sb.base))
        text = re.sub(r"^schedule:.*$", "schedule: every 1h", text, flags=re.M)
        recurring = self.sb.base / "tasks" / "recurring"
        recurring.mkdir(parents=True, exist_ok=True)
        (recurring / "weekly-improver.md").write_text(text)

    def preamble_patch(self):
        old = (self.sb.base / "worker_preamble.md").read_text().splitlines(keepends=True)
        new = old + ["- If a command is denied, say which one instead of retrying it.\n"]
        diff = "".join(difflib.unified_diff(old, new, "a/worker_preamble.md", "b/worker_preamble.md"))
        return "Two of five lessons were denied commands that were retried.\n\n" + diff

    def run_cycle(self, step):
        self.sb.set_scenario(worker=[step])
        self.sb.run()

    def test_five_failures_produce_a_patch_and_dispatcher_py_is_untouched(self):
        patch = "tasks/proposals/20261011-denied-commands.patch"
        self.run_cycle({"write": {"path": patch, "content": self.preamble_patch()}, "result": f"Wrote {patch}"})

        patches = sorted((self.sb.base / "tasks" / "proposals").glob("*.patch"))
        self.assertEqual([p.name for p in patches], ["20261011-denied-commands.patch"])
        self.assertEqual(self.sb.git("diff", "--exit-code", "--", "dispatcher.py").returncode, 0)
        self.assertEqual(self.sb.git("status", "--porcelain", "--", "dispatcher.py").stdout, "")
        self.assertEqual(self.sb.git("apply", "--check", str(patches[0])).returncode, 0)

    def test_the_improver_run_is_quiet_and_completes(self):
        self.run_cycle({"result": "no proposal"})
        (state, _), = self.sb.find("weekly-improver-*")
        self.assertEqual(state, "done")
        self.assertEqual(self.sb.telegram.calls("sendMessage"), [])
        self.assertEqual(list((self.sb.base / "tasks").glob("proposals/*.patch")), [])
        self.assertEqual(len(read_lessons(self.sb)), len(SEED_FAILURES))

    def test_the_worker_can_only_write_proposals_and_never_edit(self):
        self.run_cycle({"result": "no proposal"})
        (call,) = self.sb.claude_calls("worker")
        tools = call["allowed_tools"].split(",")
        self.assertNotIn("Edit", tools)
        self.assertNotIn("Write", tools)
        self.assertTrue(any(t.startswith("Write(") and "tasks/proposals" in t for t in tools), tools)
        self.assertFalse([t for t in tools if t.startswith("Bash") and not t.startswith("Bash(python3 scripts/open_proposal_pr.py")], tools)
        self.assertIn("logs/lessons.jsonl", call["prompt"])
        self.assertIn("scripts/open_proposal_pr.py", call["prompt"])

    def test_a_week_with_nothing_to_learn_leaves_no_patch(self):
        (self.sb.base / "logs" / "lessons.jsonl").write_text("")
        self.run_cycle({"result": "no proposal"})
        self.assertEqual(list((self.sb.base / "tasks").glob("proposals/*")), [])


class ImproverTemplateTests(unittest.TestCase):
    def test_template_is_weekly_quiet_and_scoped(self):
        meta, body = frontmatter(TEMPLATE)
        self.assertRegex(meta["schedule"], r"^weekly on \w+ at \d\d:\d\d$")
        self.assertEqual(meta["quiet"], "true")
        self.assertEqual(meta["cwd"], "__QUEUE_DIR__")
        tools = meta["allowed_tools"].split(",")
        self.assertNotIn("Edit", tools)
        self.assertNotIn("Write", tools)
        self.assertIn("## Acceptance Criteria", body)
        for allowed in ("worker_preamble.md", "coordinator/CLAUDE.md.template", "tests/test_", ".claude/skills/"):
            self.assertIn(allowed, body)
        self.assertIn("dispatcher.py", body)  # named, so the worker is told to leave it alone
        self.assertRegex(body, r"at least (2|two)")

    def test_proposals_are_runtime_files_not_committed(self):
        self.assertIn("tasks/proposals/", (REPO / ".gitignore").read_text().splitlines())

    def test_the_opener_is_owned(self):
        found = rules()
        self.assertIn("/scripts/", found)
        self.assertTrue(all(o.startswith("@") for o in found["/scripts/"]))
        self.assertTrue(OPENER.is_file())


# ---------------------------------------------------------------- 5. the opener


class DefaultTestCommandTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location("open_proposal_pr", OPENER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_checkouts_venv_python_is_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / ".venv" / "bin" / "python3"
            python.parent.mkdir(parents=True)
            python.write_text("")
            cmd = shlex.split(self.load().default_test_cmd(Path(tmp)))
            self.assertEqual(cmd, [str(python), "-m", "unittest", "discover", "-s", "tests"])

    def test_without_a_venv_it_uses_the_running_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = shlex.split(self.load().default_test_cmd(Path(tmp)))
            self.assertEqual(cmd[0], sys.executable)

EXISTING_FILES = {
    "dispatcher.py": "# dispatcher\n",
    "worker_preamble.md": "You are running unattended.\n- Work in the current directory.\n",
    "coordinator/CLAUDE.md.template": "# Coordinator\n",
    "tests/test_existing.py": "def test_ok():\n    pass\n",
    ".github/CODEOWNERS": "/dispatcher.py @crow50\n",
}


class OpenerCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="taskq-opener-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.origin = self.root / "origin.git"
        self.repo = self.root / "repo"
        self.gh_log = self.root / "gh.jsonl"
        (self.root / "home").mkdir()
        self.repo.mkdir()
        self.run_git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.root)
        self.run_git("init", "-q", "-b", "main", cwd=self.repo)
        for rel, text in EXISTING_FILES.items():
            self.put(rel, text)
        self.put("scripts/open_proposal_pr.py", OPENER.read_text())
        self.commit("baseline")
        self.run_git("remote", "add", "origin", str(self.origin))
        self.run_git("push", "-q", "-u", "origin", "main")

    def env(self, **extra):
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(self.root / "home"), "LC_ALL": "C.UTF-8",
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
            "FAKE_GH_LOG": str(self.gh_log), **extra,
        }

    def run_git(self, *args, cwd=None, git_dir=None):
        cmd = ["git"] + (["--git-dir", str(git_dir)] if git_dir else []) + list(args)
        proc = subprocess.run(cmd, cwd=cwd or self.repo, env=self.env(), capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(f"{' '.join(cmd)}: {proc.stderr}")
        return proc.stdout

    def put(self, rel, text):
        target = self.repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message):
        self.run_git("add", "-A")
        self.run_git("commit", "-q", "-m", message)

    def make_patch(self, changes, delete=()):
        """A real `git diff` for {path: new text} (and deleted paths), leaving the repo as it was."""
        for rel, text in changes.items():
            self.put(rel, text)
        for rel in delete:
            (self.repo / rel).unlink()
        self.run_git("add", "-A")
        patch = self.run_git("diff", "--cached", "--no-color")
        self.run_git("reset", "-q", "--hard", "HEAD")
        self.run_git("clean", "-fdq")
        return patch

    def open_pr(self, patch, name="p1", test_cmd="true", env=None):
        target = self.repo / "tasks" / "proposals" / f"{name}.patch"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(patch)
        gh = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_GH))}"
        return subprocess.run(
            [sys.executable, "scripts/open_proposal_pr.py", f"tasks/proposals/{name}.patch",
             "--test-cmd", test_cmd, "--gh", gh],
            cwd=self.repo, env=self.env(**(env or {})), capture_output=True, text=True, timeout=120,
        )

    def gh_calls(self):
        return [json.loads(line) for line in self.gh_log.read_text().splitlines()] if self.gh_log.exists() else []

    def remote_branches(self):
        return self.run_git("branch", "--list", "proposal/*", git_dir=self.origin).split()

    def proposals(self, sub, suffix=".patch"):
        return sorted(p.name for p in (self.repo / "tasks" / "proposals" / sub).glob(f"*{suffix}"))

    def assert_rejected(self, proc, reason_part=None, name="p1"):
        self.assertEqual(proc.returncode, 0, proc.stderr)  # a rejected proposal is not an error for the queue
        self.assertEqual(self.gh_calls(), [], "gh must not be called")
        self.assertEqual(self.remote_branches(), [], "nothing may be pushed")
        self.assertEqual(self.proposals("rejected"), [f"{name}.patch"])
        reason = (self.repo / "tasks" / "proposals" / "rejected" / f"{name}.reason").read_text()
        self.assertTrue(reason.strip())
        if reason_part:
            self.assertIn(reason_part, reason)
        self.assertEqual(self.proposals("opened"), [])
        self.assert_clean()

    def assert_clean(self):
        self.assertEqual(self.run_git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(len(self.run_git("worktree", "list").splitlines()), 1, "temporary worktree left behind")
        self.assertEqual(self.run_git("branch", "--list", "proposal/*").split(), [], "local proposal branch left behind")
        self.assertEqual((self.repo / "dispatcher.py").read_text(), EXISTING_FILES["dispatcher.py"])


class OpenerAcceptTests(OpenerCase):
    def good_patch(self):
        return "Retrying denied commands wastes attempts (2 of 5 lessons).\n\n" + self.make_patch({
            "worker_preamble.md": EXISTING_FILES["worker_preamble.md"] + "- Report a denied command; do not retry it.\n",
        })

    def test_a_passing_patch_becomes_a_pull_request(self):
        proc = self.open_pr(self.good_patch())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("https://github.com/example/queue/pull/123", proc.stdout)
        self.assertEqual(self.remote_branches(), ["proposal/p1"])
        changed = self.run_git("diff", "--name-only", "main", "proposal/p1", git_dir=self.origin).split()
        self.assertEqual(changed, ["worker_preamble.md"])
        (call,) = self.gh_calls()
        argv = call["argv"]
        self.assertEqual(argv[:2], ["pr", "create"])
        self.assertEqual(argv[argv.index("--head") + 1], "proposal/p1")
        self.assertEqual(argv[argv.index("--base") + 1], "main")
        self.assertEqual(self.proposals("opened"), ["p1.patch"])
        self.assertIn("pull/123", (self.repo / "tasks" / "proposals" / "opened" / "p1.url").read_text())
        self.assertFalse((self.repo / "tasks" / "proposals" / "p1.patch").exists())
        self.assert_clean()

    def test_the_pr_body_carries_the_rationale(self):
        self.open_pr(self.good_patch())
        argv = self.gh_calls()[0]["argv"]
        self.assertIn("--title", argv)
        self.assertIn("--body-file", argv)

    def test_the_tests_run_on_the_patched_tree(self):
        proc = self.open_pr(self.good_patch(), test_cmd="grep -q 'do not retry it' worker_preamble.md")
        self.assertEqual(len(self.gh_calls()), 1, proc.stderr + proc.stdout)

    def test_a_new_test_file_is_allowed(self):
        patch = self.make_patch({"tests/test_denied.py": "def test_denied():\n    assert True\n"})
        self.open_pr(patch)
        self.assertEqual(len(self.gh_calls()), 1)

    def test_a_new_skill_file_is_allowed(self):
        patch = self.make_patch({".claude/skills/file-issue/SKILL.md": "# File an issue\n"})
        self.open_pr(patch)
        self.assertEqual(len(self.gh_calls()), 1)

    def test_coordinator_instructions_are_allowed(self):
        patch = self.make_patch({"coordinator/CLAUDE.md.template": "# Coordinator\n- Ask for a verifiable criterion.\n"})
        self.open_pr(patch)
        self.assertEqual(len(self.gh_calls()), 1)


class OpenerRejectTests(OpenerCase):
    def test_failing_tests_mean_no_pull_request(self):
        patch = self.make_patch({"worker_preamble.md": EXISTING_FILES["worker_preamble.md"] + "- More.\n"})
        proc = self.open_pr(patch, test_cmd="false")
        self.assert_rejected(proc, "tests failed")

    def test_dispatcher_py_is_off_limits(self):
        proc = self.open_pr(self.make_patch({"dispatcher.py": "# dispatcher\nimport os\n"}))
        self.assert_rejected(proc, "dispatcher.py")

    def test_a_patch_that_also_touches_dispatcher_py_is_rejected_whole(self):
        patch = self.make_patch({
            "worker_preamble.md": EXISTING_FILES["worker_preamble.md"] + "- More.\n",
            "dispatcher.py": "# dispatcher\nimport os\n",
        })
        self.assert_rejected(self.open_pr(patch), "dispatcher.py")

    def test_ci_and_codeowners_are_off_limits(self):
        proc = self.open_pr(self.make_patch({".github/CODEOWNERS": "* @someone\n"}))
        self.assert_rejected(proc, ".github/CODEOWNERS")

    def test_the_rendered_coordinator_file_is_not_a_target(self):
        proc = self.open_pr(self.make_patch({"coordinator/CLAUDE.md": "# Rendered\n"}))
        self.assert_rejected(proc, "coordinator/CLAUDE.md")

    def test_the_opener_cannot_edit_itself(self):
        proc = self.open_pr(self.make_patch({"scripts/open_proposal_pr.py": "print('hi')\n"}))
        self.assert_rejected(proc, "scripts/open_proposal_pr.py")

    def test_existing_tests_cannot_be_edited(self):
        proc = self.open_pr(self.make_patch({"tests/test_existing.py": "def test_ok():\n    assert True\n"}))
        self.assert_rejected(proc, "tests/test_existing.py")

    def test_only_test_files_may_be_added_under_tests(self):
        proc = self.open_pr(self.make_patch({"tests/helper.py": "X = 1\n"}))
        self.assert_rejected(proc, "tests/helper.py")

    def test_deleting_an_allowed_file_is_rejected(self):
        proc = self.open_pr(self.make_patch({}, delete=["coordinator/CLAUDE.md.template"]))
        self.assert_rejected(proc, "coordinator/CLAUDE.md.template")

    def test_a_malformed_patch_is_rejected(self):
        self.assert_rejected(self.open_pr("this is not a patch\n"))

    def test_a_patch_that_does_not_apply_is_rejected(self):
        patch = self.make_patch({"worker_preamble.md": EXISTING_FILES["worker_preamble.md"] + "- More.\n"})
        self.assert_rejected(self.open_pr(patch.replace("Work in", "Walk in")), "apply")

    def test_a_gh_failure_takes_the_pushed_branch_down(self):
        patch = self.make_patch({"worker_preamble.md": EXISTING_FILES["worker_preamble.md"] + "- More.\n"})
        proc = self.open_pr(patch, env={"FAKE_GH_FAIL": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.remote_branches(), [])
        self.assertEqual(self.proposals("rejected"), ["p1.patch"])
        self.assert_clean()

    def test_a_missing_remote_rejects_instead_of_crashing(self):
        self.run_git("remote", "remove", "origin")
        patch = self.make_patch({"worker_preamble.md": EXISTING_FILES["worker_preamble.md"] + "- More.\n"})
        proc = self.open_pr(patch)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.gh_calls(), [])
        self.assertEqual(self.proposals("rejected"), ["p1.patch"])


if __name__ == "__main__":
    unittest.main()
