# Claude Task Queue

A cron-driven task queue for running headless Claude Code jobs on an Ubuntu
server (e.g. a Digital Ocean droplet). Drop a markdown task file into
`tasks/pending/` and walk away: every 15 minutes a dispatcher picks up one
task, runs it with `claude -p` under a restricted tool allowlist, has a cheap
model review the result against the task's acceptance criteria, retries with
feedback (escalating to a stronger model) when the review fails, and pings you
on Telegram when the task lands in `done/` or `failed/`.

One script (`dispatcher.py`, Python standard library only), one config file
(`.env`). No frameworks, no pip installs. Recurring tasks are supported via
schedule templates in `tasks/recurring/` (see below).

## Task lifecycle

```
tasks/pending/ ──► tasks/active/ ──► worker (claude -p, task model)
     ▲                                      │
     │                                      ▼
     │  FAIL: feedback appended,     review (claude -p, cheap model)
     │  requeue until max_attempts          │
     └──────────────────────────────── PASS │ FAIL(final)
                                        ▼        ▼
                                  tasks/done/  tasks/failed/
                                        └── Telegram ──┘
```

- One task per cron run; a lockfile (`flock`) guarantees runs never overlap
  even when a task outlives the 15-minute interval.
- Attempt 1 uses `model`; every retry after a failed review uses
  `escalation_model` (falling back to `model` if unset).
- A run killed mid-task leaves its file in `tasks/active/`; the next run
  recovers it back to `pending/` automatically.
- A queued task can be **cancelled** before it runs: it is archived to
  `tasks/cancelled/` with a timestamped note (move it back to `pending/` to
  requeue). Cancel via `/cancel <task-id>` in the Telegram chat, by asking
  the coordinator, or with `python3 dispatcher.py cancel <task-id>` on the
  box. Cancelling a name that is also a recurring template archives the
  template too, so no further instances spawn. A task already in `active/`
  can't be cancelled mid-attempt. Pending tasks that `depends_on` a
  cancelled task will wait forever (the cancel report warns about them) —
  cancel them too or restore the dependency.
- Rate limits are retried in-process with exponential backoff
  (`30s · 2^n` + jitter, capped at 10 min). If the limit persists through all
  retries, the task returns to `pending/` **without consuming an attempt** and
  the next cron run tries again.
- Each worker run is bounded by its `timeout_minutes`: on expiry the whole
  process group is killed and the attempt fails with timeout feedback, so
  nothing can hang the dispatcher.
- All runs append to `logs/dispatcher.log`; each attempt's full worker and
  reviewer transcript goes to `logs/<task>.attempt-<N>.log`.

## Task file format

A task is a markdown file with flat YAML frontmatter and a mandatory
`## Acceptance Criteria` section (see `tasks/examples/example-task.md`):

```markdown
---
model: claude-sonnet-5                        # required: worker model, attempt 1
escalation_model: claude-opus-4-8             # optional: model for attempts ≥ 2
review_model: claude-haiku-4-5-20251001       # required: cheap reviewer model
max_attempts: 3                               # required
timeout_minutes: 20                           # optional (default from .env)
allowed_tools: Read,Glob,Grep,Edit,Write      # optional (default from .env)
mcp_config: mcp/fetch.json                    # optional MCP servers JSON (default from .env)
depends_on: setup-task, fetch-data            # optional: run only after these tasks are done
cwd: /home/you/project                        # optional working dir (default: workspace/)
attempts: 0                                   # managed by the dispatcher
---
# Task title

Instructions for the worker...

## Acceptance Criteria
- Concrete, checkable criterion 1
- Concrete, checkable criterion 2
```

Frontmatter is parsed as flat `key: value` pairs — no nesting. On a failed
review the dispatcher appends an `## Attempt N Feedback` section to the file
and requeues it, so the next attempt sees exactly what the reviewer flagged.
On success the worker's report is appended as `## Result`.

