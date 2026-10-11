# Roadmap: phases 9 to 19

This continues the local `PLAN.md` (which the repo ignores). Phases 0 to 5 have shipped. Phases 6 to 8 there
(autonomy tiers, homelab monitoring, second-brain context) are still planned. This file adds the next eleven
phases and the two spikes. It is written to stand on its own: where it needs an idea from Phases 6 to 8, it
restates it.

## Where this starts

- **Host:** a Raspberry Pi 5 with 8 GB of RAM and 4 cores. About 4 GB is free with the queue idle. Docker is
  installed. n8n, Ollama and the Gemini CLI are not.
- **Queue:** cron-driven `dispatcher.py` runs one task at a time through `claude -p` (worker, then a Haiku
  reviewer) and reports to Telegram. `coordinator_bot.py` is a resumed Claude session that turns messages into
  tasks. `logs/usage.jsonl` records every call. `logs/lessons.jsonl` records every failed attempt, and a weekly
  task proposes patches as pull requests.
- **Known fault:** the coordinator resumes one session, and the `total_cost_usd` and `modelUsage` it logs are
  running totals for that session, not per call. Summing them overstates its cost about 13 times (about $92
  logged against about $7 real, Oct 4 to Oct 11). Phase 9 fixes this first, because every alert and
  every cost comparison below depends on it.
- **Tiers (from Phase 6, restated):** tier 0 is read-only (`Read`, `Glob`, `Grep`, `WebSearch`, `WebFetch`).
  Tier 1 writes inside the task's own worktree. Tier 2 leaves the box (`git push`, `gh pr`, deploys, `ssh`) and
  needs a human, and its result is a draft pull request, never a merge. The tier is computed from `allowed_tools`
  by `validate_task`, never declared by the task.

## North star: a personal assistant that earns its reach

The goal is an assistant that hears you and answers aloud, knows you, notices things before you ask, handles
the small stuff itself, and calls in specialists for the rest. Each of those maps to a phase below:

| Trait | What it means here | Phases |
| --- | --- | --- |
| One voice, many hands | The coordinator is the only thing you talk to. Agents are its staff. | 10, 13 |
| Hears and speaks | Voice notes in, spoken replies out, then a hands-free device | 17 |
| Knows you | A vault of notes, decisions and preferences, plus coordinator memory | 15 |
| Notices without being asked | Watchers on mail, calendar, feeds, the lab; a morning brief; few, useful interruptions | 18 |
| Does things for you | Email, calendar, home and files, with trust earned class by class | 19, 14 |
| Always on and quick | Cheap fast paths, an acknowledgement in a second, a model on the Pi for small jobs | 9, 16, Spike B |
| Safe to leave alone | Tiers, sandboxes, budgets, audit and undo | 6a, 12, 19 |

