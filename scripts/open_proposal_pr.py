#!/usr/bin/env python3
"""Check a proposed patch and open a pull request for it only if it is safe and green.

    open_proposal_pr.py tasks/proposals/<name>.patch [--base main] [--remote origin]
                        [--test-cmd CMD] [--gh gh]

Run from the queue's root, which must be a git checkout with a remote. The weekly
improver (tasks/examples/weekly-improver.md) writes the patch; this script, not
the model, decides what happens to it:

1. Every file it touches must be one the improver may change: worker_preamble.md,
   coordinator/CLAUDE.md.template, models.json (the model table; the tests check it is valid), a *new* tests/test_*.py, or a file under .claude/skills/.
   dispatcher.py, tests that already exist, .github/, scripts/ and everything else
   are refused, and so are deletes, renames, mode changes and binary patches.
2. The patch is applied in a temporary git worktree cut from <remote>/<base>, so
   the running checkout is never touched, and CMD (default: unittest
   discover with the checkout's .venv python) must pass there.
3. Only then is the branch pushed and `gh pr create` called.

A rejected patch moves to tasks/proposals/rejected/ next to a .reason file; an
opened one moves to tasks/proposals/opened/ next to a .url file. Either way the
script prints one line and exits 0, because a refused proposal is not an error
for the queue. Stdlib only.
"""

import argparse
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EDITABLE = {"worker_preamble.md", "coordinator/CLAUDE.md.template", "models.json"}
SKILLS_PREFIX = ".claude/skills/"
NEW_TEST = re.compile(r"tests/test_[A-Za-z0-9_]+\.py")
TEST_TIMEOUT_S = 1800
IDENTITY = ["-c", "user.name=Queue improver", "-c", "user.email=improver@localhost"]
TITLE_MAX = 72


class Rejected(Exception):
    """The proposal is refused; the message is the reason that gets recorded."""


def run(cmd, cwd, timeout=300):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Rejected(f"could not run {cmd[0]}: {exc}") from exc


def git(cwd, *args):
    return run(["git", *IDENTITY, *args], cwd)


def need(proc, what):
    if proc.returncode != 0:
        raise Rejected(f"{what}: {(proc.stderr or proc.stdout).strip()[-500:]}")
    return proc.stdout


def tail(text, lines=20):
    return "\n".join(text.strip().splitlines()[-lines:])


def check_paths(repo, patch):
    """Refuse the patch unless every file it touches is one the improver may change."""
    numstat = git(repo, "apply", "--numstat", str(patch))
    if numstat.returncode != 0:
        raise Rejected(f"not a valid patch: {numstat.stderr.strip()[-300:]}")
    summary = need(git(repo, "apply", "--summary", str(patch)), "cannot summarise patch")

    created = set()
    for line in summary.splitlines():
        line = line.strip()
        match = re.fullmatch(r"create mode (\d+) (.+)", line)
        if match and match.group(1) == "100644":
            created.add(match.group(2))
        else:  # deletes, renames, copies, mode changes, symlinks and executables
            raise Rejected(f"only plain edits and new files are allowed: {line}")

    paths = []
    for line in numstat.stdout.splitlines():
        added, deleted, path = line.split("\t", 2)
        if added == "-" or deleted == "-":
            raise Rejected(f"binary change to {path}")
        if '"' in path or " => " in path:
            raise Rejected(f"unusual path in patch: {path}")
        paths.append(path)
    if not paths:
        raise Rejected("the patch changes no files")

    for path in paths:
        editable = path in EDITABLE
        skill = path.startswith(SKILLS_PREFIX) and ".." not in Path(path).parts
        new_test = bool(NEW_TEST.fullmatch(path)) and path in created
        if not (editable or skill or new_test):
            raise Rejected(f"{path} is not a path the improver may change")


