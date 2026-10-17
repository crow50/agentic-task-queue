"""scripts/enable_learning_loop.py: one command to check, turn on and turn off the learning loop.

Written before the code. Each test runs the real script against a throwaway
git repo (a copy of the files it needs, a bare repo as "origin") with
tests/fake_gh.py standing in for `gh`; the fake answers from a scripted rule list.
"""

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_baseline import REPO, TESTS

SCRIPT = REPO / "scripts" / "enable_learning_loop.py"
FAKE_GH = TESTS / "fake_gh.py"
SLUG = "example/queue"
COPIED = ("dispatcher.py", "worker_preamble.md", "scripts/enable_learning_loop.py",
          "tasks/examples/weekly-improver.md", "ruff.toml")
CI_CHECKS = ["Lint (ruff)", "Secret scan (gitleaks)", "Tests (Python 3.11)", "Tests (Python 3.14)"]

GOOD = [
    {"match": "auth status", "stdout": "Logged in to github.com account queue-bot\n"},
    {"match": f"api repos/{SLUG}/branches/main", "stdout": "true\n"},
]


class LoopCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="taskq-enable-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.origin = self.root / "origin.git"
        self.repo = self.root / "queue"
        self.gh_log = self.root / "gh.jsonl"
        self.rules_path = self.root / "rules.json"
        (self.root / "home").mkdir()
        self.repo.mkdir()
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.root)
        self.git("init", "-q", "-b", "main")
        for rel in COPIED:
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, target)
        shutil.copytree(REPO / "taskq_lib", self.repo / "taskq_lib", ignore=shutil.ignore_patterns("__pycache__"))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.git("remote", "add", "origin", f"git@github.com:{SLUG}.git")  # what status parses
        self.git("remote", "set-url", "--push", "origin", str(self.origin))  # where a dry-run push really goes
        self.git("push", "-q", "-u", "origin", "main")
        self.script_rules(GOOD)

    def env(self, **extra):
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(self.root / "home"), "LC_ALL": "C.UTF-8",
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
            "FAKE_GH_LOG": str(self.gh_log), "FAKE_GH_SCRIPT": str(self.rules_path), **extra,
        }

    def git(self, *args, cwd=None):
        proc = subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env(), capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
        return proc.stdout

    def script_rules(self, rules):
        self.rules_path.write_text(json.dumps(rules))

    def run_script(self, *args, stdin="", env=None):
        gh = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_GH))}"
        return subprocess.run(
            [sys.executable, "scripts/enable_learning_loop.py", *args, "--gh", gh],
            cwd=self.repo, env=self.env(**(env or {})), input=stdin, capture_output=True, text=True, timeout=120,
        )

    def gh_calls(self):
        return [json.loads(line) for line in self.gh_log.read_text().splitlines()] if self.gh_log.exists() else []

    def writes(self):
        """The gh calls that change something on GitHub."""
        return [c for c in self.gh_calls() if "-X" in c["argv"] or "--input" in c["argv"]]

    def with_gitleaks(self, present=True):
        """A PATH where gitleaks is there or is not (the host's own /usr/local/bin is left out on purpose)."""
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        tool = bin_dir / "gitleaks"
        if present:
            tool.write_text("#!/bin/sh\nexit 0\n")
            tool.chmod(0o755)
        return {"PATH": f"{bin_dir}:/usr/bin:/bin"}

    @property
    def template(self):
        return self.repo / "tasks" / "recurring" / "weekly-improver.md"


class StatusTests(LoopCase):
    def test_everything_in_place_exits_zero(self):
        proc = self.run_script("status", env=self.with_gitleaks())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("✗", proc.stdout)
        for label in ("push access", "gh login", "main protected", "gitleaks", "weekly task"):
            self.assertIn(label, proc.stdout)

    def test_a_remote_that_refuses_pushes_is_reported_with_a_fix(self):
        self.git("remote", "set-url", "--push", "origin", str(self.root / "nowhere.git"))
        proc = self.run_script("status", env=self.with_gitleaks())
        self.assertEqual(proc.returncode, 1)
        self.assertRegex(proc.stdout, r"✗ push access")
        self.assertIn("deploy key", proc.stdout)

    def test_a_logged_out_gh_is_reported(self):
        self.script_rules([{"match": "auth status", "exit": 1, "stderr": "not logged in"}] + GOOD)
        proc = self.run_script("status", env=self.with_gitleaks())
        self.assertEqual(proc.returncode, 1)
        self.assertRegex(proc.stdout, r"✗ gh login")
        self.assertIn("enable_learning_loop.py login", proc.stdout)

    def test_an_unprotected_main_is_reported(self):
        self.script_rules([{"match": f"api repos/{SLUG}/branches/main", "stdout": "false\n"}] + GOOD)
        proc = self.run_script("status", env=self.with_gitleaks())
        self.assertEqual(proc.returncode, 1)
        self.assertRegex(proc.stdout, r"✗ main protected")
        self.assertIn("github-setup", proc.stdout)

    def test_missing_gitleaks_is_reported_because_the_commit_hook_needs_it(self):
        proc = self.run_script("status", env=self.with_gitleaks(present=False))
        self.assertEqual(proc.returncode, 1)
        self.assertRegex(proc.stdout, r"✗ gitleaks")
        self.assertIn("/usr/local/bin/gitleaks", proc.stdout)

    def test_status_never_writes_to_github(self):
        self.run_script("status", env=self.with_gitleaks())
        self.assertEqual(self.writes(), [])


