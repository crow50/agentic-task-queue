#!/usr/bin/env python3
"""Check, turn on and turn off the learning loop (README: "Learning loop"). Stdlib only.

    enable_learning_loop.py status         checklist of what the loop needs; exit 1 if anything is missing
    enable_learning_loop.py install        status, then install tasks/recurring/weekly-improver.md
    enable_learning_loop.py uninstall      remove the weekly task
    enable_learning_loop.py login          log the queue user in to gh, with a token read from stdin
    enable_learning_loop.py github-setup   REPO ADMIN ONLY: protect main and give the queue's deploy key write access

Run everything but github-setup as the queue user, from the venv, in the checkout:
    sudo -u taskq .venv/bin/python scripts/enable_learning_loop.py status

github-setup uses the caller's own gh login (an admin of the repo), lists what it will
change and asks first (--yes skips the question). It is the only command that writes to
GitHub, and it never runs on its own: the weekly task cannot reach it.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
TEMPLATE = Path("tasks/examples/weekly-improver.md")
INSTALLED = Path("tasks/recurring/weekly-improver.md")
RULESET = "protect-main"
# Must match the job names in .github/workflows/ci.yml (tests/test_enable_loop.py checks that).
CI_CHECKS = ["Lint (ruff)", "Secret scan (gitleaks)", "Tests (Python 3.11)", "Tests (Python 3.14)"]
ADMIN_ROLE_ID = 5  # GitHub's id for the built-in "admin" repository role
REQUIRED = ("push access", "gh login", "main protected", "gitleaks")


class Failure(Exception):
    pass


def run(cmd, cwd=None, stdin=None, timeout=60):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND=os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes"))
    try:
        return subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: {exc}")


class Context:
    def __init__(self, args):
        self.base = Path(args.base).resolve()
        self.remote = args.remote
        self.branch = args.branch
        self.gh = shlex.split(args.gh)

    def git(self, *args):
        return run(["git", *args], cwd=self.base)

    def gh_run(self, *args, stdin=None):
        return run([*self.gh, *args], cwd=self.base, stdin=stdin)

    def slug(self):
        url = self.git("remote", "get-url", self.remote).stdout.strip()
        match = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$", url)
        if not match:
            raise Failure(f"cannot read an owner/repo from the {self.remote} remote ({url or 'none'})")
        return match.group(1)


# ---------------------------------------------------------------- the checklist


def check_push(ctx):
    probe = ctx.git("push", "--dry-run", ctx.remote, "HEAD:refs/heads/proposal/_probe")
    if probe.returncode == 0:
        return True, "a dry-run push of a proposal/ branch is accepted", ""
    reason = (probe.stderr.strip().splitlines() or ["push refused"])[0]
    return False, reason, (
        "give the queue user's deploy key write access: as a repo admin run\n"
        "      sudo cat ~taskq/.ssh/id_ed25519.pub | .venv/bin/python scripts/enable_learning_loop.py github-setup --pubkey-file -"
    )


def check_gh(ctx):
    if ctx.gh_run("auth", "status").returncode == 0:
        return True, "gh is logged in", ""
    return False, "gh is not logged in", (
        "make a fine-grained token for this repo (Pull requests: write, Contents: read), then\n"
        "      read -rs T && printf %s \"$T\" | sudo -u taskq .venv/bin/python scripts/enable_learning_loop.py login"
    )


def check_protected(ctx):
    try:
        slug = ctx.slug()
    except Failure as exc:
        return False, str(exc), ""
    answer = ctx.gh_run("api", f"repos/{slug}/branches/{ctx.branch}", "--jq", ".protected")
    if answer.returncode != 0:
        return False, f"cannot ask GitHub ({(answer.stderr.strip().splitlines() or ['gh failed'])[0]})", "log in first (see gh login)"
    if answer.stdout.strip() == "true":
        return True, f"{ctx.branch} has a ruleset or protection", ""
    return False, f"{ctx.branch} accepts direct pushes", (
        "as a repo admin run .venv/bin/python scripts/enable_learning_loop.py github-setup"
    )


def check_gitleaks(ctx):
    found = shutil.which("gitleaks")
    if found:
        return True, found, ""
    return False, "not on PATH, so the pre-commit hook would reject every proposal", (
        "sudo install -m 755 /path/to/gitleaks /usr/local/bin/gitleaks"
    )


def check_task(ctx):
    if (ctx.base / INSTALLED).is_file():
        return True, f"{INSTALLED} is installed", ""
    return False, "not installed yet", "scripts/enable_learning_loop.py install"


CHECKS = [
    ("push access", check_push), ("gh login", check_gh), ("main protected", check_protected),
    ("gitleaks", check_gitleaks), ("weekly task", check_task),
]


def checklist(ctx, only=None):
    """Print one line per check; return {name: ok}."""
    results = {}
    for name, check in CHECKS:
        if only and name not in only:
            continue
        ok, detail, fix = check(ctx)
        results[name] = ok
        print(f"{'✓' if ok else ('✗' if name in REQUIRED else '·')} {name}: {detail}")
        if not ok and fix:
            print(f"    fix: {fix}")
    return results


# ---------------------------------------------------------------- commands


def cmd_status(ctx, args):
    results = checklist(ctx)
    missing = [name for name in REQUIRED if not results.get(name)]
    return 1 if missing else 0


def cmd_install(ctx, args):
    destination = ctx.base / INSTALLED
    if destination.exists():
        print(f"already installed: {INSTALLED} (left as it is, last_run included)")
        return 0
    results = checklist(ctx, only=REQUIRED)
    if not all(results.values()) and not args.force:
        print("\nnot installing: fix the lines marked ✗ above (or use --force to install anyway).")
        return 1
    source = (ctx.base / TEMPLATE).read_text()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(source.replace("__QUEUE_DIR__", str(ctx.base)))
    if os.geteuid() == 0:  # keep the file the queue user's
        owner = ctx.base.stat()
        os.chown(destination, owner.st_uid, owner.st_gid)
    check = run([sys.executable, str(ctx.base / "dispatcher.py"), "check", str(destination)], cwd=ctx.base)
    if check.returncode != 0:
        destination.unlink()
        print("the dispatcher rejects the template, so it was not installed:\n" + (check.stdout + check.stderr).strip())
        return 1
    print(f"installed {INSTALLED}: it runs on the next dispatcher cycle, then every Monday at 06:00 (server time)")
    return 0


def cmd_uninstall(ctx, args):
    destination = ctx.base / INSTALLED
    if destination.exists():
        destination.unlink()
        print(f"removed {INSTALLED}")
    else:
        print("not installed")
    return 0


def cmd_login(ctx, args):
    token = sys.stdin.read().strip()
    if not token:
        print("no token on stdin: printf %s \"$TOKEN\" | scripts/enable_learning_loop.py login", file=sys.stderr)
        return 1
    result = ctx.gh_run("auth", "login", "--with-token", stdin=token + "\n")
    if result.returncode != 0:
        print("gh refused the token: " + (result.stderr.strip().replace(token, "***") or "no reason given"), file=sys.stderr)
        return 1
    print("gh is logged in")
    return 0


def ruleset_body():
    return {
        "name": RULESET,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "bypass_actors": [{"actor_id": ADMIN_ROLE_ID, "actor_type": "RepositoryRole", "bypass_mode": "always"}],
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "pull_request", "parameters": {
                "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": False,
                "require_code_owner_review": False, "require_last_push_approval": False,
                "required_review_thread_resolution": False}},
            {"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": name} for name in CI_CHECKS]}},
        ],
    }


def cmd_github_setup(ctx, args):
    slug = ctx.slug()
    rulesets = ctx.gh_run("api", f"repos/{slug}/rulesets", "--jq", ".[].name")
    if rulesets.returncode != 0:
        print("cannot list rulesets (are you logged in as a repo admin?): " + rulesets.stderr.strip(), file=sys.stderr)
        return 1
    need_ruleset = RULESET not in rulesets.stdout.split()

    key = None
    existing = None
    if args.pubkey_file:
        text = sys.stdin.read() if args.pubkey_file == "-" else Path(args.pubkey_file).read_text()
        parts = text.split()
        if len(parts) < 2:
            print("that is not an ssh public key", file=sys.stderr)
            return 1
        key = " ".join(parts[:2])
        listing = ctx.gh_run("api", f"repos/{slug}/keys", "--jq", '.[] | [.id, .title, .key, .read_only] | @tsv')
        if listing.returncode != 0:
            print("cannot list deploy keys: " + listing.stderr.strip(), file=sys.stderr)
            return 1
        for line in listing.stdout.splitlines():
            key_id, title, material, read_only = (line.split("\t") + ["", "", "", ""])[:4]
            if material.split()[:2] == parts[:2]:
                existing = (key_id, title, read_only == "true")
    need_key = key is not None and (existing is None or existing[2])

    plan = []
    plan.append(f"create ruleset {RULESET} on {slug}: no deletion or force-push of the default branch, a pull request and "
                f"these checks required ({', '.join(CI_CHECKS)}); repo admins may bypass" if need_ruleset
                else f"ruleset {RULESET} already exists: left as it is")
    if key is not None:
        plan.append(
            (f"replace deploy key '{existing[1]}' (read-only) with the same key allowed to push" if existing else
             "add this key as a deploy key allowed to push") if need_key else "deploy key can already push: left as it is")
    print("github-setup on " + slug + ":\n" + "".join(f"  - {line}\n" for line in plan))
    if not (need_ruleset or need_key):
        return 0
    if not args.yes and input("Type yes to apply: ").strip().lower() != "yes":
        print("nothing changed")
        return 1

    if need_ruleset:
        created = ctx.gh_run("api", "-X", "POST", f"repos/{slug}/rulesets", "--input", "-", stdin=json.dumps(ruleset_body()))
        if created.returncode != 0:
            print("could not create the ruleset (the deploy key was not touched): " + created.stderr.strip(), file=sys.stderr)
            return 1
        print(f"created ruleset {RULESET}")
    if need_key:
        title = existing[1] if existing else "agentic-task-queue"
        if existing:
            gone = ctx.gh_run("api", "-X", "DELETE", f"repos/{slug}/keys/{existing[0]}")
            if gone.returncode != 0:
                print("could not remove the read-only key: " + gone.stderr.strip(), file=sys.stderr)
                return 1
        added = ctx.gh_run("api", "-X", "POST", f"repos/{slug}/keys", "--input", "-",
                           stdin=json.dumps({"title": title, "key": key, "read_only": False}))
        if added.returncode != 0:
            print("the old key was removed but the writable one was not added; add it by hand: " + added.stderr.strip(), file=sys.stderr)
            return 1
        print(f"deploy key '{title}' can now push")
    return 0


COMMANDS = {
    "status": cmd_status, "install": cmd_install, "uninstall": cmd_uninstall,
    "login": cmd_login, "github-setup": cmd_github_setup,
}


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--base", default=str(BASE), help="the queue checkout (default: this one)")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--gh", default="gh")
    parser.add_argument("--force", action="store_true", help="install: skip the prerequisite checks")
    parser.add_argument("--yes", action="store_true", help="github-setup: do not ask first")
    parser.add_argument("--pubkey-file", help="github-setup: the queue user's ssh public key ('-' = stdin)")
    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.command](Context(args), args)
    except Failure as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