Tasks missing required keys or the acceptance-criteria section are moved
straight to `failed/` with a note explaining why.

### Task dependencies

A task runs only after every task named in its `depends_on` frontmatter has
reached `done/`. **A task's ID is its file stem** — `setup-skill.md` has ID
`setup-skill` — so use kebab-case names without spaces or commas, and list
dependencies comma-separated (a trailing `.md` is tolerated):

```yaml
depends_on: setup-skill, fetch-data
```

On each run the dispatcher scans `pending/` in age order and picks the first
task whose dependencies are all satisfied — waiting tasks never block
runnable ones. Per dependency:

- **In `done/`** — satisfied. A dependency on a recurring template's stem is
  satisfied by *any* completed instance (`<stem>-<timestamp>.md`).
- **In `failed/`** — the dependent task **cascades to `failed/`** with a
  `## Dependency Failed` note and a Telegram alert. To recover: fix and
  requeue the dependency first (it must reach `done/`), then move the
  cascaded task back to `pending/`.
- **In `pending/`, `active/`, or `recurring/`** — the task waits its turn.
- **Nowhere** — the task waits, with a log warning; this is either a typo in
  the name or a dependency you plan to queue later (both files can be
  dropped in at once — order doesn't matter).

Caveats: don't reuse the stem of an old task still sitting in `done/` (it
would satisfy the dependency immediately — prune `done/` or pick fresh
names), and there is no cycle detection — two tasks depending on each other
just wait forever, visible as a repeated "waiting on dependencies; nothing
runnable" log line.

## Recurring tasks

Put a task template in `tasks/recurring/` with one extra frontmatter key,
`schedule` (see `tasks/examples/example-recurring-task.md`). On every run the
dispatcher checks each template and, when it comes due, copies a normal
one-shot instance into `pending/` named `<template>-<timestamp>.md`,
which then flows through the usual worker → review → done/failed lifecycle.

