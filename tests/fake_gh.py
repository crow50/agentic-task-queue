#!/usr/bin/env python3
"""Stand-in for the `gh` CLI (stdlib only).

Appends {"argv", "cwd", "stdin"} to the file named by $FAKE_GH_LOG, then answers.
By default it prints a pull request URL; with $FAKE_GH_FAIL set it prints an
error and exits 1 instead.

$FAKE_GH_SCRIPT names a JSON list of {"match": "text", "stdout": "...", "exit": 0,
"stderr": "..."} rules. The first rule whose "match" is a substring of the
space-joined arguments decides the answer; with no rule matching, the defaults
above apply.
"""

import json
import os
import sys

stdin = sys.stdin.read() if "--input" in sys.argv or "--with-token" in sys.argv else ""
log = os.environ.get("FAKE_GH_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": stdin}) + "\n")

script = os.environ.get("FAKE_GH_SCRIPT")
if script:
    joined = " ".join(sys.argv[1:])
    for rule in json.load(open(script)):
        if rule["match"] in joined:
            sys.stdout.write(rule.get("stdout", ""))
            sys.stderr.write(rule.get("stderr", ""))
            sys.exit(rule.get("exit", 0))

if os.environ.get("FAKE_GH_FAIL"):
    sys.stderr.write("gh: not logged in\n")
    sys.exit(1)
print("https://github.com/example/queue/pull/123")