def rationale(patch):
    """The text before the first diff line: why the change is proposed."""
    kept = []
    for line in patch.read_text().splitlines():
        if line.startswith(("diff --git", "--- ", "Index: ")):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def open_pr(args, repo, patch):
    stem = re.sub(r"[^A-Za-z0-9._-]", "-", patch.stem)
    branch = f"proposal/{stem}"
    base_ref = f"{args.remote}/{args.base}"
    text = rationale(patch)
    title = (text.splitlines()[0] if text else f"Proposal {stem}")[:TITLE_MAX]

    git(repo, "worktree", "prune")
    leftover = git(repo, "branch", "--list", branch).stdout.strip()
    if leftover:  # a branch of ours from an interrupted run; nothing is checked out on it any more
        git(repo, "branch", "-D", branch)

    tmp = Path(tempfile.mkdtemp(prefix="proposal-"))
    worktree = tmp / "wt"
    created_branch = pushed = False
    try:
        need(git(repo, "fetch", "--quiet", args.remote), f"cannot fetch {args.remote}")
        need(git(repo, "worktree", "add", "-q", "-b", branch, str(worktree), base_ref), "cannot create worktree")
        created_branch = True
        need(git(worktree, "apply", "--check", str(patch)), "patch does not apply to " + base_ref)
        need(git(worktree, "apply", str(patch)), "patch does not apply to " + base_ref)

        tests = run(shlex.split(args.test_cmd), worktree, timeout=TEST_TIMEOUT_S)
        if tests.returncode != 0:
            output = tail(tests.stdout + "\n" + tests.stderr)
            raise Rejected(f"tests failed ({output.splitlines()[-1] if output else 'no output'}):\n{output}")

        need(git(worktree, "add", "-A"), "cannot stage patch")
        need(git(worktree, "commit", "-q", "-m", f"{title}\n\n{text}\n\nProposed by the weekly improver from logs/lessons.jsonl."),
             "cannot commit")
        need(git(worktree, "push", "-q", "-u", args.remote, branch), "cannot push")
        pushed = True

        body = tmp / "body.md"
        body.write_text(
            f"{text}\n\n---\nProposed by the weekly improver from `logs/lessons.jsonl`. The tests passed on "
            f"the patched tree before this was opened. Review it like any change; nothing was applied to the running queue.\n"
        )
        gh = run(
            [*shlex.split(args.gh), "pr", "create", "--base", args.base, "--head", branch,
             "--title", title, "--body-file", str(body)],
            worktree,
        )
        need(gh, "gh pr create failed")
        return next((ln for ln in reversed(gh.stdout.splitlines()) if ln.strip()), "").strip()
    except BaseException:
        if pushed:
            git(repo, "push", "-q", args.remote, "--delete", branch)
        raise
    finally:
        git(repo, "worktree", "remove", "--force", str(worktree))
        if created_branch:
            git(repo, "branch", "-D", branch)
        shutil.rmtree(tmp, ignore_errors=True)


def file_away(patch, folder, note_suffix, note):
    target = patch.parent / folder
    target.mkdir(exist_ok=True)
    shutil.move(str(patch), str(target / patch.name))
    (target / f"{patch.stem}{note_suffix}").write_text(note.strip() + "\n")


def default_test_cmd(repo):
    """Run the suite with the checkout's own venv when it has one: the tests need its libraries."""
    for name in ("python3", "python"):
        venv_python = repo / ".venv" / "bin" / name
        if venv_python.exists():
            return f"{shlex.quote(str(venv_python))} -m unittest discover -s tests"
    return f"{shlex.quote(sys.executable)} -m unittest discover -s tests"


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("patch")
    parser.add_argument("--base", default="main")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--test-cmd", help="default: unittest discover with the checkout's .venv python")
    parser.add_argument("--gh", default="gh")
    args = parser.parse_args(argv)

    patch = Path(args.patch).resolve()
    if not patch.is_file():
        print(f"no such patch: {args.patch}", file=sys.stderr)
        return 2
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        print("not inside a git checkout", file=sys.stderr)
        return 2
    repo = Path(top.stdout.strip())
    args.test_cmd = args.test_cmd or default_test_cmd(repo)

    try:
        check_paths(repo, patch)
        url = open_pr(args, repo, patch)
    except Rejected as exc:
        file_away(patch, "rejected", ".reason", str(exc))
        print(f"rejected {patch.name}: {str(exc).splitlines()[0]}")
        return 0
    except Exception as exc:  # whatever went wrong, the queue must not see an error
        file_away(patch, "rejected", ".reason", f"unexpected {type(exc).__name__}: {exc}")
        print(f"rejected {patch.name}: unexpected {type(exc).__name__}")
        return 0
    file_away(patch, "opened", ".url", url)
    print(f"opened {patch.name}: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