Supported schedules (all times are **server-local**; cron runs every 15 min,
so that's the effective precision):

```yaml
schedule: every 30m          # also every 6h, every 2d
schedule: daily at 06:30
schedule: weekly on mon at 09:00
```

Details:

- The dispatcher writes a `last_run` timestamp back into the template after
  each spawn — don't edit it by hand unless you want to force or suppress
  the next firing (deleting it makes the template fire on the next run).
- If a previous instance of the same template is still in `pending/` or
  `active/`, the spawn is deferred (no pile-up); it fires once the queue
  clears, and misses in between collapse into a single catch-up instance.
- A failed instance lands in `failed/` like any task, and the schedule keeps
  firing on its next due date regardless.
- Templates with a missing or unparseable `schedule` are skipped with an
  error in the log.

## Giving tasks MCP access

Tasks can use MCP servers (databases, APIs, fetch, GitHub, …) in two ways:

1. **Per-task config (recommended)** — add an `mcp_config` frontmatter key
   pointing at a servers JSON file (relative paths resolve against the queue
   directory). The dispatcher passes it to the worker as `--mcp-config`:

   ```yaml
   mcp_config: mcp/fetch.json
   allowed_tools: Read,Write,mcp__fetch__fetch
   ```

   The config uses the standard `.mcp.json` shape — see `mcp/fetch.json` for
   a working example (requires `uvx` on the droplet: `pip install uv`). Set
   `DEFAULT_MCP_CONFIG` in `.env` to apply one config to every task that
   doesn't override it. A task whose `mcp_config` file doesn't exist is
   rejected to `failed/` before any API call.

2. **Zero-config** — a `.mcp.json` in the task's `cwd` is auto-discovered by
   headless runs; nothing to declare in the task file.

Either way, **the allowlist is the real gate**: MCP tools must be named in
`allowed_tools` as `mcp__<server>__<tool>` (or `mcp__<server>__*` for a whole
server). Calls to tools not on the list are denied without hanging the run —
the worker is told and carries on, and the review catches anything it
couldn't do. Security notes:

- An MCP server is a standing credential sitting on your droplet. Prefer
  specific `mcp__server__tool` entries over `mcp__*`, exactly like the
  narrow `Bash(...)` patterns.
- The reviewer never gets MCP — it runs with `Read,Glob,Grep` only.

## Skills

Headless runs discover Agent Skills automatically — from
`<cwd>/.claude/skills/<name>/SKILL.md` (project-scoped) and
`~/.claude/skills/` (available to every task). That enables two things:

**Tasks can use skills.** Allow a skill per task with `Skill(<name>)` in
`allowed_tools` (or `Skill` for all of them), and invoke it in the task body
with `/skill-name` — or just describe the work and let the model trigger it:

```yaml
allowed_tools: Read,Edit,Write,Skill(changelog-entry)
```

**Creating a skill can itself be a task.** A skill is just a markdown file
on disk — no registration step — so a task with `Write` permission can
author one, and every later task sharing that `cwd` picks it up
automatically. See the worked pair in `tasks/examples/`:
`example-skill-creation-task.md` builds `.claude/skills/changelog-entry/`,
and `example-skill-use-task.md` invokes it with `/changelog-entry` — its
`depends_on: example-skill-creation-task` line means you can drop both into
`pending/` at once and they run in the right order. The queue effectively
teaches itself new capabilities over time.

## Chat with the coordinator

`coordinator_bot.py` turns the same Telegram chat that receives queue
notifications into a two-way conversation with a **coordinator** agent — a
Claude session whose role (defined in `coordinator/CLAUDE.md`) is to turn
your ideas into well-formed task files. Describe a project from your phone;
the coordinator asks about constraints and scope, proposes a decomposition
into phase-sized tasks with dependencies, shows you the task list for
approval, and only then writes the files into `tasks/pending/` — where the
cron dispatcher picks them up. It never implements anything itself.

- The bridge is a long-polling daemon (chat needs sub-second pickup, so
  it's a systemd service, not a cron job):

  ```bash
  cp coordinator/claude-coordinator.service /etc/systemd/system/
  # edit the paths in the unit if the queue doesn't live at /root/claude-task-queue
  systemctl daemon-reload
  systemctl enable --now claude-coordinator
  journalctl -u claude-coordinator -f     # watch it (also logs/coordinator.log)
  ```

- **Conversation continuity**: replies stay in one Claude session (resumed
  via `--resume`), so the coordinator remembers the discussion. Send `/new`
  to start over; if a stored session can't be resumed the bridge falls back
  to a fresh one automatically.
- **Built-in commands** (instant, no API cost): `/new` resets the
  conversation; `/status` reports queue counts and task names straight from
  the `tasks/` directories; `/cancel <task-id>` archives a pending task or
  recurring template to `tasks/cancelled/`. You can also just tell the
  coordinator to drop a task — it runs the same cancel command after
  confirming (allowed via a narrow `Bash(python3 …/dispatcher.py cancel …)`
  pattern, not general shell access).
- **Long-term memory**: the coordinator keeps a curated
  `coordinator/memory/MEMORY.md` — imported into every session via its
  CLAUDE.md — where it records durable facts: your preferences, project
  state, decisions, lessons from failed tasks. Unlike `--resume`
  continuity, it survives `/new` and unresumable sessions, so the system
  accumulates context over time. It's a plain markdown file: edit or prune
  it by hand whenever you like.
- **Security**: only messages from `TELEGRAM_CHAT_ID` are answered — anyone
  else who finds the bot is logged and ignored. The coordinator itself runs
  with `Read,Glob,Grep` plus `Write`/`Edit` scoped to `tasks/` and
  `coordinator/memory/`, and the scoped cancel command (override with
  `COORDINATOR_ALLOWED_TOOLS` in `.env` — note a custom value must include
  the memory and cancel entries or those features silently stop working),
  so it can plan and queue but can't touch the dispatcher or anything else
  on the box.
- Notifications and chat share the bot without conflict: the dispatcher only
  ever sends messages, and the bridge is the only `getUpdates` consumer
  (enforced by its own lockfile). Long coordinator replies are split across
  messages rather than truncated. Rate limits get the same exponential
  backoff as the dispatcher; a coordinator run is capped at
  `COORDINATOR_TIMEOUT_MINUTES` so the chat can't hang.

## Setup

Prerequisites: Ubuntu with Python 3.10+, and the Claude Code CLI installed
and authenticated for the user that will run cron (run `claude` once
interactively to log in, or use `claude setup-token` for long-lived headless
credentials).

1. **Get the code onto the droplet** (clone this repo or copy the
   `claude-task-queue/` directory) and enter it:

   ```bash
   cd claude-task-queue
   ```

2. **Create a Telegram bot**: message [@BotFather](https://t.me/BotFather),
   send `/newbot`, and save the token. Send your new bot any message, then
   find your chat id in the response of:

   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```

3. **Configure**:

   ```bash
   cp .env.example .env
   nano .env    # set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CLAUDE_BIN
   ```

   Set `CLAUDE_BIN` to the absolute path from `which claude` — cron runs with
   a minimal `PATH` and won't find a bare `claude`. The claude.ai install
   script (`curl -fsSL https://claude.ai/install.sh | bash`) puts it at
   `~/.local/bin/claude`. If `CLAUDE_BIN` is empty or wrong, the dispatcher
   falls back to `PATH`, then `~/.local/bin/claude`, then
   `/usr/local/bin/claude`; if none exist it logs an error and requeues the
   task without consuming an attempt.

4. **Smoke-test** (creates the `tasks/` and `logs/` directories):

   ```bash
   python3 dispatcher.py        # should log "Queue empty; nothing to do"
   ```

5. **Queue the example task and run once manually**:

   ```bash
   cp tasks/examples/example-task.md tasks/pending/
   python3 dispatcher.py
   tail -f logs/dispatcher.log
   ```

6. **Install the cron job** (`crontab -e`):

   ```cron
   */15 * * * * /usr/bin/python3 /home/you/claude-task-queue/dispatcher.py >> /home/you/claude-task-queue/logs/cron.log 2>&1
   ```

   Overlap is safe: the in-script lockfile makes a second invocation exit
   immediately while one is running.

## Security notes

- The tool allowlist is passed to `claude -p --allowedTools`; in headless
  mode any tool not on the list is denied rather than prompted. The default
  list deliberately excludes unrestricted `Bash` — grant narrow patterns per
  task (e.g. `Bash(python3 *)`, `Bash(git *)`) instead of blanket shell
  access.
- The reviewer runs with read-only tools (`Read,Glob,Grep`) in the task's
  working directory so it can verify claims against real files.
- `.env` holds your Telegram token; it is gitignored — keep it that way and
  `chmod 600 .env`.

## Operations

- **Watch the queue**: `ls tasks/pending tasks/active tasks/done tasks/failed`
- **Remove a queued task**: `/cancel <task-id>` in the Telegram chat, ask
  the coordinator, or `python3 dispatcher.py cancel <task-id>` — the task
  is archived to `tasks/cancelled/`. Undo by moving the file back to
  `tasks/pending/`.
- **Read a task's history**: the task file itself accumulates feedback and
  results; raw transcripts are in `logs/<task>.attempt-<N>.log`.
- **Retry a failed task**: fix it up, reset `attempts: 0`, and move it back
  to `tasks/pending/`.
- **Pause the queue**: comment out the cron line, or move pending tasks
  aside — the dispatcher exits cleanly on an empty queue.
- **Stop a recurring task**: move its template out of `tasks/recurring/`.
  Completed instances accumulate in `done/`; prune them occasionally.
