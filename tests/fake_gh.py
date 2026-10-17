#!/usr/bin/env python3
"""Stand-in for the `gh` CLI (stdlib only).

Appends {"argv", "cwd", "stdin"} to the file named by $FAKE_GH_LOG, then answers.
By default it prints a pull request URL; with $FAKE_GH_FAIL set it prints an
error and exits 1 instead.

$FAKE_GH_SCRIPT names a JSON list of {"match": "text", "stdout": "...", "exit": 0,
"stderr": "..."} rules. The first rule whose "match" is a substring of the
space-joined arguments decides the answer; with no rule matching, the defaults
above apply.

It remembers the pull requests it opened (a `pr create` that succeeded), in
$FAKE_GH_LOG.prs, so a default `pr view <url or branch> --json ...` answers as the real
gh does: the JSON of that PR (open, a draft if it was created with --draft, with its
head branch), or "no pull requests found" and exit 1 for one it never opened.
"""

import json
import os
import sys

stdin = sys.stdin.read() if "--input" in sys.argv or "--with-token" in sys.argv else ""
log = os.environ.get("FAKE_GH_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": stdin}) + "\n")

URL = "https://github.com/example/queue/pull/123"
args = sys.argv[1:]
joined = " ".join(args)


def flag(name):
    return args[args.index(name) + 1] if name in args[:-1] else None


def answer():
    script = os.environ.get("FAKE_GH_SCRIPT")
    if script:
        for rule in json.load(open(script)):
            if rule["match"] in joined:
                return rule.get("stdout", ""), rule.get("stderr", ""), rule.get("exit", 0)
    if os.environ.get("FAKE_GH_FAIL"):
        return "", "gh: not logged in\n", 1
    if args[:2] == ["pr", "view"]:
        known = [json.loads(line) for line in open(log + ".prs")] if log and os.path.exists(log + ".prs") else []
        target = args[2] if len(args) > 2 and not args[2].startswith("-") else None
        for pr in reversed(known):
            if target in (pr["url"], pr["headRefName"]):
                return json.dumps(pr) + "\n", "", 0
        return "", "no pull requests found\n", 1
    return URL + "\n", "", 0


stdout, stderr, code = answer()
if args[:2] == ["pr", "create"] and code == 0 and log:
    pr = {"url": stdout.strip().splitlines()[-1] if stdout.strip() else URL, "state": "OPEN",
          "isDraft": "--draft" in args, "headRefName": flag("--head") or ""}
    with open(log + ".prs", "a") as fh:
        fh.write(json.dumps(pr) + "\n")
sys.stdout.write(stdout)
sys.stderr.write(stderr)
sys.exit(code)