class InstallTests(LoopCase):
    def test_install_renders_the_template_for_this_checkout(self):
        proc = self.run_script("install", env=self.with_gitleaks())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        text = self.template.read_text()
        self.assertNotIn("__QUEUE_DIR__", text)
        self.assertIn(f"cwd: {self.repo}", text)
        self.assertRegex(text, r"(?m)^schedule: weekly on ")

    def test_the_installed_template_passes_the_dispatchers_own_check(self):
        self.run_script("install", env=self.with_gitleaks())
        check = subprocess.run(
            [sys.executable, "dispatcher.py", "check", str(self.template)],
            cwd=self.repo, env=self.env(), capture_output=True, text=True,
        )
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_install_refuses_while_a_prerequisite_is_missing(self):
        self.script_rules([{"match": f"api repos/{SLUG}/branches/main", "stdout": "false\n"}] + GOOD)
        proc = self.run_script("install", env=self.with_gitleaks())
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(self.template.exists())
        self.assertIn("--force", proc.stdout)

    def test_force_installs_anyway(self):
        self.script_rules([{"match": f"api repos/{SLUG}/branches/main", "stdout": "false\n"}] + GOOD)
        proc = self.run_script("install", "--force", env=self.with_gitleaks())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(self.template.exists())

    def test_a_second_install_changes_nothing_not_even_last_run(self):
        self.run_script("install", env=self.with_gitleaks())
        self.template.write_text(self.template.read_text().replace("attempts: 0\n", "attempts: 0\nlast_run: 2026-10-12 06:00\n"))
        before = self.template.read_bytes()
        proc = self.run_script("install", env=self.with_gitleaks())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.template.read_bytes(), before)
        self.assertIn("already installed", proc.stdout)

    def test_uninstall_removes_it_and_is_safe_to_repeat(self):
        self.run_script("install", env=self.with_gitleaks())
        self.assertEqual(self.run_script("uninstall").returncode, 0)
        self.assertFalse(self.template.exists())
        self.assertEqual(self.run_script("uninstall").returncode, 0)