**The rule that makes it safe is the autonomy ladder.** Every kind of action (archive a newsletter, switch a
light scene, accept a calendar invite) has a level. Level 0 only answers. Level 1 prepares and waits for your
tap. Level 2 acts, can be undone, and tells you. Level 3 acts and logs, and is for things you never want to
hear about. New classes start at level 1. The coordinator can suggest a promotion after a track record ("12 of
12 drafts approved: do these yourself?"), and any undo or 👎 demotes the class at once. A short list of
actions can never be promoted: spending money, deleting data, sending as you to someone new, and anything on
locks, alarms or cameras. The more reach it has, the more of this matters, so Phase 19 comes after the sandbox
and the audit trail.

**Honest limits.** Each message today starts a `claude -p` process, so replies take seconds, not
milliseconds (Phase 17 works on this). A Pi has no GPU, so on-host speech and models are small and slow. An
always-listening microphone is a privacy decision, so voice starts as Telegram voice notes and push-to-talk,
and a wake word comes later and runs locally.

## Rules for every phase

1. **Tests first, shown failing, then the code.** `unittest` only, no new test dependencies. Add test doubles
   next to `fake_claude` (`fake_docker`, `fake_gemini` and so on) instead of calling anything real.
2. **Small diffs in `dispatcher.py`.** Put new logic in its own module or script and call it from one place.
   `dispatcher.py`, `scripts/`, `tests/` and `.github/` stay owned by you in `CODEOWNERS`.
3. **Off until switched on.** Each feature has a setting that defaults to the old behavior, so a merged phase
   changes nothing until you enable it. Every setting goes in `.env.example`.
4. **Silent unless it matters.** Alerts fire once per condition (through `state/seen.json`) and say what to do.
5. **Fail closed.** If a sandbox, a token check or a profile cannot be applied, the task is requeued without
   using an attempt, and you get one message. It does not run with fewer limits.
6. **Everything is a pull request.** Phases ship on their own `phase-N-name` branch. Anything the learning
   loop proposes still arrives as a PR.

## Order and dependencies

| Phase | What | Needs | Size | Done when |
| --- | --- | --- | --- | --- |
| 9 Visibility and savings | usage fix, context and budget alerts, session rotation, token savings, golden tasks | none | 2 sessions | tests pass, and cost per passing golden task is at most 80% of baseline with no lower pass rate |
| 10 Telegram menu | `/menu` buttons for the learning review, skill creation, usage, pause | 9 | 1 session | tests pass, plus one tap from a phone |
| 17 Voice | voice notes in, spoken replies out, faster acknowledgement; a hands-free device later | 10 | 2 sessions | tests pass, plus a spoken task and a spoken status |
| 6a Tiers and enqueue | tier from `allowed_tools`, `dispatcher.py enqueue` (pulled forward from Phase 6) | none | 1 session | tests pass |
| 11 Intake and n8n | local HTTP intake, source profiles, callback, n8n in Docker | 6a | 2 sessions | tests pass, plus an n8n schedule runs a task end to end |
| 18 Proactive | watchers, triage, an attention budget, a morning brief, standing instructions | 11, 9 | 2 sessions | a week with one brief and at most 5 interruptions a day, 70% rated useful |
| 12 Docker workers | each worker and reviewer call runs in a locked-down container | 6a | 2 sessions | tests pass, plus the on-Pi escape checks |
| 13 Agents and hiring | agent profiles, a guided hiring flow, probation, per-agent lessons | 9, 12 | 2 sessions | tests pass |
| 14 MCP expansion | a registry of servers with scoped secrets, run inside the sandbox | 12, 13 | 1 to 2 sessions | tests pass, plus a real task using one server |
| 15 Second brain | git vault, Drive ingest, an inbox, full-text search | 12, 13 | 2 sessions | golden pass rate with context at least the baseline |
| 19 Acting for you | the autonomy ladder, an audit trail with undo, email, calendar and home actions | 12, 14, 18 | 3 sessions | tests pass, plus two weeks of one level 2 class with no undos |
| Spike A embeddings | decide if a vector search is worth having | 15 | half a session | a written decision |
| Spike B local model | decide if an on-host model is worth having | 9 | half a session | a written decision |
| 16 Other models | a runner interface, Gemini, optionally a local model | 9, 12, spikes | 2 sessions | tests pass, plus a real Gemini task in a container |

Phase 9 goes first because it is cheap, it repays itself, and everything after it needs its numbers. The
fastest route to a real assistant feel is 9, 10, 17, then 6a, 11 and 18: voice and a morning brief change how it feels
to use long before the sandbox and agents do, and neither needs them. Phase 19 deliberately comes after 12,
14 and 18, because acting for you is only safe once there is a sandbox, scoped tools and a feed of events to
act on. Phase 16 comes last because it needs the sandbox and the cost data to be safe and worth doing.

**Memory on the Pi.** Budget for the 8 GB: the queue and bot 0.3 GB, n8n up to 0.8 GB, one worker container
up to 1 GB, the egress proxy 0.05 GB, speech (whisper.cpp and Piper, loaded on demand) up to 0.6 GB, a local model up to
2.5 GB if the spike says yes. These do not all fit
at once with the 3 to 4 GB the host already uses, so the dispatcher keeps running one task at a time, and a local
model stays optional.

## Phase 9: Visibility and savings

**Goal:** see what each message, task and session costs, get told before a limit or a context window is hit,
and cut the spend without lowering the pass rate.

1. **Fix the accounting.** Log the envelope's `session_id` on every `usage.jsonl` row. Keep
   `state/session_costs.json` (last `total_cost_usd` per session). Write a new field `cost_usd` equal to the
   increase over the previous total for the same session, and to the total for a new session. Keep
   `total_cost_usd` as logged. `dispatcher.py usage` sums `cost_usd`. For rows written before the fix, it
   computes the increase from file order per label, resetting when the total drops. `usage` also takes
   `--by day|label|model|agent`.
2. **Measure context.** Per call, the context size is the last iteration's `input_tokens +
   cache_read_input_tokens + cache_creation_input_tokens` from `usage.iterations[-1]`, and the window is
   `modelUsage[model].contextWindow`. Log `context_tokens` and `context_pct`. Check both fields against a real
   envelope before relying on them.
3. **Alerts (new settings, all off at 0).** `CONTEXT_WARN_PCT` (default 70), `CONTEXT_ROTATE_PCT` (85),
   `DAILY_COST_BUDGET_USD` and `DAILY_TOKEN_BUDGET`. An alert says what crossed which line and what to do,
   and fires once per day per condition. A worker call over the warn line on its own means the task is too big,
   so the message names the task. The plan's real usage limit isn't visible to the queue, so budgets are your own
   numbers, set from what `usage` shows.
4. **Rotate the coordinator session.** At `CONTEXT_ROTATE_PCT`, the next message first runs one short call that
   writes the key facts of the session to `coordinator/memory/`, then starts a fresh session seeded from
   `coordinator/CLAUDE.md` and that memory. You get one line: "Started a fresh session (context was 86%)." Also
   cap the memory directory (`COORDINATOR_MEMORY_MAX_KB`) so seeding doesn't grow without limit. The log shows
   a single coordinator call reading 1.8 million cache tokens, so growth, not message count, is the cost.
5. **Savings, each behind its own switch and measured:**
   - `review: skip-if-verified` skips the Haiku review when a `verify:` command passed and the criteria are all
     covered by it. Reviews are about 8% of spend today, so this is small but certain.
   - `max_turns:` frontmatter, passed as `--max-turns` (confirm the flag with `claude --help`), so a runaway
     task stops. The log shows worker calls of 55 turns and $2.
   - Keep everything that never changes (the preamble) first in the prompt, and everything task-specific
     after it, so prompt caching hits. A test pins the order.
   - A model table, `models.json`, mapping role and `value_class` to a model, read by the dispatcher. The
     weekly improver may propose edits to it from `usage.jsonl`, as a PR.
   - The worker preamble gets one line telling it to do fetching and filtering with scripts and bring only
     the summary into the conversation.
6. **Golden tasks.** Create `tests/golden/` with five small real tasks, each with a `verify:` command, and
   `scripts/golden.py --route <name>`, which runs them against a route (a model, a runner, an agent or a
   setting) and prints pass rate, cost per pass and time as a table. Every later phase uses this to prove its
   change helps. Run against `fake_claude` in CI. Run for real by hand.

**Tests:** two rows in one session at 0.10 and 0.25 total 0.25, not 0.35, and a new session resets. Rows
without `cost_usd` are summed by the fallback. The context percentage is computed from the last iteration. An
alert fires once and then stays quiet until the next day. A rotation starts exactly one fresh session. A
verified task with `skip-if-verified` makes zero review calls, and an unverified one makes one. The preamble is
the first thing in every worker prompt. `golden.py` against `fake_claude` prints a table and exits 0.

**Done when:** the unit tests pass, and on the Pi `golden.py` with the savings on costs at most 80% per passing
task than with them off, with the same pass rate. If the savings don't reach that, say so in the PR and keep
only what helped.

## Phase 10: Telegram menu

**Goal:** start the learning review or a skill from a menu on your phone, with no typing and no model call to
draw the menu.

1. **A `/menu` command and `setMyCommands`.** `/menu` sends one message with inline buttons: Status, Usage,
   Pause or Resume, Run learning review, Create skill from a task, and (after Phase 13) Hire an agent.
2. **Handle `callback_query`.** Add it to `allowed_updates` in the long poll. The sender check that applies to
   messages applies to the button too (`from.id` equals `TELEGRAM_USER_ID`). Answer every callback so the button
   stops spinning, and edit the menu message to show the outcome.
3. **Buttons enqueue, they don't call a model.** `callback_data` is `act:<name>[:<arg>]` and must stay under
   64 bytes.
   - **Run learning review** copies `tasks/examples/weekly-improver.md` into `tasks/pending/` for now, unless
     one is already pending or running, in which case it says so.
   - **Create skill from a task** lists the last eight done tasks as buttons, then enqueues a new
     `tasks/examples/skill-from-task.md` with that task's id. The worker reads the task and its result and
     writes a `SKILL.md` patch to `tasks/proposals/`. The existing opener (which already allows
     `.claude/skills/`) validates it and opens the PR. Like the weekly task it is `quiet: true`.
4. **Confirm anything that spends.** Those two buttons show the average cost of that kind of call from
   `usage.jsonl` and need a second tap labelled "Confirm".
5. **A name that is yours.** The assistant is called `taskq` until you say otherwise. `ASSISTANT_NAME` (in
   `.env`) is what Telegram messages, the morning brief and spoken replies use, and `coordinator/identity.md`
   holds a few lines on how it talks. A menu button, "Name your assistant", starts a short exchange: the
   coordinator proposes three names with a reason each (or you tell it to choose for itself), you pick or ask
   for more, and it saves the choice to `.env` and `identity.md` through a normal PR. The name is changeable
   later, and a wake word (Phase 17) is trained or chosen after the name settles, not before.

**Tests (extend `fake_telegram` to send `callback_query` updates):** the wrong sender gets no reply. With
`ASSISTANT_NAME` unset, messages say `taskq`, and with it set they use the new name. The naming exchange ends
with a diff that touches only `identity.md` and the `.env.example` default, never `.env` itself. Two taps on
"Run learning review" create one task. A callback longer than 64 bytes is never produced. `skill-from-task`
passes `dispatcher.py check`, and a scripted `fake_claude` run of it produces a patch the opener accepts.

**Done when:** the tests pass, and on your phone one tap plus Confirm creates a task and, if the improver finds
nothing, you hear nothing.

## Phase 6a: Tiers and enqueue (pulled forward from Phase 6)

**Goal:** the two Phase 6 pieces that Phases 11 to 14 depend on, without the rest of Phase 6.

1. **Tier computed from `allowed_tools`.** Put the rule in one function used by `validate_task`: any of `Bash`,
   `Write`, `Edit` outside a scoped pattern is tier 1 at least. Anything that can push, create a PR other than
   through the opener, deploy or run `ssh` is tier 2. A task with an `allowed_tools` entry above the tier its
   source is allowed is rejected with a message that names the entry.
2. **`python3 dispatcher.py enqueue FILE|-`.** Validate with the same code as `check`, add the missing
   defaults, write atomically into `tasks/pending/`, and print the id. Exit non-zero with the validation message
   on a bad task.
3. **A `source:` frontmatter key** (`telegram`, `cron`, `n8n:<workflow>`, `agent:<name>`), recorded in the task
   and in `lessons.jsonl`.

**Tests:** known tool lists classify to the expected tier, a bare `Bash` is at least tier 1, and `gh api` is
tier 2. `enqueue` rejects a bad task with the same message as `check`, and a good one appears in `pending/`
exactly once.

**Done when:** the tests pass.

## Phase 11: Intake and n8n

**Goal:** n8n handles what a cron-driven queue is bad at (recurring checks that need no model, webhooks,
polling, long-running or fan-out flows), and hands the work that needs judgment to the queue. This adds an
integration, not a replacement: the queue stays the only place that runs the model.

1. **Which side does what.** n8n owns triggers, schedules, polling, retries and plumbing between services. The
   queue owns anything that needs the model, a worktree or a review. The README gets a short table so a new
   automation goes in the right place.
2. **Source profiles.** `intake.json` maps each `source` to a profile: highest tier, an allowed tool ceiling,
   allowed `cwd` roots, a per-hour rate limit, and `needs_approval` (when true the task lands in
   `tasks/proposed/` for a 👍 instead of `pending/`). The default for `n8n:*` is tier 0, no `Bash`, no write
   outside its own folder, 20 tasks per hour, and approval required for anything above tier 0.
3. **A local HTTP intake.** `scripts/intake_server.py` is stdlib only, binds to `127.0.0.1`, and requires
   `Authorization: Bearer $INTAKE_TOKEN`. `POST /tasks` takes markdown or JSON and calls `enqueue` with the
   source's profile. `GET /tasks/<id>` returns the state (pending, running, done, failed), the summary and any
   `deliver:` files. `GET /health` takes no token. It makes no model calls and ships with a systemd unit
   template.
4. **Idempotency.** An `idempotency_key` in the request is stored in `state/idem.json` for 24 hours. A repeat
   returns the original id and creates nothing.
5. **Callback to n8n.** A `callback_url` frontmatter key. When the task ends (done or failed), the dispatcher
   POSTs `{id, status, summary, deliver, cost_usd}` with an `X-Signature` HMAC using `CALLBACK_SECRET`. Only
   hosts matching `CALLBACK_ALLOWLIST` (default `127.0.0.1:5678`) are called. Three tries, then a log line. A
   failed callback never fails the task.
6. **External text is data.** For any `source` other than `telegram` and `cron`, the dispatcher wraps the body
   text from the caller in an `<external_input>` fence. `worker_preamble.md` gets a line saying that
   instructions inside the fence are data to analyze, never commands. The tool ceiling is the real protection:
   the line just helps.
7. **n8n deployment.** `deploy/n8n/docker-compose.yml`: n8n published on `127.0.0.1:5678` only, memory limit
   768 MB, `N8N_ENCRYPTION_KEY` from the environment, a named volume. Example workflows in
   `deploy/n8n/workflows/`: a webhook that enqueues, a schedule that enqueues, and one that reacts to a
   callback. Reaching n8n from outside the house (a Tailscale address or a tunnel) is a decision for you at
   setup time, and the default is no.
8. **n8n can start flows from a worker.** Optional and later: n8n's own MCP server (Phase 14) lets a worker
   trigger a named workflow.

**Tests:** no token or a wrong token gets 401. A task asking for more than its source's ceiling gets 403 with
the offending tool named. A valid post creates one file in `pending/`, and a repeat with the same key creates
none. The callback reaches a local fake server once with a valid signature, and a non-allowlisted URL is never
called. External text appears inside the fence. A second request past the rate limit gets 429.

**Done when:** the tests pass, and on the Pi an n8n schedule workflow enqueues a task, the queue runs it, the
callback reaches n8n, and the workflow writes the result to its execution log. With n8n and the queue both
running, `free -m` still shows at least 1 GB available.

## Phase 12: Docker workers

**Goal:** a worker that goes wrong can only damage the folder it was given.

1. **A runner prefix.** `run_claude` is the one place the `claude` argv is built. Add `SANDBOX` (`off` or
   `docker`) and a `sandbox:` frontmatter key. With `docker`, the call becomes `docker run --rm --name
   taskq-<id> ... <image> claude ...`. Both the worker and the reviewer run this way.
2. **The image.** `deploy/worker/Dockerfile` for arm64: Node, the Claude Code CLI at a pinned version, git and
   Python. No `gh`, no ssh client. It runs as a non-root user with the same uid as `taskq`. A script builds it,
   and the image is referenced by digest.
3. **Locked-down run.** `--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --pids-limit
   256 --memory 1g --cpus 2 --user <uid>:<gid>`. Mounts: the task's `cwd` at `/work` (read-only for tier 0,
   read-write for tier 1), MCP configs read-only, and nothing else. The login token goes in with `-e NAME` so
   the value never appears in `ps` or in logs.
4. **Network.** The Claude CLI always needs `api.anthropic.com`, so "no network" is not possible. Put the worker
   on an internal Docker network whose only exit is an egress proxy container (a small allowlisting proxy).
   `HTTPS_PROXY` points at it. The allowlist is the API host plus the hosts a task declares in a `net:` key,
   and tier 0 tasks without `WebFetch` get the API host only. The proxy log shows what every worker reached.
5. **Lifecycle.** The container name makes it killable: a timeout runs `docker kill taskq-<id>`. At startup the
   dispatcher removes leftover `taskq-*` containers.
6. **Fail closed.** If the Docker daemon is down or the image is missing, the task is requeued without using an
   attempt, you get one message, and nothing runs unsandboxed. Running a task on the host again requires an
   explicit `sandbox: off` on a tier 0 task.
7. **`deliver:` and paths.** Paths inside the container map back to the host `cwd` after the run, so
   `deliver:` files are read from the host.

**Tests (a `fake_docker.py` set as `DOCKER_BIN`, which records its argv and then runs `fake_claude`):** the
argv has every hardening flag, the token is not in it, the mounts are exactly the expected list, a tier 0 mount
is read-only, a timeout produces a kill call, a leftover container is removed at startup, a missing daemon
requeues with `attempts` unchanged and exactly one message, and `sandbox: off` on a tier 1 task is rejected.

**Done when:** the tests pass, and on the Pi `scripts/sandbox_smoke.py` runs a tier 1 example task in the real
container and shows that the file is written in the mounted folder, that `~/.ssh` and `/etc/shadow` of the host
are not visible, that a host outside the allowlist cannot be reached, and that `docker inspect` shows the memory
limit.

## Phase 13: Agents and hiring

**Goal:** different jobs get different tools, context and limits, and adding a new kind of worker is a guided,
tested, reviewed step.

1. **Agent files.** `agents/<name>.md`: frontmatter with `name`, `description`, `model`, `escalation_model`,
   `review_model`, `allowed_tools`, `mcp`, `context`, `sandbox`, `net`, `max_tier`, `max_turns` and
   `budget_usd_per_task`. The body is the role prompt, appended after the preamble so the cached prefix stays
   the same.
2. **The `agent:` task key.** The agent's values are the defaults. The task may narrow them but never exceed
   them: a task's `allowed_tools` must be a subset of the agent's, and its tier cannot exceed `max_tier`. An
   unknown agent is a validation error. The agent is recorded in `usage.jsonl` and `lessons.jsonl`.
3. **Starter agents:** `researcher` (tier 0, `WebSearch`, `WebFetch`, Sonnet), `coder` (tier 1, a worktree,
   tests), `librarian` (Phase 15, writes only to the vault's inbox). More come through the hiring flow.
4. **The hiring flow.** `/hire <need>` or the menu button. The coordinator follows `coordinator/hiring.md`,
   which asks, one question at a time: what the job is in a sentence, what comes in and goes out, which tools
   and data it needs, what it must never do, how you would judge a good result, how often, and the budget.
   It then writes `tasks/proposals/hires/<name>/` containing the draft agent file and two or three trial tasks
   (each with a `verify:`).
5. **Probation.** A deterministic script, `scripts/open_hire_pr.py`, runs the trial tasks with the candidate
   agent in the sandbox and checks the draft: the tier from `allowed_tools` is within `max_tier`, no bare
   `Bash`, and every MCP server is in the registry (Phase 14). If validation passes, it opens a PR that adds
   `agents/<name>.md` and `tests/golden/<name>/`, with pass rate, cost per task and any denied tools in the
   body. If the trials fail, you get a Telegram message saying why and no PR. You are the final gate either way.
6. **Performance review.** The weekly improver groups lessons by agent. It may propose changes to an agent's
   **body**, never its frontmatter: the opener parses the file before and after the patch and rejects any change
   in the frontmatter. An agent with three or more failures in 30 days is named in the improver's PR, and nowhere
   else if there is nothing to report.

**Tests:** a task cannot exceed its agent's tools or tier, and an unknown agent is rejected. A scripted
interview produces files that pass `dispatcher.py check`. Failing trials produce a message and no PR. A patch
that changes an agent's body passes the opener, and one that changes a `allowed_tools` line is rejected.
`lessons.jsonl` rows carry the agent.

**Done when:** the tests pass, and you hire one real agent from your phone, see its probation numbers in the PR,
and merge it.

## Phase 14: MCP expansion

**Goal:** more tools for workers, with each server's secret, risk and token cost known and limited.

1. **A registry.** `mcp/registry.json` lists each server: its name, how it runs (`npx`, a container, or a URL),
   the secret names it needs, a risk class (`read`, `write`, `external`), the lowest tier that may use it, its
   known tool names, and its measured tool-definition cost in tokens.
2. **Render, don't store.** `scripts/mcp_render.py` writes a per-task config containing only the servers the
   task or agent names, with secrets filled in from `secrets/mcp.env` (mode 0600, gitignored) for those servers
   only. The rendered file is deleted after the run. In Docker the server runs inside the same container, so its
   secret exists only for that task.
3. **Allowlist from the registry.** `mcp: [github-readonly, browser]` on a task or agent expands to the
   `mcp_config` and to explicit `mcp__<server>__<tool>` entries. A wildcard is allowed only for a server
   classed `read`. The reviewer still never gets MCP.
4. **Servers, in order of value and safety:** the fetch server (exists), GitHub with a read-only fine-grained
   token, SQLite over the vault index (Phase 15), a browser (Playwright) in its own container on the proxied
   network and tier 1 or above, and n8n's MCP server so a worker can start a named workflow. For Google
   services (Drive, Calendar, Gmail), keep the credentials in n8n and expose only specific workflows to workers,
   instead of giving workers the Google tokens.
5. **Count the cost.** `scripts/mcp_cost.py` measures a server's tool-definition tokens by running a trivial
   prompt with and without it, and writes the number into the registry. Phase 9's context alert includes it.

**Tests:** a rendered config has exactly the named servers and secrets. An unregistered server, a tier below the
server's minimum, or a wildcard on a write-class server is rejected. A secret never appears in argv, in
`logs/`, or in the saved task file. The rendered config is gone after the run.

**Done when:** the tests pass, and a real task in a container uses the read-only GitHub server to list issues,
and the proxy log shows only the expected hosts.

## Phase 15: Second brain

**Goal:** agents start with the facts you would give them, kept in a place you can read, diff and review. This
extends Phase 8.

1. **Git is the source of truth.** A separate private repo, `brain`, cloned to `~taskq/brain`. Plain markdown
   with a small index note, a `decisions/` folder (what, why, what was rejected) and a glossary. It is mounted
   read-only at `/brain` in worker containers. Folders with client or NDA material are on a denylist and are
   never mounted: agents get the specific folders listed in their `context_roots`.
2. **A `context:` frontmatter key** lists vault notes for a task. `validate_task` checks each resolves inside
   the agent's `context_roots` and is not denylisted. The report shows which notes the agent saw.
3. **An inbox.** `brain/inbox/` is where anything from outside lands: Drive exports, clips, notes from n8n. The
   `librarian` agent files inbox items into proper notes and opens a PR on the brain repo. Workers can write
   nowhere else in the vault.
4. **Drive ingest, one direction.** An n8n workflow watches a "Brain Inbox" Drive folder, exports each file as
   markdown (confirm Docs export to markdown; use plain text otherwise) into the inbox, and enqueues a
   `librarian` task. Nothing writes back to Drive from a worker. If you want results in Drive, an n8n workflow
   on the task callback uploads the `deliver:` files. If n8n turns out to be too heavy, `rclone` on a timer
   does the same job.
5. **Search that costs nothing.** `scripts/brain_index.py` builds a SQLite FTS5 index (standard library) into
   `brain/.index/` (ignored). `scripts/brain_search.py` returns the top paths with snippets and is allowed as
   `Bash(python3 scripts/brain_search.py*)`. `Grep` remains.
6. **Write-back through pull requests only,** on the brain repo, from the librarian and the improver.
7. **Measure it.** Golden tasks run with and without `context:`. A note that doesn't raise the pass rate is a
   candidate for removal.

**Tests, on a small fixture vault:** a context path outside the roots or on the denylist is rejected. The
denylisted folder is not in the mount list. Ten known queries return the expected note in the top 3. An inbox
import produces a diff only inside `inbox/`. A task with `context:` shows the notes in its report.

**Done when:** the tests pass, the golden pass rate with context is at least the baseline, and a document dropped
into the Drive folder appears as a PR in the brain repo within ten minutes.

## Spike A: Would an embedding model help?

**Question:** does semantic search find things the FTS5 index misses, enough to justify a model on the Pi?

**Where it could help:** finding notes when the wording differs, grouping lessons by cause, and matching an
idea to an existing agent or skill. **Why it may not be needed:** at the size of a personal vault, full-text
search plus a good index note usually finds it. This is a half-session experiment, not a build.

**Method:**
1. Write 20 to 30 real questions from your work, each with one to three notes that answer it.
2. Compare Grep, FTS5 with BM25 ranking, a small local embedding model (a bge-small or nomic-embed class model
   through ONNX or Ollama), and, only if you allow your notes to go to a third party, one hosted embedding API.
3. Record recall at 5 and MRR for each, index build time and storage on the Pi, query latency, and RAM.
4. For lessons, cluster 100 lessons by embedding and compare to labels you assign by hand.

**Decision rule:** adopt only if recall at 5 beats FTS5 by at least 15 points, a query takes under a second,
and the model needs under 500 MB resident. If adopted, store vectors in SQLite and compare by brute force
(a few thousand notes is small), and combine with FTS5 scores. No vector database server. The result goes in
`docs/spikes/embeddings.md` either way, with the numbers.

## Spike B: A small on-host model

**Question:** can a 1 to 4 billion parameter model on the Pi take over small jobs and save calls to the main
model?

**Candidate jobs, best first:** classifying a Telegram message (status question, new task, menu action, chat) so
the coordinator skips a Sonnet call for the first and last; triaging events for Phase 18 (ignore, digest,
notify, act); labelling lessons by cause; summarizing tool output before the main model sees it. **Not for:** writing code, judging acceptance criteria, or anything that uses tools.

**Method:**
1. Run a quantized model (Qwen2.5-3B, Llama-3.2-3B or Gemma-3 class) in a container through llama.cpp or
   Ollama, on the Pi's CPU.
2. Measure tokens per second for reading and for writing, resident memory, and the effect on the dispatcher
   while it runs. My expectation is single-digit tokens per second for a 3B model at 4-bit, which is fine for
   short classification and too slow for anything long. Treat that as a guess until measured.
3. Label 30 to 50 real coordinator messages by hand and score the model on them.
4. From `coordinator.log`, estimate how many main-model calls per week it would replace.

**Decision rule:** keep it only if agreement with your labels is at least 95%, the 95th percentile latency is
under 5 seconds, it stays within 2.5 GB, and it removes a meaningful share of coordinator calls (state the
number). Otherwise record "not worth it" in `docs/spikes/local-model.md`. If kept, it plugs in as
`runner: local` in Phase 16 (an OpenAI-compatible local endpoint), limited to tier 0 tasks with no tools.

## Phase 16: Other models

**Goal:** use the cheapest capable model for each job, and keep working when one provider's limit is hit.

1. **A runner interface.** Move the building of the command line and the parsing of the result behind one small
   interface, `runners/claude.py`, `runners/gemini.py`, `runners/local.py`. A runner returns the text, the
   session id, tokens, a cost if it has one, and whether the call hit a limit or an auth failure. The Claude
   runner is today's code unchanged: the existing test suite must pass with no edits.
2. **Task keys.** `runner:` and `review_runner:` (default `claude`), plus an optional `fallback_runner:`.
   Agents can set them too. `usage.jsonl` rows gain `runner`. For a subscription runner with no dollar cost, log
   tokens and requests, and track the budget in those units.
3. **Separate pauses.** The pause state becomes `state/paused_until.<runner>`. A Claude limit no longer stops a
   task that can run on Gemini, and the dispatcher picks the next task whose runner is available. This is the
   biggest single saving when you hit the plan limit.
4. **Gemini.** First, a spike inside the phase: run ten representative tasks and compare quality, time and
   quota use. Before building, confirm with `gemini --help` and the current docs: the headless flags and
   output format, how tools and approvals are restricted, MCP support, the plan's request limits, and whether
   the Google plan's terms allow unattended use. None of that is verified here.
5. **Gemini is sandbox-only.** Its permission model is not `allowedTools`, so the runner refuses to start
   unless `sandbox` is `docker`, and tier 0 tasks get a read-only mount. This is enforced in `validate_task`.
   Good first uses: bulk reading, long-document summaries, web research, and a reviewer from another model
   family for work done by Claude, since two models of the same family tend to share blind spots.
6. **Routing.** `models.json` grows to role by task class, mapping to a runner and a model. A route is promoted
   only when `scripts/golden.py` shows a pass rate at least equal to the baseline and a lower cost per pass. The
   weekly improver may propose route changes as a PR, with those numbers in the body.

**Tests (with `fake_gemini`):** the Claude runner behaves exactly as before. A task picks its runner and
falls back when the first is paused. A Claude pause does not block a Gemini task, and the reverse. Gemini with
`sandbox: off` is rejected. Usage rows have the same shape for every runner. The golden table has one column per
route.

**Done when:** the tests pass, a real research task runs on Gemini inside a container and passes its
`verify:`, and a fake Claude pause lets the queue finish its Gemini-eligible tasks.

## Phase 17: Voice

**Goal:** talk to it and hear it answer, starting with what Telegram already carries, and make it feel quick.

1. **Voice notes in (issue #8).** The bot handles `voice` and `audio` messages: `getFile`, download the
   Opus/OGG file, transcribe on the Pi with whisper.cpp (a base or small English model, quantized) through
   `WHISPER_BIN`. The transcript goes to the coordinator exactly like typed text, marked `[voice]`, and the
   reply starts by echoing what it heard so a mishearing is obvious. Audio is deleted after
   `VOICE_RETENTION_HOURS` (default 24). The sender check applies. Measure the real-time factor on the Pi
   before choosing the model size (I expect the base model to be close to real time, which needs checking).
2. **Spoken replies out.** When your message was a voice note, or after `/voice on`, the coordinator also sends
   a short spoken summary: text to speech with Piper through `TTS_BIN`, converted to Opus with ffmpeg, sent
   with `sendVoice`. The full text still arrives as a message. Long reports are never read aloud, only their
   one-line summary.
3. **Feel quick.** Send an acknowledgement within a second and edit it into the answer with `editMessageText`.
   Answer "what's running?" and "how much today?" straight from the state files and `usage.jsonl` with no model
   call. Measure how long a plain `claude -p --resume` turn takes from the log, and then test whether keeping
   one process open with streaming JSON input (check `claude --help` for `--input-format stream-json`) removes
   the start-up cost. Keep it only if it is faster and survives a restart without losing the session.
4. **A hands-free device (later, only if 1 to 3 earn their keep).** A push-to-talk button first, on the Pi or a
   phone page, then a local wake word (openWakeWord class). The microphone is off by default, audio never
   leaves the house, and only the transcript reaches the model.

**Tests (doubles for `WHISPER_BIN`, `TTS_BIN` and ffmpeg):** a voice update becomes the same call as the
equivalent text, marked `[voice]`, and the reply begins with the transcript. A voice note from the wrong sender
is ignored and the audio is not downloaded. Audio files older than the retention are deleted. A text message
gets no audio back unless `/voice on` is set. A status question makes zero `claude` calls. An acknowledgement
is sent before the model call returns.

**Done when:** the tests pass, and on your phone you say a task, see it transcribed, and hear a one-line status
back.

## Phase 18: Proactive

**Goal:** it speaks first, rarely, and only when it matters. A morning brief that is useful, and interruptions
you rate as worth it.

1. **Watchers in n8n, with no model.** Calendar (next events, conflicts), mail (new messages from people you
   name, anything flagged), feeds, GitHub notifications, price trackers (your laptop-deal tasks become watchers),
   weather, and the lab's alerts from Phase 7. Each emits an event to the intake with
   `source: n8n:<watcher>`. Credentials stay in n8n.
2. **Triage events, cheaply.** In order: n8n filters (plain rules), then a classifier into `ignore`, `digest`,
   `notify`, `act`. Use Haiku until Spike B says a local model can do it. Events that need work become ordinary
   queue tasks at the source's tier.
3. **An attention budget.** `attention.json` sets quiet hours, a per-source urgency, and `NOTIFY_PER_DAY` (default
   5). Past the budget, notifications fall into the digest. Quiet hours hold everything except sources marked
   `urgent` (for example a failing service).
4. **Learn from reactions.** Each notification has 👍 and 👎 buttons. Ratings are stored next to the event and
   feed the weekly improver, which proposes changes to the rules and to `attention.json` as a PR. This is the
   reactions idea from the learning loop, applied to attention.
5. **The morning brief.** One scheduled call, `quiet: true`, assembles the day: calendar, overnight events, queue
   results, items waiting on you, and yesterday's spend. It is sent as text and, with Phase 17, as a short voice
   message. If there is nothing worth saying, it sends nothing. An evening wrap-up is optional.
6. **Standing instructions in plain language.** "Tell me if the lab goes down while I'm out", "watch for a
   Framework 13 under $800". The coordinator turns it into a watcher or a recurring task, shows you the
   definition, and installs it as a PR or after your 👍. `/menu` lists active ones and removes them.

**Tests:** events reach the intake with the right source and profile. The fake classifier's four outcomes route
correctly. The sixth `notify` in a day goes to the digest, and quiet hours hold a non-urgent event until
morning. The brief is silent with nothing to report. A 👎 is stored with the event and appears in the next
improver input. A standing instruction creates a definition that passes `check`.

**Done when:** the tests pass, and over one real week you receive one brief a day, at most five interruptions a
day, and you rate at least 70% of the interruptions as useful.

## Phase 19: Acting for you

**Goal:** it takes the small, reversible actions itself, shows its work, and earns more reach one class at a
time. This is the autonomy ladder from the north star made real.

1. **`autonomy.json`.** Each action class (for example `mail.archive-newsletter`, `calendar.accept-known`,
   `home.scene`, `vault.file-note`) has a level from 0 to 3. New classes start at 1. Levels are changed by you,
   never by the model, through a menu button or a PR.
2. **Promotion and demotion.** After `PROMOTE_AFTER` clean approvals (default 10) the coordinator offers one
   promotion with the evidence. Any undo, 👎, or failed action in a class drops it one level at once and tells
   you.
3. **Never-promote list, enforced in code.** Spending money, deleting data, sending as you to a new recipient,
   changing a password or permission, and any lock, alarm, camera or garage action. These stay at level 1 and
   always need a tap. The rule lives in a function with a test, not in a prompt.
4. **Audit trail and undo.** Every action writes a row to `logs/actions.jsonl` (class, inputs, result, level,
   undo recipe). The message that reports it carries an Undo button (the Phase 10 buttons). `/actions` shows the
   last 24 hours.
5. **The first classes, chosen for being reversible:** archive or label mail (never delete or send), draft
   replies into the drafts folder, propose calendar changes and accept invites from approved senders, file inbox
   notes in the vault, and Home Assistant scenes for lights and media.
6. **Home Assistant.** Use its MCP server or API behind a registry entry (Phase 14), with a long-lived token
   scoped to an allowlist of exposed entities. Confirm the current integration and its options before
   building. Security devices are outside the allowlist.
7. **Plan versus act.** Anything above level 1 runs through the same sandbox, tiers and budgets as every task.
   The model proposes an action as structured JSON, and a small deterministic executor checks it against the class
   and level, then performs it. The model never holds the credential.

**Tests:** a class cannot reach level 2 before the required approvals. One undo demotes a class. The
never-promote list cannot be raised by the file, by a menu button or by a PR. Every action writes an audit row
with an undo recipe, and the undo works. A Home Assistant call outside the allowlist is refused by the
executor. A model-proposed action for an unknown class is rejected.

**Done when:** the tests pass, and one real level 2 class runs for two weeks with no undos, or the coordinator
never offers the promotion and you decide whether the class belongs on the ladder.

## Risks to watch

- **Prompt injection through n8n and Drive.** Anything that reaches a worker from outside is untrusted. The
  defenses are the source profile's tool ceiling, the sandbox, and the fence. The fence only helps, it does not
  protect.
- **Secrets in containers.** MCP and runner credentials live in a container for one task. Scope every token to
  the least it needs, and never put one in argv, a log, or a task file.
- **Memory.** The 8 GB host cannot run everything at once. Keep one worker at a time, and treat the local
  model as optional.
- **A microphone in the house.** Voice stays push-to-talk and Telegram-based until you decide otherwise. A wake
  word, if you add one, runs locally, keeps no audio, and has a hardware mute.
- **Acting on the physical world.** Home and security actions are the highest-stakes part. They sit behind the
  never-promote list, an entity allowlist and an executor that checks every action, and the model never holds the
  credential.
- **Notification fatigue.** A proactive assistant that interrupts too often gets muted, and then it is useless.
  The attention budget and the useful-or-not ratings are the control, and the goal is fewer, better messages.
- **Terms of use for subscription logins.** Using a consumer plan login for unattended automation (already true
  for the Claude token) can be restricted. Check the current terms before relying on it.
- **Plan drift.** Settings and flags named here (`--max-turns`, the Gemini flags, Docs export to markdown)
  were written from memory of the tools and need confirming against `--help` or the docs when each phase starts.

## Not in this roadmap

Photos (#8) and outbound phone calls (#11) stay where `PLAN.md` left them. Voice notes, the other half of #8,
are Phase 17. Issue #12 (non-Claude models) is now Phase 16. Phases 6 (beyond 6a), 7 and 8 stay as planned in `PLAN.md`, except that Phase 15
extends Phase 8.
