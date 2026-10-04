#!/usr/bin/env python3
"""Stand-in for the `claude` CLI, used as CLAUDE_BIN in tests (stdlib only).

Behaviour comes from the JSON scenario file named by $FAKE_CLAUDE_SCENARIO:

    {"worker": [step, ...], "review": [step, ...], "other": [step, ...]}

The role of a call is read from its prompt: the dispatcher's worker preamble
means "worker", its reviewer template means "review", anything else is "other".
Each role has its own list of steps, consumed one per call; the last step
repeats once the list runs out. A role with no list gets a default success
(worker "Done.", review "VERDICT: PASS").

A step is a dict:
    {"result": "text"}                     success envelope with that result
    {"envelope": {...}}                    ...with these envelope fields overridden
    {"exit": 1, "stdout": "", "stderr": "message"}
                                           exit non-zero printing exactly this
    {"fixture": "name.txt"}                print tests/fixtures/<name> verbatim
                                           (combine with "exit")

Output is the envelope from tests/fixtures/envelope-success.json unless
"stdout" or "fixture" is given. Every call is appended to
<scenario>.calls.jsonl (role, model, allowed_tools, argv, prompt, exit) so
tests can count and inspect worker and review calls.
"""

import json
import os
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DEFAULTS = {
    "worker": {"result": "Done."},
    "review": {"result": "VERDICT: PASS"},
    "other": {"result": "ok"},
}


def role_of(prompt):
    if "strict automated reviewer" in prompt:
        return "review"
    if prompt.startswith("You are running unattended"):
        return "worker"
    return "other"


def flag_value(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv[:-1] else None


def main(argv):
    scenario_env = os.environ.get("FAKE_CLAUDE_SCENARIO")
    if not scenario_env:
        print("fake_claude: FAKE_CLAUDE_SCENARIO is not set", file=sys.stderr)
        return 99
    scenario = json.loads(Path(scenario_env).read_text())
    calls_path = Path(f"{scenario_env}.calls.jsonl")

    prompt = sys.stdin.read()
    role = role_of(prompt)
    previous = calls_path.read_text().splitlines() if calls_path.exists() else []
    index = sum(1 for line in previous if json.loads(line)["role"] == role)
    steps = scenario.get(role) or [DEFAULTS[role]]
    step = steps[min(index, len(steps) - 1)]
    code = int(step.get("exit", 0))

    with open(calls_path, "a") as fh:
        fh.write(json.dumps({
            "role": role,
            "model": flag_value(argv, "--model"),
            "allowed_tools": flag_value(argv, "--allowedTools"),
            "argv": argv,
            "prompt": prompt,
            "exit": code,
        }) + "\n")

    if "fixture" in step:
        sys.stdout.write((FIXTURES / step["fixture"]).read_text())
    elif "stdout" in step:
        sys.stdout.write(step["stdout"])
    else:
        envelope = json.loads((FIXTURES / "envelope-success.json").read_text())
        envelope["is_error"] = code != 0
        envelope.update(step.get("envelope", {}))
        if "result" in step:
            envelope["result"] = step["result"]
        print(json.dumps(envelope))
    if step.get("stderr"):
        sys.stderr.write(step["stderr"])
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