class LoginTests(LoopCase):
    def test_login_hands_the_token_on_stdin_to_gh(self):
        proc = self.run_script("login", stdin="ghp_secret_token\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        (call,) = [c for c in self.gh_calls() if c["argv"][:2] == ["auth", "login"]]
        self.assertIn("--with-token", call["argv"])
        self.assertEqual(call["stdin"].strip(), "ghp_secret_token")
        self.assertNotIn("ghp_secret_token", proc.stdout + proc.stderr)

    def test_login_without_a_token_does_not_call_gh(self):
        proc = self.run_script("login", stdin="")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual([c for c in self.gh_calls() if c["argv"][:2] == ["auth", "login"]], [])


PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEKEYMATERIAL queue@host"


class GithubSetupTests(LoopCase):
    def setUp(self):
        super().setUp()
        self.pubkey = self.root / "queue.pub"
        self.pubkey.write_text(PUBKEY + "\n")

    def setup_rules(self, rulesets="[]", keys="[]"):
        self.script_rules([
            {"match": f"api repos/{SLUG}/rulesets --jq", "stdout": rulesets},
            {"match": f"api repos/{SLUG}/keys --jq", "stdout": keys},
        ])

    def body(self, call):
        return json.loads(call["stdin"])

    def test_it_asks_before_changing_anything(self):
        self.setup_rules()
        proc = self.run_script("github-setup", "--pubkey-file", str(self.pubkey), stdin="no\n")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.writes(), [])
        self.assertIn("protect-main", proc.stdout)

    def test_creates_the_ruleset_with_the_real_ci_checks_and_an_admin_bypass(self):
        self.setup_rules()
        proc = self.run_script("github-setup", "--yes")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        (call,) = self.writes()
        self.assertEqual(call["argv"][:3], ["api", "-X", "POST"])
        self.assertIn(f"repos/{SLUG}/rulesets", call["argv"])
        body = self.body(call)
        self.assertEqual((body["name"], body["enforcement"], body["target"]), ("protect-main", "active", "branch"))
        self.assertEqual(body["conditions"]["ref_name"]["include"], ["~DEFAULT_BRANCH"])
        kinds = {rule["type"]: rule for rule in body["rules"]}
        self.assertEqual(sorted(kinds), ["deletion", "non_fast_forward", "pull_request", "required_status_checks"])
        contexts = [c["context"] for c in kinds["required_status_checks"]["parameters"]["required_status_checks"]]
        self.assertEqual(contexts, CI_CHECKS)
        self.assertEqual(body["bypass_actors"], [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}])
        self.assertFalse(kinds["pull_request"]["parameters"]["require_code_owner_review"])

    def test_the_check_names_match_ci_yml(self):
        ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("name: Lint (ruff)", ci)
        self.assertIn("name: Secret scan (gitleaks)", ci)
        self.assertRegex(ci, r"name: Tests \(Python \$\{\{ matrix\.python-version \}\}\)")
        self.assertIn("python-version: ['3.11', '3.14']", ci.replace('"', "'"))

    def test_an_existing_ruleset_is_left_alone(self):
        self.setup_rules(rulesets="protect-main\n")
        proc = self.run_script("github-setup", "--yes")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("already exists", proc.stdout)

    def test_a_read_only_deploy_key_is_replaced_by_a_write_key(self):
        self.setup_rules(rulesets="protect-main\n", keys="777\tQueue\tssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEKEYMATERIAL\ttrue\n")
        proc = self.run_script("github-setup", "--yes", "--pubkey-file", str(self.pubkey))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        delete, add = self.writes()
        self.assertEqual(delete["argv"][:3], ["api", "-X", "DELETE"])
        self.assertIn(f"repos/{SLUG}/keys/777", delete["argv"])
        self.assertEqual(add["argv"][:3], ["api", "-X", "POST"])
        body = self.body(add)
        self.assertEqual((body["title"], body["read_only"]), ("Queue", False))
        self.assertTrue(body["key"].startswith("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEKEYMATERIAL"))

    def test_a_missing_key_is_added_as_a_write_key(self):
        self.setup_rules(rulesets="protect-main\n")
        self.run_script("github-setup", "--yes", "--pubkey-file", str(self.pubkey))
        (add,) = self.writes()
        self.assertEqual(self.body(add)["read_only"], False)

    def test_a_key_that_can_already_write_is_left_alone(self):
        self.setup_rules(rulesets="protect-main\n", keys="777\tQueue\tssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEKEYMATERIAL\tfalse\n")
        proc = self.run_script("github-setup", "--yes", "--pubkey-file", str(self.pubkey))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.writes(), [])

    def test_the_key_is_never_touched_if_the_ruleset_fails(self):
        self.script_rules([
            {"match": f"api repos/{SLUG}/rulesets --jq", "stdout": ""},
            {"match": "-X POST", "exit": 1, "stderr": "HTTP 403"},
            {"match": f"api repos/{SLUG}/keys --jq", "stdout": "777\tQueue\tssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEKEYMATERIAL\ttrue\n"},
        ])
        proc = self.run_script("github-setup", "--yes", "--pubkey-file", str(self.pubkey))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual([c for c in self.writes() if "keys" in " ".join(c["argv"])], [])

    def test_the_public_key_can_come_from_stdin(self):
        self.setup_rules(rulesets="protect-main\n")
        proc = self.run_script("github-setup", "--yes", "--pubkey-file", "-", stdin=PUBKEY + "\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        (add,) = self.writes()
        self.assertIn("EXAMPLEKEYMATERIAL", self.body(add)["key"])

    def test_a_key_on_stdin_is_confirmed_on_the_terminal(self):
        self.setup_rules(rulesets="protect-main\n")
        tty = self.root / "tty"
        tty.write_text("yes\n")
        proc = self.run_script("github-setup", "--pubkey-file", "-", stdin=PUBKEY + "\n", env={"ENABLE_LOOP_TTY": str(tty)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(self.writes()), 1)

    def test_a_no_on_the_terminal_changes_nothing(self):
        self.setup_rules(rulesets="protect-main\n")
        tty = self.root / "tty"
        tty.write_text("no\n")
        proc = self.run_script("github-setup", "--pubkey-file", "-", stdin=PUBKEY + "\n", env={"ENABLE_LOOP_TTY": str(tty)})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.writes(), [])

    def test_no_terminal_and_no_yes_fails_cleanly(self):
        self.setup_rules(rulesets="protect-main\n")
        proc = self.run_script("github-setup", "--pubkey-file", "-", stdin=PUBKEY + "\n",
                               env={"ENABLE_LOOP_TTY": str(self.root / "missing")})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--yes", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.writes(), [])

    def test_end_of_input_at_the_prompt_fails_cleanly(self):
        proc = self.run_script("github-setup", stdin="")
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Traceback", proc.stderr)


class ReadmeTests(unittest.TestCase):
    def test_the_readme_documents_every_subcommand(self):
        text = (REPO / "README.md").read_text()
        for command in ("status", "install", "login", "github-setup", "uninstall"):
            self.assertRegex(text, rf"enable_learning_loop\.py {command}")


if __name__ == "__main__":
    unittest.main()
