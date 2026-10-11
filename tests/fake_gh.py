#!/usr/bin/env python3
"""Stand-in for the `gh` CLI (stdlib only).

Appends {"argv", "cwd"} to the file named by $FAKE_GH_LOG, then prints a pull
request URL. With $FAKE_GH_FAIL set it prints an error and exits 1 instead.
"""

import json
import os
import sys

log = os.environ.get("FAKE_GH_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
if os.environ.get("FAKE_GH_FAIL"):
    sys.stderr.write("gh: not logged in\n")
    sys.exit(1)
print("https://github.com/example/queue/pull/123")
