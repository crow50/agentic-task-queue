---
name: run-agentic-task-queue
description: Build, run, test and drive the agentic task queue (dispatcher.py, the Telegram coordinator, the learning loop). Use when asked to run or start the queue, push a task through it, try a failure or retry, check lessons.jsonl or Telegram messages, validate a task file, or run the tests.
---

The queue is a cron-driven Python dispatcher (`dispatcher.py`) that runs markdown task files through `claude -p` (worker, then reviewer) and reports to Telegram. You cannot run it for real here without spending claude usage and a bot token, so drive it through `.claude/skills/run-agentic-task-queue/driver.py`: a REPL over the repo's own test Sandbox (a temp copy of the queue, `tests/fake_claude.py` as `claude`, `tests/fake_telegram.py` as the Bot API). `dispatcher.py` itself runs unmodified as a subprocess.

All paths are relative to the repo root.

## Prerequisites

Python 3.11+ with the `venv` module (3.12.3 here) and git. `tmux` is optional (used below to keep the REPL alive). No apt packages were needed.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -q --require-hashes -r requirements.txt
.venv/bin/python -c "import filelock, httpx; print('deps ok')"   # -> deps ok
```

The tests are stdlib only, but they run `dispatcher.py`, which needs these libraries. Always use `.venv/bin/python`.

## Run (agent path)

One-shot tour (a pass, then a failure that is requeued with feedback and logged as a lesson):

```bash
.venv/bin/python .claude/skills/run-agentic-task-queue/driver.py demo
```

Scripted: pipe commands on stdin, one per line.

```bash
printf '%s\n' "fail the report was missing" "task flaky max_attempts=2" "run" "lessons" "ls" "quit" \
  | .venv/bin/python .claude/skills/run-agentic-task-queue/driver.py
```

Interactive, state kept across commands (the sandbox lives as long as the process). Use a wide pane: log lines are long and wrap.

```bash
tmux new-session -d -s tq -x 200 -y 60 ".venv/bin/python .claude/skills/run-agentic-task-queue/driver.py"
timeout 30 bash -c 'until tmux capture-pane -t tq -p | grep -q READY; do sleep 0.2; done'
tmux send-keys -t tq "recurring pulse schedule='every 1h' quiet=true" Enter
tmux send-keys -t tq "run" Enter
sleep 3; tmux capture-pane -t tq -p -S -100 | grep -v '^$'
tmux send-keys -t tq "quit" Enter
```

| command | what it does |
|---|---|
| `task NAME [key=value ...]` | add `tasks/pending/NAME.md` (`max_attempts=2`, `quiet=true`, `verify=...`, `body=TEXT`) |
| `recurring NAME schedule='every 1h' [k=v ...]` | add a template to `tasks/recurring/` |
| `scenario worker=JSON review=JSON` | script the fake claude (steps per role, see `tests/fake_claude.py`) |
| `pass` / `fail TEXT` / `worker-error TEXT` | shortcuts for the reviewer's verdict and a failing worker call |
| `set KEY=VALUE` | change a sandbox `.env` setting (empty value removes it) |
| `run [N]` | N dispatcher cycles, printing its log; one cycle processes one task |
| `ls`, `cat STATE NAME`, `check STATE NAME` | queue state, a task file, `dispatcher.py check` on it |
| `telegram`, `lessons`, `usage`, `calls [ROLE]` | messages sent, `logs/lessons.jsonl`, the usage report, every fake claude call |
| `path`, `help`, `quit` | sandbox dir, command list, exit |

The sandbox is a temp dir (`taskq-test-*`) removed on exit; nothing in the repo's own `tasks/`, `logs/` or `.env` is touched. A fresh process is the way to reset it.

## Direct invocation

The dispatcher's own subcommands work on a real checkout without any claude or Telegram:

```bash
.venv/bin/python dispatcher.py check tasks/examples/example-task.md   # -> example-task.md: OK
```

## Test

```bash
.venv/bin/python -m py_compile dispatcher.py coordinator_bot.py
.venv/bin/python -m unittest discover -s tests          # 285 tests, about 2 minutes, all OK
(cd tests && ../.venv/bin/python -m unittest test_learning.LessonsTests)   # one class: 8 tests, 3 s
```

CI also runs ruff (`.venv/bin/pip install ruff && .venv/bin/ruff check .` passes; CI pins its own version).

## Run (human path)

Production is `sudo python3 scripts/install.py` (creates the `taskq` user, cron every 15 min, the coordinator systemd unit; needs a bot token and a claude login). Not exercised here: it needs root and real credentials.

## Gotchas

- **`check` decides "template" by the directory name.** `dispatcher.py check tasks/examples/weekly-improver.md` fails with `'schedule' only belongs in tasks/recurring/ templates`; the file must sit in a directory called `recurring` (the driver's `check recurring NAME` does).
- **A usage limit pauses the sandbox queue for an hour.** After a worker step with stderr `Claude AI usage limit reached`, `run` only prints `Queue paused until ...; exiting` and the task stays pending. Start a new driver process to reset.
- **`quiet: true` tasks send no Telegram message on a pass** (`telegram` shows `0 messages`); a permanent failure still does.
- **The fake claude steps repeat the last entry.** `fail ...` / `worker-error ...` stay in force for every later call until you `pass` or set another scenario.
- **One `run` = one task.** A due recurring template is spawned and processed in the same cycle, but two pending tasks need two `run`s.
- **Env is scrubbed.** The dispatcher subprocess sees only `PATH`, `HOME`, `LC_ALL` and the scenario path, so a real token or claude login can never leak into a driver run, and a real `claude` can't be used through it.

## Troubleshooting

- `unknown command 'x'; try help`: the driver only knows the commands in the table (`-` and `_` are interchangeable, so `worker-error` works).
- `error: FileNotFoundError: ... tasks/done/nope.md`: `cat`/`check` need an existing STATE and NAME; run `ls` first.
