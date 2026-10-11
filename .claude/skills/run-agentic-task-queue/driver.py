#!/usr/bin/env python3
"""Drive the task queue end to end with no real claude, Telegram or cron.

Reads commands from stdin (one per line) and runs them against a throwaway
copy of the queue: dispatcher.py as a real subprocess, tests/fake_claude.py
standing in for `claude`, tests/fake_telegram.py standing in for the Bot API.
It is the repo's own test Sandbox (tests/test_baseline.py) behind a REPL, so
what you see is what the tests see. Run it with the repo's venv python from
the repo root:

    printf 'task t1\nrun\nls\nquit\n' | .venv/bin/python .claude/skills/run-agentic-task-queue/driver.py
    .venv/bin/python .claude/skills/run-agentic-task-queue/driver.py demo

Type `help` for the commands.
"""

import json
import shlex
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))

from test_baseline import QUEUE_STATES, Sandbox, fail  # noqa: E402

HELP = """\
task NAME [key=value ...]   add tasks/pending/NAME.md (keys: model, max_attempts, quiet, cwd, verify, ...; body=TEXT, \\n allowed)
recurring NAME schedule='every 1h' [key=value ...]   add a template to tasks/recurring/
scenario worker=JSON review=JSON other=JSON   script the fake claude: a JSON list of steps per role
pass                        reviewer answers VERDICT: PASS from now on (the default)
fail TEXT                   reviewer answers VERDICT: FAIL with TEXT as feedback, from now on
worker-error TEXT           the worker's claude call exits 1 printing TEXT
set KEY=VALUE               change a .env setting of the sandbox (VALUE empty removes it)
run [N]                     N dispatcher cycles (default 1); prints the dispatcher log
ls                          task names per state
cat STATE NAME              print a task file (STATE: pending, active, done, failed, ...)
check STATE NAME            python3 dispatcher.py check <file>
telegram                    every message the dispatcher sent, and the number of uploads
lessons                     logs/lessons.jsonl
usage                       python3 dispatcher.py usage
calls [ROLE]                every fake claude call: role, model, tools, start of the prompt
path                        the sandbox directory (queue root)
demo                        a canned tour: one pass, one failure with retry, then the totals
quit
"""


def parse_kv(words):
    meta = {}
    for word in words:
        key, sep, value = word.partition("=")
        if not sep:
            raise ValueError(f"expected key=value, got {word!r}")
        meta[key] = value
    return meta


class Driver:
    def __init__(self):
        self.sb = Sandbox()
        self.scenario = {}

    def close(self):
        self.sb.close()

    def push_scenario(self):
        self.sb.set_scenario(**self.scenario)

    def do_help(self):
        print(HELP)

    def do_task(self, name, *pairs):
        meta = parse_kv(pairs)
        body = meta.pop("body", None)
        kwargs = {"body": body.replace("\\n", "\n")} if body else {}
        self.sb.add_task(name, **kwargs, **meta)
        print(f"added tasks/pending/{name}.md")

    def do_recurring(self, name, *pairs):
        meta = parse_kv(pairs)
        body = meta.pop("body", None)
        kwargs = {"body": body.replace("\\n", "\n")} if body else {}
        self.sb.add_task(name, state="recurring", **kwargs, **meta)
        print(f"added tasks/recurring/{name}.md")

    def do_scenario(self, *pairs):
        for role, raw in parse_kv(pairs).items():
            self.scenario[role] = json.loads(raw)
        self.push_scenario()
        print("scenario:", json.dumps(self.scenario))

    def do_pass(self):
        self.scenario["review"] = [{"result": "VERDICT: PASS"}]
        self.push_scenario()
        print("reviewer passes")

    def do_fail(self, *text):
        self.scenario["review"] = [fail(" ".join(text) or "not good enough")]
        self.push_scenario()
        print("reviewer fails")

    def do_worker_error(self, *text):
        self.scenario["worker"] = [{"exit": 1, "stdout": "", "stderr": " ".join(text) or "boom"}]
        self.push_scenario()
        print("worker errors")

    def do_set(self, *pairs):
        self.sb.configure(**{k: (v or None) for k, v in parse_kv(pairs).items()})
        print("ok")

    def do_run(self, count="1"):
        for _ in range(int(count)):
            try:
                proc = self.sb.run()
            except AssertionError as exc:
                print(exc)
                return
            print(proc.stdout.rstrip() or "(dispatcher printed nothing)")

    def do_ls(self):
        for state in QUEUE_STATES:
            print(f"{state:10} {', '.join(self.sb.names(state)) or '-'}")

    def do_cat(self, state, name):
        print((self.sb.base / "tasks" / state / f"{name}.md").read_text().rstrip())

    def do_check(self, state, name):
        proc = self.sb.python(str(self.sb.base / "dispatcher.py"), "check", f"tasks/{state}/{name}.md")
        print((proc.stdout + proc.stderr).rstrip(), f"[exit {proc.returncode}]")

    def do_telegram(self):
        texts = self.sb.telegram.texts()
        for text in texts:
            print("MESSAGE:", text.replace("\n", "\n         "))
        print(f"({len(texts)} messages, {len(self.sb.telegram.calls('sendDocument'))} uploads)")

    def do_lessons(self):
        path = self.sb.base / "logs" / "lessons.jsonl"
        print(path.read_text().rstrip() if path.exists() else "(no lessons yet)")

    def do_usage(self):
        proc = self.sb.python(str(self.sb.base / "dispatcher.py"), "usage")
        print((proc.stdout + proc.stderr).rstrip())

    def do_calls(self, role=None):
        for call in self.sb.claude_calls(role):
            print(f"{call['role']:7} model={call['model']} tools={call['allowed_tools']} "
                  f"prompt={call['prompt'][:70]!r}")

    def do_path(self):
        print(self.sb.base)

    def do_demo(self):
        for line in (
            "task good",
            "run",
            "ls",
            "fail the report was missing",
            "task flaky max_attempts=2",
            "run",
            "lessons",
            "ls",
            "telegram",
        ):
            print(f"> {line}")
            self.handle(line)

    def handle(self, line):
        words = shlex.split(line)
        if not words:
            return True
        command, args = words[0].replace("-", "_"), words[1:]
        if command in ("quit", "exit"):
            return False
        method = getattr(self, f"do_{command}", None)
        if method is None:
            print(f"unknown command {words[0]!r}; try help")
            return True
        try:
            method(*args)
        except (TypeError, ValueError, OSError, KeyError) as exc:
            print(f"error: {type(exc).__name__}: {exc}")
        return True


def main(argv):
    driver = Driver()
    try:
        if argv == ["demo"]:
            driver.do_demo()
            return 0
        print(f"READY sandbox={driver.sb.base}", flush=True)
        for line in sys.stdin:
            if not driver.handle(line):
                break
            print(flush=True)
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
