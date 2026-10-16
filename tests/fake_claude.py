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
    {"stream": [item, ...]}                with --output-format stream-json, print these events
                                           between the init line and the result line (see below)
    {"write": {"path": "p", "content": "text"}}
                                           first write that file (path relative to
                                           the call's cwd, parents created), then
                                           act like the other keys say; combine
                                           with "result" for the reply text. A list of
                                           such dicts writes several files

Called with `--output-format stream-json`, a success or error step prints JSON
lines as the real CLI does (tests/fixtures/stream-sample.jsonl is a real run):
a system/init line, the events of "stream", then the envelope as the final
`result` line. A stream item is one of:
    {"tool": "Edit", "input": {"file_path": "a.py"}, "tokens": 5}   an assistant tool_use
    {"text": "words"}                      an assistant text block
    {"sleep": 0.3}                         wait this many seconds (output is flushed per line)
    {"event": {...}}                       any other event, printed as is
Steps with "stdout" or "fixture" still print exactly that, in either format.

Output is the envelope from tests/fixtures/envelope-success.json unless
"stdout" or "fixture" is given. Every call is appended to
<scenario>.calls.jsonl (role, model, allowed_tools, argv, the login/GitHub tokens it
was given in its environment, prompt, exit) so
tests can count and inspect worker and review calls.
"""

import json
import os
import sys
import time
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


def emit(event):
    print(json.dumps(event), flush=True)


def emit_stream(items, envelope):
    session = envelope.get("session_id", "fake-session")
    emit({"type": "system", "subtype": "init", "session_id": session, "model": "fake", "tools": ["Read"]})
    for item in items:
        if "sleep" in item:
            time.sleep(item["sleep"])
        elif "event" in item:
            emit(item["event"])
        else:
            block = (
                {"type": "tool_use", "id": "toolu_fake", "name": item["tool"], "input": item.get("input", {})}
                if "tool" in item else {"type": "text", "text": item.get("text", "")}
            )
            emit({"type": "assistant", "session_id": session, "parent_tool_use_id": None,
                  "message": {"role": "assistant", "content": [block],
                              "usage": {"input_tokens": 3, "output_tokens": item.get("tokens", 1)}}})
    emit({**envelope, "type": "result"})


def main(argv):
    if argv[:1] == ["--version"]:
        print("2.0.0 (Claude Code)")
        return 0
    if argv[:1] == ["setup-token"]:  # the real one prints a token and does not save it
        print("Open the URL to log in...\nYour OAuth token (valid for 1 year):\n"
              "sk-ant-oat01-FAKEFAKEFAKEFAKEFAKE0123456789\nStore this token securely.")
        return 0
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
            "oauth_token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
            "gh_token": os.environ.get("GH_TOKEN"),
            "prompt": prompt,
            "exit": code,
        }) + "\n")

    writes = step.get("write") or []
    for write in [writes] if isinstance(writes, dict) else writes:
        target = Path(write["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(write["content"])

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
        if flag_value(argv, "--output-format") == "stream-json":
            emit_stream(step.get("stream", []), envelope)
        else:
            print(json.dumps(envelope))
    if step.get("stderr"):
        sys.stderr.write(step["stderr"])
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
