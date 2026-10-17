#!/usr/bin/env python3
"""Claude task queue dispatcher.

Cron-driven, single-file task queue for headless Claude Code (`claude -p`).
Picks one markdown task from tasks/pending/, runs it with the model and tool
allowlist declared in its YAML frontmatter, has a cheap model review the
result against the task's "## Acceptance Criteria" section, and either
requeues the task with feedback (up to max_attempts, escalating the model
after the first failure) or moves it to tasks/done/ and notifies via
Telegram. Templates in tasks/recurring/ with a "schedule" frontmatter key
spawn one-shot instances into pending/ whenever they come due.

A usage limit pauses the whole queue (state/paused_until), an expired login
stops it (state/auth_failed) until a cheap probe call works again, and a
review that cannot run keeps the worker's finished report instead of
discarding it. Reports too long for one message go out as a preview plus the
full text as an attachment, and a task's `deliver:` files are sent as documents.
A task's `verify:` command is run by the dispatcher itself (no shell, scrubbed
environment, allowlisted prefixes) between the worker and the reviewer: a
non-zero exit fails the attempt without a review call, and with `review: skip`
a zero exit ends the task.
Libraries: tenacity (retries), filelock (shared logs) and httpx (uploads);
install them with `pip install --require-hashes -r requirements.txt` in a venv.

Usage: python3 dispatcher.py                   (typically from cron every 15 minutes)
       python3 dispatcher.py cancel <task-id>  (archive a queued task to tasks/cancelled/)
       python3 dispatcher.py retry <task-id>   (requeue a failed/cancelled task, attempts reset)
       python3 dispatcher.py check <file>      (validate a task or recurring template file)
       python3 dispatcher.py usage             (claude calls, cost and turns by day and model)
"""

import difflib
import fcntl
import fnmatch
import functools
import hashlib
import html
import json
import logging
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

try:
    import filelock
    import httpx
    import tenacity
except ImportError as exc:  # cron has no terminal: say what to do in the one place it will be seen
    # The README, the coordinator and cron all say `python3 dispatcher.py ...`, but the libraries live in the
    # queue's own .venv. Hand over to it once (`-X taskq_venv` marks the second run so a broken venv cannot
    # loop), and only when run as a script: the Telegram bridge imports this module and must not be replaced.
    _venv_python = Path(__file__).resolve().parent / ".venv" / "bin" / "python3"
    if __name__ == "__main__" and "taskq_venv" not in sys._xoptions and _venv_python.exists():
        os.execv(str(_venv_python), [str(_venv_python), "-X", "taskq_venv", *sys.argv])
    sys.exit(
        f"dispatcher.py needs the libraries in requirements.txt ({exc}).\n"
        "Install them in a venv and run the queue with that venv's python:\n"
        "  python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.txt"
    )

BASE = Path(__file__).resolve().parent
TASKS = BASE / "tasks"
PENDING = TASKS / "pending"
ACTIVE = TASKS / "active"
DONE = TASKS / "done"
FAILED = TASKS / "failed"
RECURRING = TASKS / "recurring"
CANCELLED = TASKS / "cancelled"
PROPOSED = TASKS / "proposed"  # waiting for your 👍 before they may run
LOGS = BASE / "logs"
STATE = BASE / "state"
WORKTREES = BASE / "worktrees"  # one git worktree per task that sets repo:
APPROVALS = STATE / "approvals"  # one <task>.json per task that needed approval; claude cannot write state/
LOCKFILE = BASE / "dispatcher.lock"
PAUSED_FILE = STATE / "paused_until"  # first line: ISO UTC time; second line: why
AUTH_FAILED_FILE = STATE / "auth_failed"  # present while the claude login is known to be expired
SEEN_FILE = STATE / "seen.json"  # problems already reported, so each is said once
USAGE_LOG = LOGS / "usage.jsonl"
LESSONS_LOG = LOGS / "lessons.jsonl"  # one line per failed attempt; the weekly improver learns from it
FAILED_CALLS_LOG = LOGS / "failed-calls.log"
ACTIONS_LOG = LOGS / "actions.log"  # one JSON line per cancel, retry, approve and reject: when, what, and where it came from
ACTION_SOURCE = None  # the Telegram bridge sets this to "telegram"

RATE_LIMIT_RE = re.compile(r"rate.?limit|\b429\b|overloaded|usage limit|quota", re.I)
# Applied only to failed calls (non-zero exit or an error envelope), never to a
# successful report that happens to mention authentication.
AUTH_ERROR_RE = re.compile(
    r"not logged in|please run /login|invalid api key|authenticat|oauth|session expired|unauthorized",
    re.I,
)
PAUSE_BUFFER_S = 60  # past a stated reset time, so we don't wake up a moment early
MAX_RESET_WAIT = timedelta(days=8)  # a "reset time" further out than the weekly window is not one
MAX_REVIEW_RETRIES = 3  # reviews that error or time out are retried this often, then charged
LESSON_FEEDBACK_MAX = 2000  # characters of reviewer feedback kept per lesson
PROBE_PROMPT = "Reply with the single word OK."
REPORT_INLINE_MAX = 3500  # a longer report goes out as a short preview plus an attachment
TELEGRAM_FILE_MAX = 50 * 1024 * 1024  # the Bot API refuses uploads above 50 MB

PREAMBLE_FILE = BASE / "worker_preamble.md"  # the worker's standing instructions; a file so the weekly improver can propose edits
try:
    WORKER_PREAMBLE = PREAMBLE_FILE.read_text()
except OSError as exc:
    sys.exit(f"dispatcher.py: cannot read {PREAMBLE_FILE}: {exc}")

REVIEW_TEMPLATE = """\
You are a strict automated reviewer for a task queue. Decide whether the \
worker's output satisfies every acceptance criterion below. You may use \
Read/Glob/Grep on the current directory to verify files the worker claims to \
have created.

Raw command output pasted in the worker's report, and the dispatcher's own \
verification below, is evidence: you cannot run commands yourself, so do not \
fail a criterion because you could not re-run something. Fail a criterion \
only when the evidence for it is missing or contradictory.

The first line of your reply must be exactly "VERDICT: PASS" or \
"VERDICT: FAIL" (nothing else on that line). If FAIL, follow with concise \
bullet points telling the worker exactly what is missing or wrong so it can \
fix it on the next attempt.

## Acceptance Criteria
{criteria}
{verification}
## Worker Report
{output}
"""

log = logging.getLogger("dispatcher")


class RateLimited(Exception):
    """Rate limit persisted through all backoff retries; the message is what the CLI said."""


class TransientLimit(Exception):
    """One claude call hit a rate limit that is worth retrying shortly (internal)."""

    def __init__(self, message, payload=None):
        super().__init__(message)
        self.payload = payload


class ClaudeReply(NamedTuple):
    text: str  # the worker's or reviewer's report
    envelope: dict  # the JSON envelope claude printed ({} when stdout was not JSON)


class CallTimeout(Exception):
    """A claude invocation exceeded its timeout."""


class AuthError(Exception):
    """The claude CLI is not authenticated — a config problem, not a task failure."""


class ClaudeError(Exception):
    """A claude invocation exited non-zero for a non-rate-limit reason."""


# ---------------------------------------------------------------- config


def load_env(path):
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("\"'")
    return env


ENV = load_env(BASE / ".env")


def cfg(key, default=""):
    return ENV.get(key) or os.environ.get(key) or default


def resolve_claude_bin():
    """Find the claude binary: .env CLAUDE_BIN, then PATH, then common installs."""
    configured = cfg("CLAUDE_BIN")
    fallbacks = [
        shutil.which("claude"),
        str(Path.home() / ".local" / "bin" / "claude"),  # claude.ai install.sh default
        "/usr/local/bin/claude",
    ]
    for candidate in ([configured] if configured else []) + fallbacks:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            if configured and candidate != configured:
                log.warning("CLAUDE_BIN=%s does not exist; using %s instead", configured, candidate)
            return candidate
    return None


# ---------------------------------------------------------------- task files


def unquote(value):
    """Drop one pair of matching quotes around a frontmatter value; a lone quote is part of the value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_task(path):
    """Split a task file into (frontmatter dict, body). Flat key: value only."""
    text = path.read_text()
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not match:
        return None, text
    meta = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = unquote(value.strip())
    return meta, match.group(2)


def write_task(path, meta, body):
    front = "\n".join(f"{k}: {v}" for k, v in meta.items())
    write_atomic(path, f"---\n{front}\n---\n{body}")


def mcp_config_path(meta):
    """Resolve the task's MCP config: frontmatter, then .env default. None if unset."""
    raw = (meta or {}).get("mcp_config") or cfg("DEFAULT_MCP_CONFIG")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else BASE / path


def task_cwd(meta):
    return meta.get("cwd") or cfg("DEFAULT_CWD") or str(BASE / "workspace")


def deliver_refs(meta):
    """The paths in a task's `deliver:` line (comma separated, relative to its cwd)."""
    return [ref.strip() for ref in (meta.get("deliver") or "").split(",") if ref.strip()]


def collect_deliverables(meta, cwd):
    """(files to send, notes on those skipped) for a passed task's `deliver:` line.

    Paths are resolved first, so a symlink or `..` cannot reach outside cwd.
    """
    root = Path(cwd).resolve()
    files, notes = [], []
    for ref in deliver_refs(meta):
        path = (root / ref).resolve()
        if not path.is_relative_to(root):
            notes.append(f"{ref}: outside the working directory, not sent")
        elif not path.is_file():
            notes.append(f"{ref}: not found")
        elif path.stat().st_size > TELEGRAM_FILE_MAX:
            notes.append(f"{ref}: over Telegram's 50 MB limit, not sent")
        elif path not in files:
            files.append(path)
    return files, notes


def extract_criteria(body):
    match = re.search(
        r"^##\s*Acceptance Criteria\s*\n(.*?)(?=^##\s|\Z)", body, re.S | re.M
    )
    return match.group(1).strip() if match else None


VALUE_CLASSES = ("deliverable", "research", "verification", "admin")
SCHEDULE_FORMS = "every 30m|6h|2d (any number), daily at 06:30, weekly on mon at 09:00"


def _whole_number(minimum):
    def check(value):
        try:
            number = int(value)
        except ValueError:
            return "must be an integer"
        return None if number >= minimum else f"must be at least {minimum}"
    return check


def _positive_number(value):
    try:
        number = float(value)
    except ValueError:
        return "must be a number"
    return None if number > 0 else "must be greater than 0"


# Every frontmatter key a task file may carry: (required, only on recurring
# templates, rule). A rule is None (any text), a tuple of allowed values, or a
# function returning a problem string (None when the value is fine).
FRONTMATTER_KEYS = {
    "model": (True, False, None),
    "review_model": (True, False, None),
    "max_attempts": (True, False, _whole_number(1)),
    "escalation_model": (False, False, None),
    "attempts": (False, False, _whole_number(0)),  # managed by the dispatcher
    "timeout_minutes": (False, False, _positive_number),
    "allowed_tools": (False, False, None),
    "mcp_config": (False, False, None),
    "depends_on": (False, False, None),
    "cwd": (False, False, None),
    "deliver": (False, False, None),
    "verify": (False, False, None),  # checked by verify_problem()
    "review": (False, False, ("skip",)),
    "quiet": (False, False, ("true", "false")),  # true: no "Task done" report on a pass; failures still report
    "value_class": (False, False, VALUE_CLASSES),  # may be absent: older files read as untagged
    "pending_review": (False, False, None),  # managed by the dispatcher
    "review_failures": (False, False, None),  # managed by the dispatcher
    "tier": (False, False, ("0", "1", "2")),  # 0 reads, 1 writes in its workspace, 2 leaves the box; computed, see tier_of()
    "repo": (False, False, None),  # a git checkout (name under REPOS_DIR or absolute path): worktree + draft pull request
    "base": (False, False, None),  # branch the task branch starts from (default main)
    "propose": (False, False, ("true", "false")),  # true: the worker may write follow-up proposals
    "branch": (False, False, None),  # managed by the dispatcher
    "pr_url": (False, False, None),  # managed by the dispatcher
    "pending_publish": (False, False, None),  # managed by the dispatcher
    "held_until": (False, False, None),  # managed by the dispatcher: not before this time
    "publish_failures": (False, False, None),  # managed by the dispatcher
    "publish_blocked": (False, False, None),  # managed by the dispatcher: the credentials cannot publish; wait for the probe
    "proposed_by": (False, False, None),  # managed by the dispatcher
    "depth": (False, False, _whole_number(0)),  # managed by the dispatcher
    "schedule": (False, True, None),  # checked by check_schedule()
    "last_run": (False, True, None),  # managed by the dispatcher
}


def verify_prefixes():
    raw = cfg("VERIFY_ALLOWED_PREFIXES", "python3 -m unittest,pytest,bash scripts/verify-")
    return [prefix.strip() for prefix in raw.split(",") if prefix.strip()]


def verify_problem(command):
    """Why a `verify:` command may not run (None when it may). No shell is involved: the command is split with shlex.

    An allowed prefix is compared token by token. Its last token must match
    exactly, unless it ends in "-" or "/": then it is the start of a token
    (so `scripts/verify-` allows `scripts/verify-x.sh`, but `pytest` does not
    allow `pytest-evil`).
    """
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return f"verify command cannot be parsed ({exc})"
    if not tokens:
        return "verify command is empty"
    for prefix in verify_prefixes():
        head = shlex.split(prefix)
        if len(tokens) < len(head) or tokens[:len(head) - 1] != head[:-1]:
            continue
        last, wanted = tokens[len(head) - 1], head[-1]
        if last == wanted or (wanted.endswith(("-", "/")) and last.startswith(wanted)):
            return None
    return (
        f"verify command {command!r} does not start with an allowed prefix "
        f"(VERIFY_ALLOWED_PREFIXES: {', '.join(verify_prefixes()) or 'none'})"
    )


def check_schedule(spec):
    """Raise ValueError unless `spec` is a schedule is_due() understands."""
    is_due(spec, None, datetime.now())


def frontmatter_problems(meta, template):
    problems = []
    for key, value in meta.items():
        if key not in FRONTMATTER_KEYS:
            hint = difflib.get_close_matches(key, FRONTMATTER_KEYS, n=1)
            problems.append(
                f"unknown frontmatter key '{key}'" + (f" (did you mean '{hint[0]}'?)" if hint else "")
            )
            continue
        required, template_only, rule = FRONTMATTER_KEYS[key]
        if template_only and not template:
            problems.append(f"'{key}' only belongs in tasks/recurring/ templates")
        if not value or rule is None:
            continue
        if isinstance(rule, tuple):
            if value not in rule:
                problems.append(f"{key} must be one of {', '.join(rule)} (got '{value}')")
        elif (message := rule(value)) is not None:
            problems.append(f"{key} {message}")
    for key, (required, template_only, _) in FRONTMATTER_KEYS.items():
        if required and not meta.get(key):
            problems.append(f"frontmatter is missing required key '{key}'")
    return problems


DEFAULT_TASK_TOOLS = (
    "Read,Glob,Grep,Edit,Write,Write(tasks/proposals/**),WebFetch,WebSearch,Skill(*),mcp__*,"
    "Bash(python3 *),Bash(git *),Bash(gh issue *),Bash(gh pr create*),"
    "Bash(df *),Bash(du *),Bash(uptime),Bash(free *)"
)


def split_tools(raw):
    """Split a tool list on the commas outside parentheses: Bash(a, b) is one entry."""
    items, depth, current = [], 0, ""
    for char in raw:
        depth += (char == "(") - (char == ")")
        if char == "," and depth <= 0:
            items.append(current.strip())
            current = ""
        else:
            current += char
    items.append(current.strip())
    return [item for item in items if item]


def allowed_tools_problem(value):
    """A task may only grant tools matching TASK_ALLOWED_TOOLS (fnmatch patterns).

    The default has no `Bash(gh *)` or `gh api`, so a task can't hand itself the
    whole GitHub API; it can open issues and pull requests.
    """
    patterns = split_tools(cfg("TASK_ALLOWED_TOOLS", DEFAULT_TASK_TOOLS))
    refused = [
        tool for tool in split_tools(value)
        if tool not in patterns and not any(fnmatch.fnmatchcase(tool, pattern) for pattern in patterns)
    ]
    if refused:
        return f"allowed_tools grants {', '.join(refused)}, which TASK_ALLOWED_TOOLS does not allow"
    return None


# ------------------------------------------------------------ autonomy tiers
#
# Tier 0 reads, tier 1 writes inside the task's own workspace, tier 2 leaves the
# box (push, pull requests, ssh, deploys). The dispatcher computes the tier from
# allowed_tools, so a task file can't claim a lower one.

READ_TOOLS = {"Read", "Glob", "Grep", "WebSearch", "WebFetch", "Skill"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
READ_ONLY_MCP = re.compile(r"^(fetch|get|list|search|read|query|find|describe|view)(_|-|$)")
BASH_READ_ONLY = {"df", "du", "uptime", "free", "ls", "cat", "head", "tail", "wc", "pwd", "date"}
BASH_OFF_BOX = {"ssh", "scp", "rsync", "curl", "wget", "nc", "kubectl", "docker", "systemctl", "sudo", "gh"}
GH_NEVER = ("gh api", "gh pr merge", "gh pr ready", "gh repo", "gh auth", "gh workflow")
GH_SAMPLES = ("gh api x", "gh pr merge 1", "gh pr ready 1", "gh repo delete x", "gh auth login", "gh workflow run x")


def bash_inner(tool):
    """The command pattern inside Bash(...), or None when the tool is not a Bash grant."""
    if tool.startswith("Bash(") and tool.endswith(")"):
        return tool[5:-1].strip()
    return None


def bash_tier(inner):
    words = inner.split()
    if not words:
        return 2
    if any(inner.startswith(extra) for extra in split_tools(cfg("TIER2_BASH"))):
        return 2
    first = words[0].removeprefix("./").rstrip("*?")
    if any(char in first for char in "*?[") or not first:
        return 2  # too broad to know what it runs
    if first in BASH_OFF_BOX or first.startswith("deploy"):
        return 2
    if first == "git":
        sub = words[1] if len(words) > 1 else ""
        stripped = sub.rstrip("*?")
        if stripped.startswith("push") or (stripped and sub != stripped and "push".startswith(stripped)):
            return 2
        return 1
    if first in ("python3", "python", "pytest"):
        return 1
    if first == "bash":
        return 1 if len(words) > 1 and words[1].startswith("scripts/verify-") else 2
    return 0 if first in BASH_READ_ONLY else 2


def tool_tier(tool):
    inner = bash_inner(tool)
    if inner is not None:
        return bash_tier(inner)
    name = tool.split("(")[0]
    if name.startswith("mcp__"):
        parts = name.split("__")
        return 0 if len(parts) > 2 and READ_ONLY_MCP.match(parts[-1]) else 2
    if name in READ_TOOLS:
        return 0
    if name in WRITE_TOOLS:
        return 1
    return 2  # Bash with no pattern, or a tool we have never heard of


def task_tools(meta):
    return split_tools(meta.get("allowed_tools") or cfg("DEFAULT_ALLOWED_TOOLS", "Read,Glob,Grep,Edit,Write"))


def tier_of(meta):
    if meta.get("repo"):
        return 2  # the dispatcher itself pushes the branch and opens the pull request
    return max((tool_tier(tool) for tool in task_tools(meta)), default=0)


def never_granted(tool):
    """Why no task may hold this tool, whatever TASK_ALLOWED_TOOLS says (None when it is fine)."""
    if tool == "Bash":
        return "bare Bash runs anything"
    inner = bash_inner(tool)
    if inner is None:
        return None
    if inner in ("", "*"):
        return "Bash(*) runs anything"
    if inner.startswith(GH_NEVER) or any(fnmatch.fnmatchcase(sample, inner) for sample in GH_SAMPLES):
        return "it reaches the GitHub API or changes a repository beyond opening an issue or draft pull request"
    if re.search(r"git push\b.*\s(-f|--force)", inner):
        return "force-pushing rewrites history"
    return None


def autonomy_problems(meta):
    """Problems with a task's tools, tier and repo that no allowlist can excuse."""
    problems = []
    tools = split_tools(meta.get("allowed_tools") or "")
    for tool in tools:
        if reason := never_granted(tool):
            problems.append(f"allowed_tools grants {tool}, which no task may have ({reason})")
    if meta.get("repo"):
        if meta.get("cwd"):
            problems.append("repo and cwd are mutually exclusive: a repo task runs in its own worktree")
        for tool in tools:
            if bash_inner(tool) is not None and bash_tier(bash_inner(tool)) == 2 and not never_granted(tool):
                problems.append(
                    f"a task with repo: may not grant {tool}; the dispatcher pushes the branch and opens the pull request"
                )
    declared = meta.get("tier")
    if declared and declared != str(tier_of(meta)):
        problems.append(f"tier {declared} does not match the tier {tier_of(meta)} its allowed_tools work out to")
    return problems


def validate_task(meta, body, template=False):
    problems = []
    if meta is None:
        return ["missing YAML frontmatter (--- block at top of file)"]
    problems += frontmatter_problems(meta, template)
    if extract_criteria(body) is None:
        problems.append("no '## Acceptance Criteria' section in task body")
    if meta.get("allowed_tools") and (problem := allowed_tools_problem(meta["allowed_tools"])):
        problems.append(problem)
    problems += autonomy_problems(meta)
    if problem := repo_problem(meta):
        problems.append(problem)
    mcp = mcp_config_path(meta)
    if mcp is not None and not mcp.is_file():
        problems.append(f"mcp_config file not found: {mcp}")
    cwd = os.path.normpath(task_cwd(meta))
    for ref in deliver_refs(meta):  # a clear reject before any claude call; symlinks are checked on delivery
        if not Path(os.path.normpath(os.path.join(cwd, ref))).is_relative_to(cwd):
            problems.append(f"deliver path '{ref}' is outside the task's cwd")
    if meta.get("verify") and (problem := verify_problem(meta["verify"])):
        problems.append(problem)
    if meta.get("review") == "skip" and not meta.get("verify"):
        problems.append("review: skip needs a verify command; nothing else would check the result")
    if template:
        if not meta.get("schedule"):
            problems.append("recurring template has no 'schedule' key")
        else:
            try:
                check_schedule(meta["schedule"])
            except ValueError as exc:
                problems.append(f"{exc}; supported: {SCHEDULE_FORMS}")
    return problems


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tail(text, limit=2000):
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


# ---------------------------------------------------------------- shared files


ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def write_atomic(path, text):
    """Replace a file in one step: a reader (or a power cut) sees the old content or the new, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())  # a power cut after the rename must not leave an empty file
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def append_locked(path, text):
    """Append to a log that both the dispatcher and the coordinator bridge write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with filelock.FileLock(f"{path}.lock"):
        with open(path, "a") as fh:
            fh.write(text)


# ---------------------------------------------------------------- claude calls


USAGE_FIELDS = (
    "total_cost_usd", "num_turns", "duration_ms", "usage", "modelUsage",
    "permission_denials", "terminal_reason",
)


def append_transcript(transcript, text):
    with open(transcript, "a") as fh:
        fh.write(text.rstrip("\n") + "\n")


def parse_envelope(stdout):
    """The JSON envelope `claude -p --output-format json` prints, or None."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def extract_result(stdout):
    """Pull the 'result' field out of --output-format json; fall back to raw."""
    data = parse_envelope(stdout)
    if data is not None and "result" in data:
        return str(data["result"])
    return stdout


def classify_call(returncode, out, err):
    """Judge one finished claude call as (status, message).

    status is "ok", "rate_limited", "auth" or "error". The rate-limit and login
    patterns only ever see a failed call (non-zero exit, or an envelope with
    is_error set), so a report that merely mentions OAuth is never taken for an
    expired login. An error envelope on exit 0 that matches neither pattern is
    "error" too, but callers still treat its text as the report.
    """
    envelope = parse_envelope(out)
    if returncode == 0 and not (envelope and envelope.get("is_error")):
        return "ok", ""
    if envelope is not None:
        text = str(envelope.get("result") or "").strip()
        evidence = f"{text}\n{err}\n{envelope.get('api_error_status') or ''}"
    else:
        text = out.strip()
        evidence = f"{out}\n{err}"
    message = text or f"{out}\n{err}".strip()
    if RATE_LIMIT_RE.search(evidence):
        return "rate_limited", f"{message}\n{err}".strip()  # a reset time may be on either stream
    if AUTH_ERROR_RE.search(evidence):
        return "auth", message
    return "error", message


def model_of(cmd):
    return cmd[cmd.index("--model") + 1] if "--model" in cmd[:-1] else None


def log_usage(label, model, exit_code, status, envelope=None):
    """Append one line per claude call to logs/usage.jsonl (read by `usage` and the daily cap)."""
    envelope = envelope or {}
    record = {
        "time": now_iso(),
        "label": label,  # worker, review, probe or coordinator
        "model": model,
        "exit": exit_code,
        "status": status,  # ok, error, rate_limited, auth or timeout
        **{key: envelope.get(key) for key in USAGE_FIELDS},
        "is_error": envelope["is_error"] if "is_error" in envelope else exit_code != 0,
    }
    try:
        append_locked(USAGE_LOG, json.dumps(record) + "\n")
    except OSError as exc:  # bookkeeping must never fail a task
        log.warning("Could not write %s: %s", USAGE_LOG.name, exc)


def log_lesson(path, attempts, feedback):
    """Append {task, attempt, feedback, time} to logs/lessons.jsonl for every failed attempt."""
    record = {"task": path.stem, "attempt": attempts, "feedback": feedback.strip()[:LESSON_FEEDBACK_MAX], "time": now_iso()}
    try:
        append_locked(LESSONS_LOG, json.dumps(record) + "\n")
    except OSError as exc:  # bookkeeping must never fail a task
        log.warning("Could not write %s: %s", LESSONS_LOG.name, exc)


def log_failed_call(label, exit_code, out, err, command=""):
    """Keep everything a failed call printed. The real usage-limit wording is not known yet; this is where it shows up."""
    entry = f"=== {now_iso()} {label} exit={exit_code} ===\n"
    if command:
        entry += f"$ {command}\n"
    entry += f"--- stdout\n{out}\n--- stderr\n{err}\n\n"
    try:
        append_locked(FAILED_CALLS_LOG, entry)
    except OSError as exc:
        log.warning("Could not write %s: %s", FAILED_CALLS_LOG.name, exc)


def rate_limit_retrying(label, retries=None):
    """tenacity policy for a transient rate limit: `retries` more tries, exponential wait plus jitter."""
    if retries is None:
        retries = int(cfg("MAX_RATE_LIMIT_RETRIES", "2"))
    base_delay = float(cfg("RATE_LIMIT_BASE_DELAY", "30"))

    def announce(state):
        log.warning(
            "%s: rate limited; backing off %.1fs (retry %d/%d)",
            label, state.next_action.sleep, state.attempt_number, retries,
        )

    return tenacity.Retrying(
        retry=tenacity.retry_if_exception_type(TransientLimit),
        stop=tenacity.stop_after_attempt(retries + 1),
        wait=tenacity.wait_exponential_jitter(initial=base_delay, max=600, jitter=base_delay / 3),
        before_sleep=announce,
        reraise=True,
    )


def claude_env():
    """The environment for a claude subprocess: ours, plus the credentials kept in .env.

    .env is never exported, so the long-lived login token (and the scoped GitHub
    token) reach claude only through here.
    """
    env = dict(os.environ)
    for key, value in (
        ("CLAUDE_CODE_OAUTH_TOKEN", cfg("CLAUDE_CODE_OAUTH_TOKEN")),
        ("GH_TOKEN", cfg("GH_TOKEN")),
    ):
        if value:
            env[key] = value
    return env


STREAM_EVENT_TYPES = {"system", "assistant", "user", "rate_limit_event", "stream_event"}


def is_result_line(obj):
    """The envelope `claude -p` ends a stream with (or prints alone with --output-format json)."""
    return isinstance(obj, dict) and (obj.get("type") == "result" or ("type" not in obj and "is_error" in obj))


def read_stream(proc, prompt, timeout_s, watcher, raw_path):
    """Read a `--output-format stream-json` call while it runs; returns (out, err) like communicate().

    Each event goes to watcher.feed() the moment it arrives, and watcher.tick()
    runs about once a second when it is quiet. `out` is what the rest of the
    dispatcher already understands: the final `result` line (the same envelope
    --output-format json prints), or, when there is none, only the lines that
    are not stream events, so a rate_limit_event can never read as a limit. The
    raw stream goes to raw_path. Raises TimeoutExpired past timeout_s.
    """
    lines = queue.Queue()

    def pump(pipe, tag):
        try:
            for line in pipe:
                lines.put((tag, line))
        finally:
            lines.put((tag, None))

    def write_prompt():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:  # claude exited before reading it all
            pass

    for target, args in ((pump, (proc.stdout, "out")), (pump, (proc.stderr, "err")), (write_prompt, ())):
        threading.Thread(target=target, args=args, daemon=True).start()

    deadline = time.monotonic() + timeout_s
    result, text, err, open_pipes = None, [], [], 2
    with open(raw_path, "a") as raw:
        while open_pipes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout_s)
            try:
                tag, line = lines.get(timeout=min(1.0, remaining))
            except queue.Empty:
                watcher.tick()
                continue
            if line is None:
                open_pipes -= 1
            elif tag == "err":
                err.append(line)
            else:
                raw.write(line)
                raw.flush()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    event = None
                if is_result_line(event):
                    result = line
                elif not (isinstance(event, dict) and event.get("type") in STREAM_EVENT_TYPES):
                    text.append(line)
                watcher.feed(event)
    proc.wait(timeout=max(1.0, deadline - time.monotonic()))
    return (result if result is not None else "".join(text)), "".join(err)


def _call_claude(cmd, prompt, timeout_s, cwd, transcript, label, watcher=None):
    """One claude -p invocation: a ClaudeReply, or an exception. Never sleeps.

    With a watcher the call is read as it runs (--output-format stream-json): see read_stream().
    """
    model = model_of(cmd)
    command = " ".join(cmd)
    append_transcript(transcript, f"\n=== {label} @ {now_iso()} ===\n$ {command}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=claude_env(),
            text=True,
            start_new_session=True,
        )
    except OSError as exc:  # binary vanished or isn't executable
        raise ClaudeError(f"cannot execute {cmd[0]!r}: {exc}")
    try:
        if watcher is None:
            out, err = proc.communicate(prompt, timeout=timeout_s)
        else:
            out, err = read_stream(proc, prompt, timeout_s, watcher, transcript.with_suffix(".stream.jsonl"))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        append_transcript(transcript, f"[{label}] TIMED OUT after {timeout_s:.0f}s")
        log_usage(label, model, None, "timeout")
        log_failed_call(label, "timeout", "", "", command)
        raise CallTimeout(timeout_s)
    append_transcript(
        transcript,
        f"[{label} stdout]\n{out}\n[{label} stderr]\n{err}\n[{label} exit {proc.returncode}]",
    )
    envelope = parse_envelope(out)
    status, message = classify_call(proc.returncode, out, err)
    log_usage(label, model, proc.returncode, status, envelope)
    if status != "ok":
        log_failed_call(label, proc.returncode, out, err, command)
    reply = ClaudeReply(extract_result(out), envelope or {})
    if status == "ok":
        return reply
    if status == "rate_limited":
        if parse_reset_time(message) is not None:
            raise RateLimited(message)  # the reset is hours away; waiting a few minutes is pointless
        raise TransientLimit(message)
    if status == "auth":
        raise AuthError(message)
    if proc.returncode == 0:  # an error envelope on exit 0: the reviewer judges the text, as before
        return reply
    raise ClaudeError(message)


def run_claude(cmd, prompt, timeout_s, cwd, transcript, label, retries=None, watcher=None):
    """Run one claude -p invocation, retrying a transient rate limit with backoff.

    Raises RateLimited when the limit outlasts the retries (or names a reset
    time), AuthError when the login is bad, CallTimeout, or ClaudeError.
    """
    try:
        return rate_limit_retrying(label, retries)(
            _call_claude, cmd, prompt, timeout_s, cwd, transcript, label, watcher
        )
    except TransientLimit as exc:
        raise RateLimited(str(exc)) from None


# ---------------------------------------------------------------- telegram


# ------------------------------------------------------------------- secrets
#
# One list of what a secret looks like, used three ways: the diff guard refuses a commit
# that adds one, the Telegram bridge refuses to read or keep a message that holds one, and
# every message sent to Telegram is redacted first. Better a false alarm than a leaked key.

SECRET_PATTERNS = (
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("Notion token", re.compile(r"\bntn_[A-Za-z0-9]{20,}")),
    ("DigitalOcean token", re.compile(r"\bdop_v1_[A-Za-z0-9]{20,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("password or token", re.compile(
        r"(?P<keep>(?:token|secret|password|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9/+_=.\-]{16,}", re.I)),
)


def secret_kinds(text):
    """The kinds of secret `text` appears to contain, in the order of SECRET_PATTERNS. Never the values."""
    return [kind for kind, pattern in SECRET_PATTERNS if pattern.search(text)]


def redact_secrets(text):
    """`text` with every secret replaced by [redacted <kind>] (a generic assignment keeps its name)."""
    for kind, pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m, kind=kind: (m.groupdict().get("keep") or "") + f"[redacted {kind}]", text)
    return text


TELEGRAM_MAX = 4096


def clip_for_telegram(text, limit=TELEGRAM_MAX):
    """`text` cut so it fits one message; a last resort for senders that have no attachment to fall back on."""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def md_to_telegram_html(text):
    """Convert common markdown to Telegram HTML (for parse_mode=HTML).

    Handles code fences (unclosed ones run to the end), inline code, bold,
    italic, links, headers, and bullets. Everything else is HTML-escaped, so
    the worst case is a literal character — never a rejected message; senders
    still fall back to plain text if Telegram returns 400.
    """
    stashed = []

    def stash(tag, content):
        stashed.append(f"<{tag}>{html.escape(content)}</{tag}>")
        return f"\x00{len(stashed) - 1}\x00"

    text = re.sub(
        r"```[^\n`]*\n?(.*?)(?:```|\Z)",
        lambda m: stash("pre", m.group(1).rstrip("\n")),
        text, flags=re.S,
    )
    text = re.sub(r"`([^`\n]+)`", lambda m: stash("code", m.group(1)), text)

    text = html.escape(text, quote=False)

    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(\S(?:[^*\n]*\S)?)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_(\S(?:[^_\n]*\S)?)_(?![\w_])", r"<i>\1</i>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s\"]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"^(\s*)[-*]\s+", r"\1• ", text, flags=re.M)

    return re.sub(r"\x00(\d+)\x00", lambda m: stashed[int(m.group(1))], text)


def telegram_url(method):
    """Bot API URL for a method. TELEGRAM_API_BASE lets tests point at a local fake."""
    base = cfg("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
    return f"{base}/bot{cfg('TELEGRAM_BOT_TOKEN')}/{method}"


def telegram_configured():
    if cfg("TELEGRAM_BOT_TOKEN") and cfg("TELEGRAM_CHAT_ID"):
        return True
    log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID); skipping notification")
    return False


def send_message_id(raw, formatted=True):
    """sendMessage (HTML first when `formatted`, plain text if Telegram returns 400).

    Returns the new message's id (0 if Telegram answered without one), or None
    when nothing was sent. The id is what editMessageText and reactions refer to.
    """
    chat_id = cfg("TELEGRAM_CHAT_ID")
    raw = clip_for_telegram(redact_secrets(raw))  # a worker that prints a key must not forward it to a phone
    variants = [({"chat_id": chat_id, "text": md_to_telegram_html(raw), "parse_mode": "HTML"}, True)] if formatted else []
    variants.append(({"chat_id": chat_id, "text": raw}, False))
    for params, is_html in variants:
        data = urllib.parse.urlencode(params).encode()
        try:
            with urllib.request.urlopen(telegram_url("sendMessage"), data, timeout=30) as reply:
                body = reply.read()
        except urllib.error.HTTPError as exc:
            if is_html and exc.code == 400:  # bad entities — resend unformatted
                log.warning("Telegram rejected HTML formatting; resending as plain text")
                continue
            log.warning("Telegram notification failed: %s", exc)
            return None
        except Exception as exc:  # notification failure must never fail the task
            log.warning("Telegram notification failed: %s", exc)
            return None
        log.info("Telegram notification sent")
        try:
            return int(json.loads(body)["result"]["message_id"])
        except (ValueError, KeyError, TypeError):
            return 0
    return None


def post_message(raw):
    """sendMessage as HTML, or as plain text when Telegram returns 400. True once it was sent."""
    return send_message_id(raw) is not None


def edit_message(message_id, raw):
    """editMessageText as plain text. True when done (or harmlessly unchanged), False on failure,
    or the seconds Telegram asked us to wait after a 429."""
    data = urllib.parse.urlencode({"chat_id": cfg("TELEGRAM_CHAT_ID"), "message_id": message_id, "text": redact_secrets(raw)}).encode()
    try:
        urllib.request.urlopen(telegram_url("editMessageText"), data, timeout=30).close()
        return True
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read())
        except (ValueError, OSError):
            detail = {}
        if exc.code == 400 and "not modified" in str(detail.get("description", "")):
            return True
        if exc.code == 429:
            return float((detail.get("parameters") or {}).get("retry_after") or 30)
        log.warning("Telegram edit failed: %s", exc.code)
    except Exception as exc:  # a status edit must never fail the task
        log.warning("Telegram edit failed: %s", type(exc).__name__)
    return False


def post_document(name, source):
    """sendDocument with `source` (bytes, or a Path streamed from disk). True once it was sent."""
    try:
        with ExitStack() as stack:
            content = source if isinstance(source, bytes) else stack.enter_context(source.open("rb"))
            reply = httpx.post(
                telegram_url("sendDocument"), data={"chat_id": cfg("TELEGRAM_CHAT_ID")},
                files={"document": (name, content)}, timeout=120,
            )
        reply.raise_for_status()
        if reply.json().get("ok") is not True:  # only Telegram's own "ok" counts as sent
            raise ValueError("Telegram answered ok: false")
    except (httpx.HTTPError, OSError, ValueError) as exc:
        # httpx puts the request URL, which contains the bot token, in its messages: log only the cause.
        cause = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
        log.warning("Telegram document %s failed: %s", name, cause)
        return False
    log.info("Telegram document %s sent", name)
    return True


# ------------------------------------------------------------- live status


def describe_event(event):
    """The tool a stream-json `assistant` event is calling: {tool, target, tokens, id}, or None for any other event."""
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return None
    message = event.get("message") or {}
    uses = [b for b in message.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use"]
    if not uses:
        return None
    name, tool_input = uses[-1].get("name") or "", uses[-1].get("input") or {}
    target = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern") \
        or str(tool_input.get("command") or "")[:40]
    target = str(target)
    if len(target) > 60:
        target = "…" + target[-59:]
    return {
        "tool": name, "target": target, "id": message.get("id"),
        "tokens": int((message.get("usage") or {}).get("output_tokens") or 0),
    }


def clock_text(seconds):
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


class StatusEditor:
    """One Telegram message that follows a worker run: the current tool and file, elapsed time, steps and tokens.

    Nothing is sent until the worker first calls a tool. Afterwards the message is
    edited at most once per `interval` seconds (STATUS_EDIT_INTERVAL_SECONDS), plain
    text, whatever the number of events. finish() waits out the interval before
    its last edit. Every Telegram failure is swallowed: a status message is never
    allowed to fail a task. `send(text)` returns the message id (None = could not
    send); `edit(message_id, text)` returns True, False, or seconds to back off.
    """

    def __init__(self, title, send, edit, clock=time.monotonic, interval=None, sleep=time.sleep):
        self.title, self._send, self._edit, self._clock, self._sleep = title, send, edit, clock, sleep
        self.interval = float(cfg("STATUS_EDIT_INTERVAL_SECONDS", "10")) if interval is None else float(interval)
        self.started = clock()
        self.message_id = None
        self.dead = False  # the message could not be sent: leave the chat alone
        self.last_call = None
        self.blocked_until = 0.0
        self.last_text = None
        self.tool = self.target = ""
        self.steps = 0
        self.tokens = {}
        self.cost = None

    def feed(self, event):
        """The stream-reader's hook: one parsed event (or None for an unparseable line)."""
        try:
            if isinstance(event, dict) and event.get("type") == "result":
                self.cost = event.get("total_cost_usd")
            elif info := describe_event(event):
                self.event(info)
        except Exception as exc:  # noqa: BLE001 - a status bug must never fail the task
            log.warning("status update failed: %s", type(exc).__name__)

    def event(self, info):
        self.tool, self.target = info["tool"], info.get("target") or ""
        self.steps += 1
        key = info.get("id") or object()
        self.tokens[key] = max(self.tokens.get(key, 0), int(info.get("tokens") or 0))
        self._push()

    def tick(self):
        """Quiet stretch: refresh the elapsed time, still no faster than the interval."""
        self._push()

    def _elapsed(self):
        return clock_text(self._clock() - self.started)

    def _detail(self):
        total = sum(self.tokens.values())
        tokens = f"{total / 1000:.1f}k" if total >= 1000 else str(total)
        return f"{self._elapsed()} · {self.steps} steps · {tokens} tokens"

    def _text(self):
        return f"⚙️ {self.title}\n🔧 {self.tool} {self.target}".rstrip() + f"\n⏱ {self._detail()}"

    def _due(self):
        now = self._clock()
        return now >= self.blocked_until and (self.last_call is None or now - self.last_call >= self.interval)

    def _push(self):
        if self.dead or not self.steps:
            return
        text = self._text()
        if self.message_id is None:
            self.last_call = self._clock()
            try:
                self.message_id = self._send(text)
            except Exception:  # noqa: BLE001
                self.message_id = None
            self.dead = not self.message_id  # None, or 0 (no id to edit)
            self.last_text = text
        elif text != self.last_text and self._due():
            self._do_edit(text)

    def _do_edit(self, text):
        self.last_call = self._clock()
        try:
            result = self._edit(self.message_id, text)
        except Exception:  # noqa: BLE001
            result = False
        if isinstance(result, (int, float)) and not isinstance(result, bool):  # Telegram said to wait
            self.blocked_until = self._clock() + float(result)
        self.last_text = text

    def finish(self, outcome, cost=None):
        """The last edit: how it ended, how long, and the exact cost. Waits for the interval first."""
        if self.dead or not self.message_id:
            return
        if cost is not None:
            self.cost = cost
        wait = self.interval - (self._clock() - self.last_call) if self.last_call is not None else 0
        if wait > 0:
            self._sleep(wait)
        cost = f" · ${float(self.cost):.2f}" if self.cost is not None else ""
        self._do_edit(f"{outcome}\n{self.title} · {self._detail()}{cost}")


class QuietWatcher:
    """The stream reader's hook when nobody wants a status message."""

    def feed(self, event):
        pass

    def tick(self):
        pass


def start_status(path, meta):
    """A StatusEditor for this task's worker run, or None when there is nobody to tell (no Telegram, a quiet task)."""
    if meta.get("quiet") == "true" or not (cfg("TELEGRAM_BOT_TOKEN") and cfg("TELEGRAM_CHAT_ID")):
        return None
    return StatusEditor(
        path.name,
        send=lambda text: send_message_id(text, formatted=False),
        edit=edit_message,
    )


def fits_in_message(text, limit=REPORT_INLINE_MAX):
    """True when `text` can go out as one message, in its HTML form and its plain-text fallback, """
    return len(text) <= limit and len(md_to_telegram_html(text)) <= TELEGRAM_MAX


def fit_preview(prefix, text, suffix):
    """prefix + the start of text + suffix, cut at a line break so the HTML form fits one message.

    The raw text is cut first and converted second: HTML escaping makes it longer.
    """
    size = max(0, min(len(text), REPORT_INLINE_MAX - len(prefix) - len(suffix)))
    while True:
        piece = text[:size]
        cut = piece.rfind("\n")
        if cut > size // 2:
            piece = piece[:cut]
        message = prefix + piece.rstrip() + suffix
        if size == 0 or len(md_to_telegram_html(message)) <= TELEGRAM_MAX:
            return message
        size = int(size * 0.8)


def send_report(title, text, files=(), task_id="report", root=None):
    """Tell the user something. A long `text` becomes a preview plus `<task_id>.md`; `files` follow as documents.

    A file Telegram did not accept is named in a follow-up as "not delivered: <path>" (relative to `root` when given).

    The one way the dispatcher sends done, failed and dependency notices, so
    nothing depends on a path only the machine running the queue can see.
    """
    if not telegram_configured():
        return
    body = f"{title}\n\n{text}" if title and text else title or text
    attachments = []
    if fits_in_message(body):
        post_message(body)
    else:
        name = f"{task_id}.md"
        prefix = f"{title}\n\n" if title else ""
        post_message(fit_preview(prefix, text, f"\n\n[Preview — the full report is attached as {name}]"))
        attachments.append((f"the full report ({name})", name, redact_secrets(text).encode()))
    root = Path(root).resolve() if root else None
    attachments += [(str(path.relative_to(root)) if root and path.is_relative_to(root) else path.name, path.name, path)
                    for path in map(Path, files)]
    for label, name, source in attachments:
        if not post_document(name, source):
            post_message(f"⚠️ Couldn't attach {label}: Telegram refused the upload.\nnot delivered: {label}")


def send_telegram(text):
    send_report("", text)


# ---------------------------------------------------------------- limits, login, once-only reports

# The wording below is a best guess at what the CLI prints. logs/failed-calls.log
# records the real text of the next limit hit; extend these patterns from it.
_EPOCH_RE = re.compile(r"(?:limit|quota)[^|\n]*\|\s*(\d{10}|\d{13})\b", re.I)  # "...limit reached|1760000000"
_RELATIVE_RE = re.compile(
    r"(?:try again|retry|resets?)\s+in\s+(\d+(?:\.\d+)?)\s*"
    r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|[smh])\b", re.I,
)
_CLOCK_RE = re.compile(r"\bresets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)?(?:\s*\(([^)]+)\))?", re.I)


def parse_reset_time(text, now=None):
    """When a usage limit lifts, from the CLI's message, as an aware UTC datetime; None if it says nothing usable."""
    now = now or datetime.now(timezone.utc)
    result = None
    match = _EPOCH_RE.search(text)
    if match:
        raw = int(match.group(1))
        result = datetime.fromtimestamp(raw / 1000 if len(match.group(1)) == 13 else raw, timezone.utc)
    if result is None:
        match = _RELATIVE_RE.search(text)
        if match:
            seconds = {"s": 1, "m": 60, "h": 3600}[match.group(2)[0].lower()]
            result = now + timedelta(seconds=float(match.group(1)) * seconds)
    if result is None:
        match = _CLOCK_RE.search(text)
        if match and (match.group(2) or match.group(3)):  # "resets 3" alone could be anything
            hour, minute, ampm = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "").lower()
            if ampm:
                hour = hour % 12 + (12 if ampm == "pm" else 0)
            if hour < 24 and minute < 60:
                zone = None  # the machine's own zone, unless the message names one we know
                if match.group(4):
                    try:
                        zone = ZoneInfo(match.group(4).strip())
                    except Exception:
                        zone = None
                local_now = now.astimezone(zone)
                result = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if result <= local_now:
                    result += timedelta(days=1)
    if result is not None and now < result <= now + MAX_RESET_WAIT:
        return result.astimezone(timezone.utc)
    return None


def usage_limit_until(message):
    """How long to stay paused for a usage limit: the stated reset time, else the cooldown setting."""
    reset = parse_reset_time(message)
    if reset is not None:
        return reset + timedelta(seconds=PAUSE_BUFFER_S)
    return datetime.now(timezone.utc) + timedelta(minutes=float(cfg("USAGE_LIMIT_COOLDOWN_MINUTES", "60")))


def format_when(moment):
    """Local clock time for a message: HH:MM today, otherwise with the weekday."""
    local = moment.astimezone()
    return local.strftime("%H:%M" if local.date() == datetime.now().astimezone().date() else "%a %H:%M")


class Pause(NamedTuple):
    until: datetime
    reason: str


def read_pause():
    """The pause in state/paused_until, or None. An unreadable file is deleted: it must never wedge the queue."""
    try:
        lines = PAUSED_FILE.read_text().splitlines()
    except OSError:
        return None
    try:
        until = datetime.strptime(lines[0].strip(), ISO_FMT).replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        log.warning("Ignoring unreadable %s and removing it", PAUSED_FILE)
        PAUSED_FILE.unlink(missing_ok=True)
        return None
    return Pause(until, lines[1].strip() if len(lines) > 1 else "paused")


def pause_queue(until, reason, notify=True):
    """Stop the queue until `until`, saying so once. No-op if it is already paused at least that long."""
    current = read_pause()
    if current and current.until > datetime.now(timezone.utc) and current.until >= until:
        return
    write_atomic(PAUSED_FILE, f"{until.astimezone(timezone.utc).strftime(ISO_FMT)}\n{reason}\n")
    log.warning("Queue paused until %s (%s)", format_when(until), reason)
    if notify:
        send_telegram(f"⏸ Queue paused until {format_when(until)} ({reason}). It resumes by itself.")


def current_stop():
    """Why queued work cannot run now: ("auth", None, ""), ("paused", until, reason), or None."""
    if AUTH_FAILED_FILE.exists():
        return "auth", None, ""
    pause = read_pause()
    if pause and pause.until > datetime.now(timezone.utc):
        return "paused", pause.until, pause.reason
    return None


AUTH_NOTICE = (
    "🔑 Claude login expired — the queue is stopped, and nothing runs until it is renewed.\n"
    "Fix: on the queue host run `claude` and sign in again (or `claude setup-token`). "
    "The next cron run checks the login with one cheap call and resumes by itself."
)


def mark_auth_failed(message, notify=True):
    """Record an expired login. With notify, say so once on Telegram and in the log."""
    write_atomic(AUTH_FAILED_FILE, f"{now_iso()}\n{tail(message, 500)}\n")
    if notify:
        report_once(
            "auth", "",
            f"Claude login expired ({' '.join(message.split())[:200]}); queue stopped until `claude` works "
            "again: run `claude` and sign in, or `claude setup-token`",
            AUTH_NOTICE,
            level=logging.ERROR,
        )


def login_works():
    """One cheap claude call. False only if the login is still bad (or the call timed out)."""
    claude_bin = resolve_claude_bin()
    if claude_bin is None:
        log.debug("login probe skipped: no claude binary")
        return False
    cwd = Path(cfg("DEFAULT_CWD") or BASE / "workspace")
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = [claude_bin, "-p", "--model", cfg("PROBE_MODEL", "claude-haiku-4-5-20251001"),
           "--output-format", "json"]
    try:
        run_claude(cmd, PROBE_PROMPT, 120, str(cwd), LOGS / "probe.log", "probe", retries=0)
    except (AuthError, CallTimeout) as exc:
        log.debug("login probe failed: %r", exc)
        return False
    except (RateLimited, ClaudeError):
        pass  # whatever that was, it was not the login
    return True


def queue_is_stopped():
    """True when this run must end at once: the queue is paused, or the login is still expired.

    Also does the housekeeping when a stop ends: an expired pause file is
    removed and the resume announced, and auth_failed is cleared once a probe
    call works. While stopped it stays quiet: the first notice was the report.
    """
    pause = read_pause()
    if pause:
        if pause.until > datetime.now(timezone.utc):
            log.info("Queue paused until %s (%s); exiting", format_when(pause.until), pause.reason)
            return True
        PAUSED_FILE.unlink(missing_ok=True)
        log.info("Pause (%s) is over; resuming", pause.reason)
        send_telegram(f"▶️ Queue resumed (it was paused: {pause.reason}).")
    if AUTH_FAILED_FILE.exists():
        if not login_works():
            return True
        AUTH_FAILED_FILE.unlink(missing_ok=True)
        clear_reported("auth")
        log.info("Claude login works again; resuming")
        send_telegram("✅ Claude login restored — queue resumed.")
    return False


# Problems that repeat every cycle are reported once. state/seen.json maps a
# key to the signature last reported; a key like "kind|tasks/pending/x.md"
# is dropped when that file goes away (prune_seen), and a changed signature
# counts as a new problem.


def load_seen():
    try:
        data = json.loads(SEEN_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def report_once(key, signature, log_text, telegram_text, level=logging.WARNING):
    """Log and send a problem unless this exact problem was already reported. True if it was said."""
    seen = load_seen()
    if seen.get(key) == signature:
        return False
    seen[key] = signature
    write_atomic(SEEN_FILE, json.dumps(seen, indent=1))
    log.log(level, "%s", log_text)
    send_telegram(telegram_text)
    return True


def clear_reported(key):
    """The problem is gone: forget it, so it is reported again if it comes back."""
    seen = load_seen()
    if key in seen:
        del seen[key]
        write_atomic(SEEN_FILE, json.dumps(seen, indent=1))


def prune_seen():
    seen = load_seen()
    kept = {k: v for k, v in seen.items() if "|" not in k or (BASE / k.split("|", 1)[1]).exists()}
    if kept != seen:
        write_atomic(SEEN_FILE, json.dumps(kept, indent=1))


def usage_records():
    """Every parseable line of logs/usage.jsonl as (local datetime, record)."""
    try:
        lines = USAGE_LOG.read_text().splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
            when = datetime.strptime(record["time"], ISO_FMT).replace(tzinfo=timezone.utc).astimezone()
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if isinstance(record, dict):
            records.append((when, record))
    return records


def attempts_today():
    """Worker calls today (local time) that used up an attempt; a limit or login failure doesn't."""
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone()
    return sum(
        1 for when, record in usage_records()
        if record.get("label") == "worker" and when >= midnight
        and record.get("status") not in ("rate_limited", "auth")
    )


def daily_cap_reached():
    """Pause until local midnight when today's attempts hit MAX_ATTEMPTS_PER_DAY and work is waiting."""
    cap = int(cfg("MAX_ATTEMPTS_PER_DAY", "12"))
    if cap <= 0 or not any(PENDING.glob("*.md")) or attempts_today() < cap:
        return False
    midnight = (datetime.now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    pause_queue(midnight.astimezone(), f"daily attempt cap of {cap} reached")
    return True


def usage_report():
    """Print claude calls, errors, cost, turns and web searches by day and model."""
    records = usage_records()
    if not records:
        print("No usage recorded yet.")
        return 0
    rows = {}

    def row(day, model):
        return rows.setdefault((day, model), {"calls": 0, "errors": 0, "cost": 0.0, "turns": 0, "searches": 0})

    for when, record in records:
        day = when.strftime("%Y-%m-%d")
        entry = row(day, record.get("model") or "unknown")
        entry["calls"] += 1
        entry["errors"] += bool(record.get("is_error")) or record.get("exit") not in (0, None)
        entry["turns"] += record.get("num_turns") or 0
        by_model = record.get("modelUsage") or {}
        if by_model:  # one call can spend on several models (a search sub-agent, say)
            for name, spent in by_model.items():
                part = row(day, name)
                part["cost"] += spent.get("costUSD") or 0
                part["searches"] += spent.get("webSearchRequests") or 0
        else:
            entry["cost"] += record.get("total_cost_usd") or 0

    def show(day, model, entry):
        print(f"{day:<10}  {model:<34}  {entry['calls']:>5}  {entry['errors']:>6}  "
              f"{entry['cost']:>9.4f}  {entry['turns']:>5}  {entry['searches']:>12}")

    print(f"{'day':<10}  {'model':<34}  {'calls':>5}  {'errors':>6}  "
          f"{'cost_usd':>9}  {'turns':>5}  {'web_searches':>12}")
    total = {"calls": 0, "errors": 0, "cost": 0.0, "turns": 0, "searches": 0}
    for (day, model), entry in sorted(rows.items()):
        show(day, model, entry)
        for key in total:
            total[key] += entry[key]
    show("total", "all", total)
    return 0


# ---------------------------------------------------------------- recurring

DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
LAST_RUN_FMT = "%Y-%m-%dT%H:%M:%S"


def is_due(spec, last_run, now):
    """True when a recurring schedule should fire. All times are server-local.

    Supported specs: "every 30m|6h|2d", "daily at 06:30",
    "weekly on mon at 09:00" (see SCHEDULE_FORMS). Raises ValueError on anything
    else, which is how check_schedule() tells a template's schedule is bad.
    """
    spec = spec.strip()
    match = re.fullmatch(r"every\s+(\d+)\s*(m|h|d)", spec, re.I)
    if match:
        seconds = int(match.group(1)) * {"m": 60, "h": 3600, "d": 86400}[match.group(2).lower()]
        return last_run is None or (now - last_run).total_seconds() >= seconds
    match = re.fullmatch(r"daily\s+at\s+(\d{1,2}):(\d{2})", spec, re.I)
    if match:
        target = now.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
        if target > now:
            target -= timedelta(days=1)
        return last_run is None or last_run < target
    match = re.fullmatch(r"weekly\s+on\s+([a-z]{3})[a-z]*\s+at\s+(\d{1,2}):(\d{2})", spec, re.I)
    if match:
        day = DAY_NAMES.get(match.group(1).lower())
        if day is None:
            raise ValueError(f"unknown weekday in schedule {spec!r}")
        target = now.replace(hour=int(match.group(2)), minute=int(match.group(3)), second=0, microsecond=0)
        target -= timedelta(days=(now.weekday() - day) % 7)
        if target > now:
            target -= timedelta(days=7)
        return last_run is None or last_run < target
    raise ValueError(f"unrecognized schedule {spec!r}")


def spawn_recurring():
    """Stamp out a one-shot pending instance for each due recurring template."""
    now = datetime.now()
    for template in sorted(RECURRING.glob("*.md")):
        meta, body = parse_task(template)
        schedule_key = f"schedule|tasks/recurring/{template.name}"
        problems = validate_task(meta, body, template=True)
        if problems:  # said once per distinct set of problems; the template stays put so it can be fixed in place
            report_once(
                schedule_key, "\n".join(problems),
                f"Recurring template {template.name} is invalid: {'; '.join(problems)}",
                f"⚠️ Recurring task not scheduled: {template.name}\n"
                + "\n".join(f"- {problem}" for problem in problems)
                + "\nIt will not run until the template is fixed; this is the only notice.",
                level=logging.ERROR,
            )
            continue
        last_run = None
        if meta.get("last_run"):
            try:
                last_run = datetime.strptime(meta["last_run"], LAST_RUN_FMT)
            except ValueError:
                log.warning("Recurring template %s has malformed last_run %r; treating as never run",
                            template.name, meta["last_run"])
        due = is_due(meta["schedule"], last_run, now)
        clear_reported(schedule_key)
        if not due:
            continue
        # Don't pile up instances while an earlier one is still queued or running;
        # last_run stays untouched so the template fires once the queue clears.
        existing = list(PENDING.glob(f"{template.stem}-[0-9]*.md")) + list(
            ACTIVE.glob(f"{template.stem}-[0-9]*.md")
        )
        if existing:
            log.info("Recurring %s is due but an instance is still queued (%s); deferring",
                     template.name, existing[0].name)
            continue
        instance_meta = {k: v for k, v in meta.items() if k not in ("schedule", "last_run")}
        instance_meta["attempts"] = "0"
        instance_name = f"{template.stem}-{now.strftime('%Y%m%d-%H%M%S')}.md"
        write_task(PENDING / instance_name, instance_meta, body)
        meta["last_run"] = now.strftime(LAST_RUN_FMT)
        write_task(template, meta, body)
        log.info("Recurring %s (schedule: %s) spawned instance %s",
                 template.name, meta["schedule"], instance_name)


# ---------------------------------------------------------------- queue flow


def recover_stale():
    for stale in sorted(ACTIVE.glob("*.md")):
        dest = PENDING / stale.name
        shutil.move(str(stale), str(dest))
        log.warning("Recovered stale active task %s back to pending (previous run died?)", stale.name)


@contextmanager
def hold_queue_lock(timeout=10.0):
    """Yield True with the dispatcher lock held, or False if it stayed busy."""
    lock = open(LOCKFILE, "w")
    acquired = False
    deadline = time.time() + timeout
    try:
        while not acquired:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                if time.time() >= deadline:
                    break
                time.sleep(0.5)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def action_source():
    if ACTION_SOURCE:
        return ACTION_SOURCE
    try:
        return "cli, terminal" if sys.stdin.isatty() else "cli, no terminal"  # no terminal: the coordinator, cron or a script
    except (ValueError, OSError):
        return "cli, no terminal"


def audited(action):
    """Record every call of a (stem) -> (ok, message) command in logs/actions.log, whether it worked or not.

    The command-line forms configure no logging, so before this a task could be cancelled, retried or approved
    and nothing said by whom or when.
    """
    def decorate(command):
        @functools.wraps(command)
        def inner(stem, *args, **kwargs):
            ok, message = command(stem, *args, **kwargs)
            record = {"time": now_iso(), "action": action, "task": str(stem).strip().removesuffix(".md"), "ok": bool(ok),
                      "source": action_source(), "message": redact_secrets(" ".join(str(message).split()))[:300]}
            try:
                append_locked(ACTIONS_LOG, json.dumps(record) + "\n")
            except OSError as exc:  # bookkeeping must never fail the command
                log.warning("Could not write %s: %s", ACTIONS_LOG.name, exc)
            return ok, message
        return inner
    return decorate


@audited("cancel")
def cancel_task(stem):
    """Archive a queued task (and/or recurring template) to tasks/cancelled/.

    Returns (ok, message). Holds the dispatcher lock while moving files so a
    cancel can never race pick_task() moving the same task into active/.
    Cancels, in one call: the pending task <stem>.md, any queued recurring
    instances <stem>-<timestamp>.md, and the recurring template <stem>.md.
    """
    stem = stem.strip()
    if stem.endswith(".md"):
        stem = stem[:-3]
    if not stem or "/" in stem:
        return False, "Give a task id (the file stem, e.g. 'fetch-data')."

    for directory in (PENDING, ACTIVE, DONE, FAILED, RECURRING, CANCELLED):
        directory.mkdir(parents=True, exist_ok=True)

    def archive(path):
        dest = CANCELLED / path.name
        if dest.exists():  # e.g. template and an old pending copy share a name
            dest = CANCELLED / f"{path.stem}.cancelled-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        shutil.move(str(path), str(dest))

    with hold_queue_lock() as held:
        if not held:
            return False, (
                f"The dispatcher is mid-run; could not cancel '{stem}' safely. "
                f"Try again in a moment."
            )
        note = (
            f"\n\n## Cancelled ({now_iso()})\n\n"
            f"Removed from the queue before running. Move this file back to "
            f"tasks/pending/ to requeue it.\n"
        )
        lines = []
        instances = sorted(PENDING.glob(f"{stem}-[0-9]*.md"))
        queued = [d / f"{stem}.md" for d in (PENDING, PROPOSED) if (d / f"{stem}.md").exists()]
        for path in queued + instances:
            meta, body = parse_task(path)
            write_task(path, meta or {}, body.rstrip("\n") + note)
            archive(path)
            lines.append(f"Cancelled {path.name} (was {path.parent.name}).")
        template = RECURRING / f"{stem}.md"
        if template.exists():
            archive(template)
            lines.append(f"Cancelled recurring template {template.name}; no further instances will spawn.")

        if not lines:
            running = [ACTIVE / f"{stem}.md"] + sorted(ACTIVE.glob(f"{stem}-[0-9]*.md"))
            running = [p for p in running if p.exists()]
            if running:
                return False, (
                    f"'{running[0].name}' is running right now and can't be cancelled "
                    f"mid-attempt. If it fails review and requeues, cancel it then."
                )
            for label, directory in (("done", DONE), ("failed", FAILED), ("cancelled", CANCELLED)):
                if (directory / f"{stem}.md").exists():
                    return False, f"'{stem}' is not queued — it is already in tasks/{label}/."
            return False, f"No task named '{stem}' in pending/, active/, or recurring/."

        # Warn about tasks left waiting on the stem we just cancelled.
        dependents = sorted(
            p.name for p in PENDING.glob("*.md") if stem in dep_names(parse_task(p)[0])
        )
        if dependents:
            lines.append(
                f"Warning: still pending and depending on '{stem}' (they will never "
                f"run unless it is restored or they are cancelled too): {', '.join(dependents)}."
            )
        log.info("cancel_task(%s): %s", stem, " ".join(lines))
        return True, "\n".join(lines)


@audited("retry")
def retry_task(stem):
    """Move a failed or cancelled task back to pending/ with attempts reset.

    Returns (ok, message), holding the dispatcher lock like cancel_task().
    """
    stem = stem.strip()
    if stem.endswith(".md"):
        stem = stem[:-3]
    if not stem or "/" in stem:
        return False, "Give a task id (the file stem, e.g. 'fetch-data')."

    for directory in (PENDING, ACTIVE, DONE, FAILED, RECURRING, CANCELLED):
        directory.mkdir(parents=True, exist_ok=True)

    with hold_queue_lock() as held:
        if not held:
            return False, (
                f"The dispatcher is mid-run; could not retry '{stem}' safely. "
                f"Try again in a moment."
            )
        for label, directory in (("failed", FAILED), ("cancelled", CANCELLED)):
            path = directory / f"{stem}.md"
            if path.exists():
                meta, body = parse_task(path)
                meta = meta or {}
                meta["attempts"] = "0"
                for key in ("pending_review", "review_failures", "pending_publish", "publish_failures", "publish_blocked",
                            "held_until"):
                    meta.pop(key, None)  # a fresh start redoes the work too
                write_task(path, meta, body)
                shutil.move(str(path), str(PENDING / path.name))
                log.info("retry_task(%s): requeued from tasks/%s/", stem, label)
                return True, (
                    f"Requeued {path.name} from tasks/{label}/ with attempts reset "
                    f"to 0. The next dispatcher run picks it up."
                )
        if (PENDING / f"{stem}.md").exists():
            return False, f"'{stem}' is already pending."
        if (ACTIVE / f"{stem}.md").exists():
            return False, f"'{stem}' is running right now."
        if (DONE / f"{stem}.md").exists():
            return False, f"'{stem}' already completed — it is in tasks/done/."
        if (RECURRING / f"{stem}.md").exists():
            return False, f"'{stem}' is a recurring template; it spawns on its own schedule."
        return False, f"No task named '{stem}' in failed/ or cancelled/."


def dep_names(meta):
    """Parse depends_on into a list of task stems (trailing .md tolerated)."""
    names = []
    for part in ((meta or {}).get("depends_on") or "").split(","):
        part = part.strip()
        if part.endswith(".md"):
            part = part[:-3]
        if part:
            names.append(part)
    return names


def dep_state(dep):
    """Where a dependency lives: done, failed, cancelled, waiting, or missing.

    Matches the task itself (<dep>.md) or any recurring instance of it
    (<dep>-<timestamp>.md). done/ is checked first so one successful
    recurring instance satisfies the dependency even if a later one failed.
    """
    def present(directory):
        return (directory / f"{dep}.md").exists() or any(directory.glob(f"{dep}-[0-9]*.md"))

    if present(DONE):
        return "done"
    if present(FAILED):
        return "failed"
    if present(CANCELLED):
        return "cancelled"
    if present(PENDING) or present(ACTIVE) or present(RECURRING) or present(PROPOSED):
        return "waiting"
    return "missing"


def cascade_dependency_failure(path, failed_dep):
    meta, body = parse_task(path)
    body = body.rstrip("\n") + (
        f"\n\n## Dependency Failed ({now_iso()})\n\n"
        f"Not run: dependency '{failed_dep}' is in tasks/failed/. Fix and requeue the "
        f"dependency (it must reach done/), then move this task back to pending/.\n"
    )
    write_task(path, meta or {}, body)
    shutil.move(str(path), str(FAILED / path.name))
    log.error("Task %s cascaded to failed/: its dependency %s failed permanently", path.name, failed_dep)
    send_report(
        f"❌ Task not run: {path.name}\nIts dependency '{failed_dep}' failed permanently.", "",
        task_id=path.stem,
    )


# ------------------------------------------------------------ approval gate
#
# A task at or above APPROVAL_MIN_TIER waits in tasks/proposed/ until you approve
# it: a 👍 on its announcement (handled by the bridge, in code), or /approve. The
# approval is recorded in state/approvals/<task>.json, which claude can't write,
# together with a digest of the spec. Change the spec afterwards and the approval
# no longer matches, so the task is parked and announced again.

TIER_LABELS = {0: "reads only", 1: "writes in its workspace", 2: "leaves the box"}
DISPATCHER_HEADING = re.compile(r"^## (Attempt \d+ Feedback|Result \(|Invalid Task|Cancelled)", re.M)
DIGEST_KEYS = ("model", "allowed_tools", "verify", "repo", "base", "cwd", "mcp_config", "deliver", "propose", "depends_on")
RECURRING_INSTANCE = re.compile(r"^(?P<template>.+)-\d{8}-\d{6}$")


def approval_min_tier():
    try:
        return int(cfg("APPROVAL_MIN_TIER", "0") or 0)
    except ValueError:
        return 0


def spec_digest(meta, body):
    """What an approval covers: the tools, checks and places the task may touch, and the text you were shown.

    Everything the dispatcher appends later (feedback, results) and its own
    bookkeeping keys are left out, so a retry doesn't need a new approval.
    """
    found = DISPATCHER_HEADING.search(body)
    spec = body[:found.start()] if found else body
    parts = [f"{key}={meta.get(key, '')}" for key in DIGEST_KEYS] + [spec.strip()]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def read_ledger(stem):
    try:
        return json.loads((APPROVALS / f"{stem}.json").read_text())
    except (OSError, ValueError):
        return None


def write_ledger(stem, entry):
    write_atomic(APPROVALS / f"{stem}.json", json.dumps(entry))


@contextmanager
def ledger_lock():
    """The dispatcher and the bridge both change approvals; one at a time."""
    APPROVALS.mkdir(parents=True, exist_ok=True)
    with filelock.FileLock(str(APPROVALS / ".lock"), timeout=30):
        yield


def is_recurring_instance(stem):
    found = RECURRING_INSTANCE.match(stem)
    return bool(found) and (RECURRING / f"{found.group('template')}.md").exists()


def needs_approval(path, meta, body):
    """True when this task may not run yet. Invalid tasks are not gated: process_task rejects them."""
    threshold = approval_min_tier()
    if threshold <= 0 or meta is None or validate_task(meta, body):
        return False
    if tier_of(meta) < threshold or is_recurring_instance(path.stem):
        return False
    entry = read_ledger(path.stem)
    return not (entry and entry.get("status") == "approved" and entry.get("digest") == spec_digest(meta, body))


def short(text, limit):
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def approval_text(stem, meta, body):
    tier = tier_of(meta)
    spec = body.split("## Acceptance Criteria")[0]
    lines = [f"🔐 Approval needed: {stem} (tier {tier}: {TIER_LABELS[tier]})", "", short(spec, 700), ""]
    lines.append("Acceptance criteria:\n" + short(extract_criteria(body) or "(none)", 700))
    lines.append("")
    lines.append("Tools: " + ", ".join(task_tools(meta)))
    lines.append(f"verify: {meta['verify']}" if meta.get("verify") else "verify: (none; the reviewer judges)")
    if meta.get("repo"):
        lines.append(f"repo: {meta['repo']} → the result is a draft pull request")
    if meta.get("proposed_by"):
        lines.append(f"Proposed by {meta['proposed_by']} (depth {meta.get('depth', '?')})")
    lines += ["", f"👍 run it · 👎 drop it   (or /approve {stem} · /reject {stem})"]
    return "\n".join(lines)


def announce_proposals():
    """Tell you about each task waiting in proposed/ that you haven't been told about (or whose spec changed)."""
    if not (cfg("TELEGRAM_BOT_TOKEN") and cfg("TELEGRAM_CHAT_ID")) or not PROPOSED.is_dir():
        return
    for path in sorted(PROPOSED.glob("*.md")):
        with ledger_lock():
            meta, body = parse_task(path)
            if meta is None:
                continue
            digest, entry = spec_digest(meta, body), read_ledger(path.stem)
            if entry and entry.get("status") == "awaiting" and entry.get("message_id") and entry.get("digest") == digest:
                continue
            message_id = send_message_id(approval_text(path.stem, meta, body), formatted=False)
            if not message_id:
                continue  # not sent: try again next time
            write_ledger(path.stem, {"task": path.stem, "tier": tier_of(meta), "digest": digest,
                                     "status": "awaiting", "message_id": message_id, "at": now_iso()})
            log.info("Task %s is waiting for approval (announced as message %s)", path.name, message_id)


def park_for_approval(path):
    """Move a task that may not run to proposed/, stamp its computed tier, and announce it."""
    meta, body = parse_task(path)
    meta["tier"] = str(tier_of(meta))
    write_task(path, meta, body)
    dest = PROPOSED / path.name
    if dest.exists():
        log.warning("Task %s needs approval but proposed/ already holds a file of that name; leaving it", path.name)
        return False
    PROPOSED.mkdir(parents=True, exist_ok=True)
    with ledger_lock():
        shutil.move(str(path), str(dest))
        write_ledger(path.stem, {"task": path.stem, "tier": int(meta["tier"]), "digest": spec_digest(meta, body),
                                 "status": "awaiting", "message_id": None, "at": now_iso()})
    log.info("Task %s is tier %s and needs approval; parked in proposed/", path.name, meta["tier"])
    announce_proposals()
    return True


def clean_stem(stem):
    stem = stem.strip()
    return stem[:-3] if stem.endswith(".md") else stem


@audited("approve")
def approve_task(stem):
    """Approve a task waiting in proposed/: record it and move it to pending/. Returns (ok, message)."""
    stem = clean_stem(stem)
    if not stem or "/" in stem:
        return False, "Give a task id (the file stem)."
    path = PROPOSED / f"{stem}.md"
    if not path.is_file():
        for label, directory in (("pending", PENDING), ("active", ACTIVE), ("done", DONE), ("cancelled", CANCELLED)):
            if (directory / f"{stem}.md").exists():
                return False, f"'{stem}' is not waiting for approval; it is in tasks/{label}/."
        return False, f"No task named '{stem}' is waiting for approval."
    with ledger_lock():
        entry = read_ledger(stem)
        meta, body = parse_task(path)
        problems = validate_task(meta, body) if meta else ["the frontmatter is missing"]
        if problems:
            return False, f"'{stem}' is invalid and was not queued:\n" + "\n".join(f"- {p}" for p in problems)
        if problem := proposal_problem(meta):
            return False, f"'{stem}' was not queued: {problem}."
        if not entry or entry.get("status") != "awaiting" or not entry.get("message_id"):
            return False, f"'{stem}' has not been announced yet; approve it from the announcement."
        digest = spec_digest(meta, body)
        if entry.get("digest") != digest:
            write_ledger(stem, {**entry, "digest": digest, "message_id": None})
            return False, (f"'{stem}' changed after it was announced, so that approval does not cover it. "
                           "It will be announced again.")
        write_ledger(stem, {**entry, "status": "approved", "at": now_iso()})
        shutil.move(str(path), str(PENDING / path.name))
    log.info("Task %s approved", stem)
    return True, f"Approved {stem}: queued, it runs next."


@audited("reject")
def reject_task(stem):
    """Drop a task waiting in proposed/ into cancelled/. Returns (ok, message)."""
    stem = clean_stem(stem)
    path = PROPOSED / f"{stem}.md"
    if not stem or "/" in stem or not path.is_file():
        return False, f"No task named '{stem}' is waiting for approval."
    with ledger_lock():
        meta, body = parse_task(path)
        write_task(path, meta or {}, body.rstrip("\n") + f"\n\n## Cancelled ({now_iso()})\n\nNot approved.\n")
        dest = CANCELLED / path.name
        if dest.exists():
            dest = CANCELLED / f"{path.stem}.cancelled-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        CANCELLED.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        entry = read_ledger(stem) or {"task": stem}
        write_ledger(stem, {**entry, "status": "rejected", "at": now_iso()})
    log.info("Task %s rejected", stem)
    return True, f"Dropped {stem}: moved to tasks/cancelled/."


def apply_reaction(message_id, emoji):
    """A 👍/👎 on an announcement: the reply to send, or None when the message is not one of ours."""
    if not message_id or not APPROVALS.is_dir():
        return None
    for ledger in sorted(APPROVALS.glob("*.json")):
        try:
            entry = json.loads(ledger.read_text())
        except (OSError, ValueError):
            continue
        if entry.get("message_id") != message_id:
            continue
        stem = entry["task"]
        if entry.get("status") != "awaiting":
            return f"{stem} was already {entry.get('status')}."
        if emoji == "👍":
            ok, message = approve_task(stem)
        elif emoji == "👎":
            ok, message = reject_task(stem)
        else:
            return None
        return ("✅ " if ok else "⚠️ ") + message
    return None


# ------------------------------------------- worktrees and draft pull requests
#
# A task with `repo:` runs in its own git worktree on branch task/<id>. Only after
# verify and review pass does the dispatcher itself commit, push and open a DRAFT
# pull request; the worker is told not to, and is denied git push and gh. Nothing
# here merges anything.

MAX_PUBLISH_FAILURES = 3
GIT_IDENTITY = ["-c", "user.name=Queue worker", "-c", "user.email=worker@localhost"]
REPO_NOTE = (
    "You are working in a git worktree on branch {branch}, cut from {base}. Do not run git commit, "
    "git push or gh: when verification and review pass, the dispatcher commits your changes and "
    "opens a draft pull request itself.\n\n"
)


class GitError(Exception):
    pass


def repo_path(meta):
    ref = (meta or {}).get("repo")
    if not ref:
        return None
    path = Path(ref)
    return path if path.is_absolute() else Path(cfg("REPOS_DIR") or BASE / "workspace") / ref


def repo_problem(meta):
    ref = (meta or {}).get("repo")
    if not ref:
        return None
    if not Path(ref).is_absolute() and ("/" in ref or ref in (".", "..")):
        return f"repo '{ref}' must be a plain name (looked up under REPOS_DIR) or an absolute path"
    path = repo_path(meta)
    if not path.is_dir():
        return f"repo '{ref}' not found (looked for {path})"
    if not (path / ".git").exists():
        return f"repo '{ref}' ({path}) is not a git checkout"
    return None


def safe_stem(stem):
    return re.sub(r"[^A-Za-z0-9._-]", "-", stem)


def branch_name(stem):
    return f"task/{safe_stem(stem)}"


def worktree_path(stem):
    return WORKTREES / safe_stem(stem)


def publish_env():
    env = claude_env()  # carries GH_TOKEN; git and gh have no use for the claude login
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")  # never wait for a password in an unattended run
    if env.get("GH_TOKEN"):
        # GH_TOKEN reaches `gh` by itself, but `git push` over https asks git's credential helpers, and the queue
        # user has none. Register `gh auth git-credential` for github.com only (the empty entry first clears any
        # inherited helper), so the same fine-grained token pushes the task branch.
        helper = f"!{cfg('GH_BIN', 'gh')} auth git-credential"
        entries = [("credential.https://github.com.helper", ""), ("credential.https://github.com.helper", helper)]
        env["GIT_CONFIG_COUNT"] = str(len(entries))
        for number, (key, value) in enumerate(entries):
            env[f"GIT_CONFIG_KEY_{number}"], env[f"GIT_CONFIG_VALUE_{number}"] = key, value
    return env


def git_run(cwd, *args, timeout=300):
    try:
        return subprocess.run(["git", *GIT_IDENTITY, *args], cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env=publish_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 124, "", f"{type(exc).__name__}: {exc}")


def git_need(cwd, *args, timeout=300):
    proc = git_run(cwd, *args, timeout=timeout)
    if proc.returncode != 0:
        raise GitError(f"git {args[0]} failed: {tail(proc.stderr or proc.stdout, 400)}")
    return proc.stdout.strip()


def base_ref(repo, meta):
    base = meta.get("base") or "main"
    return f"origin/{base}" if git_run(repo, "rev-parse", "--verify", "-q", f"origin/{base}").returncode == 0 else base


def prepare_worktree(meta, stem):
    """The task's worktree: new on the first attempt, the same one (with the last attempt's work) on a retry."""
    repo, branch, worktree = repo_path(meta), branch_name(stem), worktree_path(stem)
    git_run(repo, "worktree", "prune")
    if (worktree / ".git").exists():
        return worktree
    if "origin" in git_run(repo, "remote").stdout.split():
        git_run(repo, "fetch", "--quiet", "origin", timeout=120)  # best effort: an offline Pi still works from what it has
    WORKTREES.mkdir(parents=True, exist_ok=True)
    if git_run(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0:
        git_need(repo, "worktree", "add", "-q", str(worktree), branch)  # branch left by an earlier run
    else:
        git_need(repo, "worktree", "add", "-q", "-b", branch, str(worktree), base_ref(repo, meta))
    return worktree


def remove_worktree(meta, stem):
    worktree = worktree_path(stem)
    if repo_path(meta) is not None and worktree.exists():
        git_run(repo_path(meta), "worktree", "remove", "--force", str(worktree))
    shutil.rmtree(worktree, ignore_errors=True)


def prune_worktrees():
    """Delete worktrees of finished tasks once they are LOG_RETENTION_DAYS old (a failed task's is kept to look at)."""
    days = float(cfg("LOG_RETENTION_DAYS", "30"))
    if days <= 0 or not WORKTREES.is_dir():
        return
    cutoff = time.time() - days * 86400
    for worktree in WORKTREES.iterdir():
        if any((state / f"{worktree.name}.md").exists() for state in (PENDING, ACTIVE, PROPOSED)):
            continue
        try:
            if worktree.is_dir() and worktree.stat().st_mtime < cutoff:
                common = git_run(worktree, "rev-parse", "--git-common-dir").stdout.strip()
                if common:
                    git_run(Path(common).resolve().parent, "worktree", "remove", "--force", str(worktree))
                shutil.rmtree(worktree, ignore_errors=True)
        except OSError as exc:
            log.warning("could not prune worktree %s: %s", worktree.name, exc)


DIFF_GUARD_DENY_DEFAULT = ".venv/,venv/,node_modules/,__pycache__/,*.pyc,.env,*.pem,id_rsa*,*.sqlite"
GUARD_LISTED_MAX = 20  # findings named in the feedback; the rest are counted


def denied_path(path, patterns):
    """True when `path` matches a DIFF_GUARD_DENY entry: `name/` is a folder anywhere in the path, anything else a file glob."""
    parts = path.split("/")
    for pattern in patterns:
        if pattern.endswith("/"):
            if any(fnmatch.fnmatch(part, pattern.rstrip("/")) for part in parts[:-1]):
                return True
        elif fnmatch.fnmatch(parts[-1], pattern) or fnmatch.fnmatch(path, pattern):
            return True
    return False


def diff_guard(worktree, repo, meta):
    """Scan what the worker changed against the branch point, before anything is committed.

    Returns feedback for the worker when something must not be published, else "". The feedback
    names paths and kinds of secret, never a value. Deleted lines are not scanned.
    """
    patterns = [p.strip() for p in cfg("DIFF_GUARD_DENY", DIFF_GUARD_DENY_DEFAULT).split(",") if p.strip()]
    max_kb = float(cfg("DIFF_GUARD_MAX_KB", "1024"))
    try:
        git_need(worktree, "add", "-A")
        base = git_need(worktree, "merge-base", base_ref(repo, meta), "HEAD")
        changes = git_need(worktree, "-c", "core.quotepath=off", "diff", "--cached", "--name-status", "--no-renames", "-z", base)
        tokens = [t for t in changes.split("\0") if t]
        changed = list(zip(tokens[::2], tokens[1::2]))
        problems = []
        for status, path in changed:
            if denied_path(path, patterns):
                problems.append(f"- {path}: on the deny list (DIFF_GUARD_DENY)")
            elif status == "A":
                size_kb = int(git_need(worktree, "cat-file", "-s", f":{path}")) / 1024
                if size_kb > max_kb:
                    problems.append(f"- {path}: {size_kb:.0f} KB is over the {max_kb:g} KB limit for an added file (DIFF_GUARD_MAX_KB)")
        flagged = {path for _, path in changed if any(path in problem for problem in problems)}
        diff = git_need(worktree, "-c", "core.quotepath=off", "diff", "--cached", "-U0", "--no-color", "--no-renames", "--text", base)
    except (GitError, ValueError) as exc:
        return f"The dispatcher could not check the change before committing it ({exc}). Make sure the repository is healthy."
    found, path, header = {}, None, False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path, header = None, True
        elif header and line.startswith("+++ "):
            path = line[4:].removeprefix("b/") if line != "+++ /dev/null" else None
        elif line.startswith("@@"):
            header = False
        elif not header and line.startswith("+") and path and path not in flagged:
            for kind in secret_kinds(line[1:]):
                found.setdefault(path, [])
                if kind not in found[path]:
                    found[path].append(kind)
    problems += [f"- {path}: an added line looks like a {', '.join(kinds)} (the value is not repeated here)"
                 for path, kinds in found.items()]
    if not problems:
        return ""
    shown = "\n".join(problems[:GUARD_LISTED_MAX]) + (f"\n- ...and {len(problems) - GUARD_LISTED_MAX} more" if len(problems) > GUARD_LISTED_MAX else "")
    return (
        "The dispatcher refused to commit this change, so nothing was pushed and no pull request was opened:\n"
        f"{shown}\n"
        "Delete these files or lines from the working tree. If a file is a build or environment folder, add its "
        "path to .gitignore as well. Never put a key or token in a file; read it from the environment."
    )


def pr_title(stem, body):
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line and not line.lower().startswith("acceptance criteria"):
            return short(line, 72)
        if line:
            break
    return stem


def pr_body(stem, body, worker_out, verified):
    parts = [short(body.split("## Acceptance Criteria")[0], 1500), "**Acceptance criteria**\n" + short(extract_criteria(body) or "(none)", 1500)]
    if verified:
        parts.append(f"**Verification**: `{verified.command}` exited 0\n```\n{tail(verified.output or '(no output)', 1500)}\n```")
    parts.append("**Worker report**\n" + tail(worker_out, 2500))
    parts.append(f"---\nDraft pull request opened by the task queue from task `{stem}`. Nothing merges automatically.")
    return "\n\n".join(part for part in parts if part.strip())


def open_pr_count(cwd):
    proc = gh_run(cwd, "pr", "list", "--state", "open", "--limit", "100", "--json", "headRefName",
                  "--jq", '[.[] | select(.headRefName | startswith("task/"))] | length')
    try:
        if proc.returncode == 0:
            return int(proc.stdout.strip() or 0)
        detail = tail(proc.stderr or proc.stdout, 300)
    except ValueError as exc:
        detail = f"{type(exc).__name__}: {exc}"
    raise GitError(f"could not count open pull requests: {detail}")


PUBLISH_AUTH_RE = re.compile(
    r"\b40[13]\b|permission to \S+ denied|permission denied \(publickey|authentication failed|bad credentials|"
    r"could not read (?:username|password)|resource not accessible|invalid username or token|requires authentication",
    re.I,
)
PUBLISH_PERMISSIONS = ("WRITE", "MAINTAIN", "ADMIN")


def publish_auth_problem(text):
    """True when git or gh output says the credentials cannot do the job (a 401 or 403): a retry never helps."""
    return bool(PUBLISH_AUTH_RE.search(text or ""))


def gh_run(cwd, *args, timeout=120):
    command = [*shlex.split(cfg("GH_BIN", "gh")), *args]
    try:
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=publish_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 124, "", f"{type(exc).__name__}: {exc}")


PROBE_BRANCH = "taskq-publish-probe"  # named in a --dry-run push, never created


def publish_probe(repo):
    """(ok, detail): can this checkout's credentials write to its remote? One `gh repo view` and one `git push --dry-run`."""
    proc = gh_run(repo, "repo", "view", "--json", "viewerPermission", timeout=60)
    if proc.returncode != 0:
        return False, f"gh repo view failed: {tail(proc.stderr or proc.stdout, 300)}"
    try:
        permission = str(json.loads(proc.stdout).get("viewerPermission") or "").upper()
    except (ValueError, AttributeError):
        permission = ""
    if permission not in PUBLISH_PERMISSIONS:
        return False, f"the token has {permission or 'no'} permission on the repository; it needs WRITE"
    # The role above is the person's; a fine-grained token with Contents read-only still reports it. Only the
    # remote's answer to a push says whether this token may write, and a dry run asks without sending anything.
    remote = git_run(repo, "push", "--dry-run", "origin", f"HEAD:refs/heads/{PROBE_BRANCH}", timeout=60)
    if remote.returncode != 0:
        return False, f"git push --dry-run origin failed: {tail(remote.stderr or remote.stdout, 300)}"
    return True, ""


def remote_slug(repo):
    """owner/name of the checkout's origin on GitHub, else the folder name."""
    url = git_run(repo, "remote", "get-url", "origin").stdout.strip()
    match = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else Path(repo).name


def pull_request_info(cwd, ref):
    """What `gh pr view` says about a pull request (a URL or a branch name). Raises GitError if it cannot say."""
    proc = gh_run(cwd, "pr", "view", ref, "--json", "url,state,isDraft,headRefName")
    if proc.returncode != 0:
        raise GitError(f"gh pr view could not find the pull request: {tail(proc.stderr or proc.stdout, 300)}")
    try:
        info = json.loads(proc.stdout)
    except ValueError:
        info = None
    if not isinstance(info, dict):
        raise GitError(f"gh pr view gave no usable answer: {tail(proc.stdout, 200)}")
    return info


def pull_request_problems(cwd, info, branch, sha):
    """Why `info` is not the draft pull request we meant to open (empty when it is), checked against the remote too."""
    problems = []
    if str(info.get("state")).upper() != "OPEN":
        problems.append(f"it is {str(info.get('state')).lower()}, not open")
    if info.get("isDraft") is not True:
        problems.append("it is not a draft")
    if info.get("headRefName") != branch:
        problems.append(f"its head branch is {info.get('headRefName')!r}, not {branch!r}")
    remote = git_run(cwd, "ls-remote", "origin", f"refs/heads/{branch}")
    found = remote.stdout.split()[0] if remote.returncode == 0 and remote.stdout.split() else ""
    if found != sha:
        problems.append("the remote branch is not at the commit that was pushed")
    return problems


def publish_draft_pr(path, meta, body, worker_out, verified):
    """Commit the worktree, push task/<id>, open a draft PR and check it is what we claim. Returns (status, detail):
    ok (the URL), nochange, held (open PR count), denied (the credentials cannot publish: why) or error (why)."""
    stem, branch = path.stem, branch_name(path.stem)
    worktree, repo, base = worktree_path(path.stem), repo_path(meta), meta.get("base") or "main"
    title = pr_title(stem, body)
    try:
        git_need(worktree, "add", "-A")
        if git_run(worktree, "status", "--porcelain").stdout.strip():
            git_need(worktree, "commit", "-q", "-m", f"{title}\n\nTask {stem}, done by the queue worker.")
        if int(git_need(worktree, "rev-list", "--count", f"{base_ref(repo, meta)}..HEAD") or 0) == 0:
            return "nochange", ""
        cap = int(cfg("MAX_OPEN_PRS", "3") or 0)
        if cap > 0 and not meta.get("pr_url"):
            open_prs = open_pr_count(worktree)
            if open_prs >= cap:
                return "held", str(open_prs)
        git_need(worktree, "push", "-q", "-u", "origin", branch, timeout=300)
        sha = git_need(worktree, "rev-parse", "HEAD")
        with tempfile.TemporaryDirectory() as tmp:
            body_file = Path(tmp) / "body.md"
            body_file.write_text(pr_body(stem, body, worker_out, verified))
            proc = gh_run(worktree, "pr", "create", "--draft", "--base", base, "--head", branch,
                          "--title", title, "--body-file", str(body_file))
        if proc.returncode != 0:
            try:  # an earlier try may have opened it and failed only at the check below: use it, never delete its branch
                info = pull_request_info(worktree, branch)
            except GitError:
                info = None
            if info is None or str(info.get("state")).upper() != "OPEN":
                git_run(worktree, "push", "-q", "origin", "--delete", branch)  # no PR: don't leave a dangling branch
                raise GitError(f"gh pr create failed: {tail(proc.stderr or proc.stdout, 400)}")
            url = info.get("url") or ""
        else:
            url = next((line.strip() for line in reversed(proc.stdout.splitlines()) if line.strip()), "")
            if not url.startswith("http"):
                raise GitError(f"gh pr create printed no URL: {tail(proc.stdout, 200)}")
            info = pull_request_info(worktree, url)
        if problems := pull_request_problems(worktree, info, branch, sha):
            raise GitError(f"the pull request {url} did not check out: " + "; ".join(problems))
        return "ok", info.get("url") or url
    except (GitError, ValueError) as exc:
        return ("denied" if publish_auth_problem(str(exc)) else "error"), str(exc)


def conclude_pass(path, meta, body, attempts, max_attempts, worker_out, denials, cwd, verified=None, skipped=False):
    """A task passed verification and review. A repo task now becomes a draft PR; any other is simply done."""
    collect_proposals(path, meta)
    if not meta.get("repo"):
        finish_passed(path, meta, body, attempts, max_attempts, worker_out, denials, cwd, verified, skipped)
        return
    status, detail = publish_draft_pr(path, meta, body, worker_out, verified)
    stem = path.stem
    if status == "ok":
        for key in ("pending_publish", "publish_failures", "publish_blocked", "pending_review", "review_failures",
                    "held_until"):
            meta.pop(key, None)
        meta["pr_url"], meta["branch"] = detail, branch_name(stem)
        clear_reported(f"pr-held|tasks/pending/{path.name}")
        clear_reported(f"publish-denied|tasks/pending/{path.name}")
        finish_passed(path, meta, body, attempts, max_attempts, worker_out, denials, cwd, verified, skipped, pr_url=detail)
        remove_worktree(meta, stem)
    elif status == "nochange":
        for key in ("pending_publish", "publish_failures", "publish_blocked", "pending_review", "review_failures",
                    "held_until"):
            meta.pop(key, None)
        handle_failure(path, meta, body, attempts, max_attempts,
                       "The worker made no changes to the repository, so there is nothing to open a pull request for. "
                       "Make the change the task describes.")
    elif status == "denied":  # a setup problem: keep the work, use no attempt, say what to fix once, wait for the probe
        meta["pending_publish"], meta["publish_blocked"] = "1", "1"
        meta.pop("held_until", None)
        park_report(path, meta, body, attempts, worker_out, denials)
        slug = remote_slug(repo_path(meta))
        report_once(
            f"publish-denied|tasks/pending/{path.name}", "denied",
            f"Task {stem}: the credentials cannot publish to {slug}: {detail}",
            f"🔑 {path.name} is finished and checked, but the token cannot publish it to {slug}. In .env, GH_TOKEN "
            "needs Contents and Pull requests set to read and write on that one repository. Nothing is lost: the "
            "work is kept and publishes by itself on the next run once the token works. This is the only message "
            "about it.",
        )
    elif status == "held":
        meta["pending_publish"] = "1"
        if until := hold_until():
            meta["held_until"] = until
        park_report(path, meta, body, attempts, worker_out, denials)
        report_once(
            f"pr-held|tasks/pending/{path.name}", detail,
            f"Task {stem}: {detail} draft PRs are open (MAX_OPEN_PRS); result held",
            f"⏸ Draft PR for {path.name} is held: {detail} task pull requests are already open "
            f"(MAX_OPEN_PRS={cfg('MAX_OPEN_PRS', '3')}). The work is done and checked; it is published as soon as you merge or close one.",
        )
    else:
        failures = int(meta.get("publish_failures") or 0) + 1
        log.warning("Task %s: could not publish (%d/%d): %s", path.name, failures, MAX_PUBLISH_FAILURES, detail)
        meta["pending_publish"], meta["publish_failures"] = "1", str(failures)
        if failures < MAX_PUBLISH_FAILURES:
            if until := hold_until():
                meta["held_until"] = until
            park_report(path, meta, body, attempts, worker_out, denials)  # try again later; the work is not redone
            return
        meta.pop("pending_publish", None)
        meta.pop("held_until", None)
        body = body.rstrip("\n") + f"\n\n## Publish failed ({now_iso()})\n\n{detail}\n"
        write_task(path, meta, body)
        shutil.move(str(path), str(FAILED / path.name))
        send_report(f"❌ Task could not open a pull request: {path.name}\nThe work passed verification and review and is kept in {worktree_path(stem)}. Last error:",
                    detail, task_id=stem)


# ---------------------------------------------------------------- proposals
#
# A task with `propose: true` may write follow-up task files into a staging folder
# of its own, tasks/proposed/.incoming/<id>/, and nowhere else. When the task
# passes, the dispatcher (not the worker) validates each file, stamps proposed_by
# and depth, applies the caps, and moves the survivors to tasks/proposed/. There
# they wait for a 👍 like any task: a proposal never enters pending/ by itself.

PROPOSE_NOTE = (
    "You may propose follow-up work. Write each proposal as a task file (the usual frontmatter, a body, and a "
    "## Acceptance Criteria section) into {staging}/ and nowhere else, at most {limit} of them. They are only "
    "proposals: they wait for the human's approval before anything runs. Do not write to tasks/pending/.\n\n"
)
TASK_DIRS = (PENDING, PROPOSED, ACTIVE, DONE, FAILED, CANCELLED, RECURRING)
MANAGED_KEYS = ("tier", "pr_url", "branch", "pending_review", "review_failures", "pending_publish", "publish_failures",
                "publish_blocked")


def proposal_limits():
    return int(cfg("MAX_PROPOSAL_DEPTH", "2")), int(cfg("MAX_PROPOSALS_PER_TASK", "5"))


def staging_dir(stem):
    return PROPOSED / ".incoming" / safe_stem(stem)


def find_task(stem):
    return next((d / f"{stem}.md" for d in TASK_DIRS if (d / f"{stem}.md").exists()), None)


def count_proposals(stem):
    """Proposals this task has already made, wherever they have got to (a cancelled one still counts)."""
    return sum(
        1 for directory in TASK_DIRS for file in directory.glob("*.md")
        if (parse_task(file)[0] or {}).get("proposed_by") == stem
    )


def proposal_problem(meta):
    """Why a proposal may not be approved (None when it may): its parent and its depth must be what the dispatcher stamped."""
    parent_stem = meta.get("proposed_by")
    if not parent_stem:
        return None
    parent = find_task(parent_stem)
    if parent is None:
        return f"its parent task '{parent_stem}' is not in the queue"
    expected = int((parse_task(parent)[0] or {}).get("depth") or 0) + 1
    if str(meta.get("depth")) != str(expected):
        return f"its depth ({meta.get('depth')}) is not its parent's plus one ({expected})"
    if expected > proposal_limits()[0]:
        return f"depth {expected} is past the limit of {proposal_limits()[0]}"
    return None


def unique_name(stem):
    name, number = safe_stem(stem), 2
    while find_task(name) is not None:
        name, number = f"{safe_stem(stem)}-{number}", number + 1
    return name


def collect_proposals(path, meta):
    """Turn what a passed task left in its staging folder into proposals; then empty the folder."""
    staging = staging_dir(path.stem)
    if not staging.is_dir():
        return
    try:
        if meta.get("propose") != "true":
            return
        max_depth, per_task = proposal_limits()
        depth = int(meta.get("depth") or 0)
        files = sorted(staging.glob("*.md"), key=lambda f: (f.stat().st_mtime, f.name))
        kept, dropped = [], []
        if files and depth >= max_depth:
            dropped.append(f"all {len(files)}: this task is already at proposal depth {depth} (limit {max_depth})")
            files = []
        room = per_task - count_proposals(path.stem)
        for file in files:
            proposal, body = parse_task(file)
            if proposal is not None:  # the dispatcher's keys are the dispatcher's to set, whatever the worker wrote
                for key in MANAGED_KEYS:
                    proposal.pop(key, None)
                proposal.update({"attempts": "0", "proposed_by": path.stem, "depth": str(depth + 1)})
            problems = validate_task(proposal, body) if proposal is not None else ["no frontmatter"]
            if len(kept) >= room:
                dropped.append(f"{file.stem}: over the limit of {per_task} proposals per task")
            elif problems:
                dropped.append(f"{file.stem}: {problems[0]}")
            else:
                name = unique_name(file.stem)
                write_task(PROPOSED / f"{name}.md", proposal, body)
                kept.append(name)
        if kept or dropped:
            lines = []
            if kept:
                lines.append(f"💡 {path.stem} proposed {len(kept)} follow-up task{'s' if len(kept) != 1 else ''}: "
                             f"{', '.join(kept)}. They wait for your 👍.")
            lines += [f"Dropped {note}" for note in dropped]
            send_telegram("\n".join(lines))
            log.info("Task %s: %d proposal(s) kept, %d dropped", path.name, len(kept), len(dropped))
            announce_proposals()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ------------------------------------------------------------------ holds, enqueue


def hold_until():
    """When a held or failed publish may next be tried (None: at once)."""
    minutes = float(cfg("PUBLISH_RETRY_MINUTES", "10") or 0)
    if minutes <= 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime(ISO_FMT)


def is_held(meta):
    try:
        return datetime.strptime(meta["held_until"], ISO_FMT).replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False


SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def enqueue_task(arg):
    """Validate a task file another program wrote and drop it into the queue. Returns (ok, message).

    It arrives whole (a hard link from a temporary file next to pending/, so a watcher on pending/
    sees one event and never a half-written file) and never overwrites a task of the same name.
    A task that needs approval goes to proposed/ and is announced, exactly as if pick_task had found it.
    """
    path = Path(arg)
    if not path.is_file():
        return False, f"{arg}: no such file"
    if not SAFE_NAME.match(path.stem):
        return False, f"{path.name}: the name may only use letters, digits, '.', '_' and '-'"
    meta, body = parse_task(path)
    problems = validate_task(meta, body)
    if problems:
        return False, f"{path.name} is invalid:\n" + "\n".join(f"- {p}" for p in problems)
    if (existing := find_task(path.stem)) is not None:
        return False, f"{path.stem} is already in tasks/{existing.parent.name}/; pick another name"
    gated = needs_approval(path, meta, body)
    destination = (PROPOSED if gated else PENDING) / path.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = TASKS / f".enqueue-{os.getpid()}-{path.name}.tmp"
    try:
        if gated:
            meta["tier"] = str(tier_of(meta))
            write_task(temporary, meta, body)
        else:
            shutil.copyfile(path, temporary)
        try:
            os.link(temporary, destination)  # fails rather than overwrite
        except FileExistsError:
            return False, f"{path.stem} is already in tasks/{destination.parent.name}/; pick another name"
    finally:
        temporary.unlink(missing_ok=True)
    if not gated:
        log.info("Enqueued %s", path.name)
        return True, f"Queued {path.name} in tasks/pending/; the next dispatcher run picks it up."
    with ledger_lock():
        write_ledger(path.stem, {"task": path.stem, "tier": int(meta["tier"]), "digest": spec_digest(meta, body),
                                 "status": "awaiting", "message_id": None, "at": now_iso()})
    announce_proposals()
    log.info("Enqueued %s: tier %s, waiting for approval", path.name, meta["tier"])
    return True, f"{path.name} is tier {meta['tier']} and needs approval: waiting in tasks/proposed/ (announced on Telegram)."


def pick_task(skip=()):
    candidates = sorted(PENDING.glob("*.md"), key=lambda p: (p.stat().st_mtime, p.name))
    waiting = 0
    for src in candidates:
        if src.stem in skip:  # already handled in this run
            continue
        meta, body = parse_task(src)
        if meta and is_held(meta):  # a held PR or a failed publish: not before its time
            continue
        if meta and meta.get("publish_blocked") and not repo_problem(meta):
            ok, detail = publish_probe(repo_path(meta))
            if not ok:  # still broken: one cheap probe per run, no push, no message (it was said once)
                log.info("Task %s: publishing is still blocked (%s)", src.name, detail)
                continue
            meta.pop("publish_blocked", None)
            write_task(src, meta, body)
            log.info("Task %s: the publish probe passed; publishing", src.name)
        blocked = None
        cancelled_dep = None
        for dep in dep_names(meta):
            if dep == src.stem:
                log.warning("Task %s depends on itself and will never run", src.name)
                blocked = "waiting"
                break
            state = dep_state(dep)
            if state == "done":
                continue
            if state == "failed":
                cascade_dependency_failure(src, dep)
                blocked = "failed"
                break
            if state == "cancelled":
                cancelled_dep = dep
                blocked = "waiting"
                break
            if state == "missing":
                log.warning(
                    "Task %s is waiting on dependency %r, which is not in any queue "
                    "directory — typo, or queue it later", src.name, dep,
                )
            else:
                log.info("Task %s is waiting on dependency %r", src.name, dep)
            blocked = "waiting"
            break
        cancelled_key = f"cancelled-dependency|tasks/pending/{src.name}"
        if cancelled_dep is not None:
            report_once(
                cancelled_key, cancelled_dep,
                f"Task {src.name} is waiting on dependency {cancelled_dep!r}, which was cancelled — it "
                "will never run; cancel it too, or move the dependency back to pending/",
                f"⚠️ {src.name} will never run: it depends on '{cancelled_dep}', which was cancelled.\n"
                f"Cancel {src.name} too, or restore '{cancelled_dep}'. This is the only notice.",
            )
        else:
            clear_reported(cancelled_key)
        if blocked is None:
            if needs_approval(src, meta, body):
                park_for_approval(src)
                continue
            dest = ACTIVE / src.name
            shutil.move(str(src), str(dest))
            return dest
        if blocked == "waiting":
            waiting += 1
    if waiting:
        log.info("%d task(s) waiting on dependencies; nothing runnable this cycle", waiting)
    return None


def handle_failure(path, meta, body, attempts, max_attempts, feedback):
    log_lesson(path, attempts, feedback)
    body = body.rstrip("\n") + f"\n\n## Attempt {attempts} Feedback ({now_iso()})\n\n{feedback.strip()}\n"
    write_task(path, meta, body)
    if attempts >= max_attempts:
        shutil.rmtree(staging_dir(path.stem), ignore_errors=True)  # a failed task's proposals are not trusted
        shutil.move(str(path), str(FAILED / path.name))
        log.error("Task %s FAILED permanently after %d/%d attempts", path.name, attempts, max_attempts)
        send_report(
            f"❌ Task failed: {path.name}\nExhausted {max_attempts} attempts. Last feedback:", feedback,
            task_id=path.stem,
        )
    else:
        shutil.move(str(path), str(PENDING / path.name))
        log.info("Task %s failed review on attempt %d/%d; requeued with feedback", path.name, attempts, max_attempts)


def parse_verdict(review_out):
    """Return (verdict, feedback). Unparseable output counts as FAIL."""
    match = re.search(r"VERDICT:\s*(PASS|FAIL)", review_out, re.I)
    if not match:
        return "FAIL", "Reviewer output had no parseable verdict:\n" + tail(review_out)
    feedback = re.sub(r".*?VERDICT:\s*(PASS|FAIL)[^\n]*\n?", "", review_out, count=1, flags=re.S)
    return match.group(1).upper(), feedback.strip() or "(no feedback given)"


def escalation_model(meta):
    """Model for attempts after the first: the task's own choice, else Sonnet. Opus only when a task names it."""
    if meta.get("escalation_model"):
        return meta["escalation_model"]
    if "opus" in meta["model"].lower():
        return meta["model"]  # the task already chose the strongest model; don't downgrade it
    return cfg("DEFAULT_ESCALATION_MODEL", "claude-sonnet-5")


def requeue_unconsumed(path, meta, body, attempts):
    """Put the task back as if this attempt never started: a limit or a login problem costs no attempt."""
    meta["attempts"] = str(attempts - 1)
    write_task(path, meta, body)
    shutil.move(str(path), str(PENDING / path.name))


def park_report(path, meta, body, attempts, worker_out, denials):
    """The review can't run now: keep the worker's finished report and requeue for a review-only retry."""
    report = LOGS / f"{path.stem}.attempt-{attempts}.worker.txt"
    write_atomic(report, worker_out)
    sidecar = report.with_suffix(".json")
    if denials:
        write_atomic(sidecar, json.dumps({"permission_denials": denials}))
    else:
        sidecar.unlink(missing_ok=True)
    meta["pending_review"] = str(report.relative_to(BASE))
    requeue_unconsumed(path, meta, body, attempts)


def load_parked_report(meta):
    """(worker report, permission denials) saved by park_report, or None when there is nothing usable to reuse.

    The path comes from the task file, which a coordinator wrote, so it is only
    trusted inside logs/: anything else would let a task read any file and
    have it reviewed, stored and sent to Telegram.
    """
    ref = meta.get("pending_review")
    if not ref:
        return None
    report = BASE / ref
    try:
        if not report.resolve().is_relative_to(LOGS.resolve()):
            raise OSError("outside logs/")
        text = report.read_text()
    except OSError as exc:
        log.warning("pending_review %s is unusable (%s); running the worker again", ref, exc)
        meta.pop("pending_review", None)
        meta.pop("review_failures", None)
        return None
    try:
        denials = json.loads(report.with_suffix(".json").read_text()).get("permission_denials") or []
    except (OSError, json.JSONDecodeError, AttributeError):
        denials = []
    return text, denials


def denial_notice(denials):
    """One Telegram line naming the tool calls claude refused, so a report can't lean on commands that never ran."""
    shown = []
    for denial in denials or []:
        if not isinstance(denial, dict):
            continue
        tool_input = denial.get("tool_input") or {}
        detail = next((str(tool_input[k]) for k in ("command", "file_path", "path", "url", "pattern")
                       if tool_input.get(k)), "")
        name = denial.get("tool_name") or "tool"
        item = detail if name == "Bash" and detail else f"{name}({detail})" if detail else name
        shown.append("`" + " ".join(item.replace("`", "'").split())[:120] + "`")
    if not shown:
        return ""
    more = f" and {len(shown) - 5} more" if len(shown) > 5 else ""
    return f"⚠️ Permission denied, so these never ran: {', '.join(shown[:5])}{more}"


VERIFY_TAIL_CHARS = 3000  # how much of a verify command's output goes to feedback and the reviewer
VERIFY_READ_BYTES = 64 * 1024  # and how much of its log file is read back to find that tail

VERIFICATION_BLOCK = """
## Mechanical Verification
After the worker finished, the dispatcher itself ran `{command}` in the working \
directory. It exited 0. The end of its output:
```
{output}
```
"""


class VerifyResult(NamedTuple):
    command: str
    exit_code: int | None  # None when the command never started or was stopped
    output: str  # the tail of what it printed (stdout and stderr together)
    problem: str  # empty when it exited 0; otherwise why the attempt fails


def verify_env(cwd):
    """The only environment a verify command sees: no tokens, no inherited variables."""
    path = os.pathsep.join([str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"])
    return {"PATH": path, "HOME": cwd, "LC_ALL": "C.UTF-8"}


def run_verify(command, cwd, timeout_s, output_path, transcript):
    """Run a task's `verify:` command the way validate_task allowed it: split with shlex, no shell, scrubbed env.

    The output is written to `output_path`, and only its tail is read back.
    """
    refusal = verify_problem(command)  # validate_task checked already; a file edited since must not slip through
    if refusal:
        return VerifyResult(command, None, "", f"Verification not run: {refusal}.")
    append_transcript(transcript, f"\n=== verify @ {now_iso()} ===\n$ {command}")
    timed_out = False
    with open(output_path, "wb") as out:
        try:
            proc = subprocess.Popen(
                shlex.split(command), cwd=cwd, env=verify_env(cwd), stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
            )
        except OSError as exc:
            return VerifyResult(command, None, "", f"Verification could not start `{command}`: {exc}.")
        try:
            code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            code = proc.wait()
    with open(output_path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - VERIFY_READ_BYTES))
        output = tail(fh.read().decode(errors="replace"), VERIFY_TAIL_CHARS)
    append_transcript(transcript, f"[verify output]\n{output}\n[verify {'TIMED OUT' if timed_out else f'exit {code}'}]")
    if timed_out:
        problem = f"Verification stopped: `{command}` did not finish within {timeout_s / 60:g} minutes."
    elif code != 0:
        problem = f"Verification failed: the dispatcher ran `{command}` after your attempt and it exited {code}."
    else:
        problem = ""
    return VerifyResult(command, None if timed_out else code, output, problem)


def verify_feedback(result):
    return f"{result.problem}\nFix what it reports. The end of its output:\n```\n{result.output or '(no output)'}\n```"


def check_file(arg):
    """Validate one task or recurring template file. Returns (ok, message); the coordinator runs this after writing a file."""
    path = Path(arg)
    if not path.is_file():
        return False, f"{arg}: no such file"
    meta, body = parse_task(path)
    problems = validate_task(meta, body, template=path.resolve().parent.name == "recurring")
    if problems:
        return False, f"{path.name} is invalid:\n" + "\n".join(f"- {p}" for p in problems)
    return True, f"{path.name}: OK"


def finish_passed(path, meta, body, attempts, max_attempts, worker_out, denials, cwd, verified=None, skipped=False,
                  pr_url=None):
    """A task passed: record the result, move it to done/ and tell the user."""
    name = path.name
    result = worker_out.strip()
    if verified:
        result += (
            f"\n\nVerified by the dispatcher: `{verified.command}` exited 0.\n"
            f"```\n{verified.output or '(no output)'}\n```"
        )
    body = body.rstrip("\n") + f"\n\n## Result (attempt {attempts}, {now_iso()})\n\n{result}\n"
    write_task(path, meta, body)
    shutil.move(str(path), str(DONE / name))
    log.info("Task %s PASSED%s on attempt %d/%d; moved to done/", name,
             " verification (review skipped)" if skipped else " review", attempts, max_attempts)
    lines = [f"✅ Task done: {name} (attempt {attempts}/{max_attempts})"]
    if verified:
        lines.append(f"Verified by `{verified.command}` (exit 0)" + ("; review skipped" if skipped else ""))
    if notice := denial_notice(denials):
        lines.append(notice)
    if pr_url:
        lines.append(f"🔀 Draft PR ready: {pr_url}")
    files, notes = collect_deliverables(meta, cwd)
    if meta.get("quiet") != "true" or pr_url:  # quiet tasks (the weekly improver) report only failures, and PR links
        send_report("\n".join(lines), worker_out.strip(), files=files, task_id=path.stem, root=cwd)
    if notes:
        send_telegram("⚠️ Not delivered:\n" + "\n".join(f"- {note}" for note in notes))


def process_task(path):
    """Run one task through worker, verify and review, with a live status message while the worker works."""
    meta, _ = parse_task(path)
    status = start_status(path, meta or {})
    try:
        run_task(path, status)
    finally:
        if status is not None:
            status.finish(status_outcome(path.name))


def status_outcome(name):
    """How the attempt ended, judged by where the task file went."""
    if (DONE / name).exists():
        return "✅ Passed"
    if (FAILED / name).exists():
        return "❌ Failed"
    if (PENDING / name).exists():
        return "↩️ Needs another attempt (or waiting for the limit to reset)"
    return "⏹ Stopped"


def run_task(path, status=None):
    name = path.name
    meta, body = parse_task(path)

    problems = validate_task(meta, body)
    if problems:
        detail = "\n".join(f"- {p}" for p in problems)
        log.error("Task %s is invalid:\n%s", name, detail)
        body = body.rstrip("\n") + f"\n\n## Invalid Task ({now_iso()})\n\n{detail}\n"
        write_task(path, meta or {}, body)
        shutil.move(str(path), str(FAILED / name))
        send_report(f"❌ Task rejected as invalid: {name}", detail, task_id=path.stem)
        return

    claude_bin = resolve_claude_bin()
    if claude_bin is None:
        log.error(
            "No claude binary found (CLAUDE_BIN=%r in .env). Set CLAUDE_BIN to the "
            "absolute path from 'which claude'. Task %s requeued untouched.",
            cfg("CLAUDE_BIN"), name,
        )
        shutil.move(str(path), str(PENDING / name))
        return

    if needs_approval(path, meta, body):  # belt and braces: nothing unapproved reaches a claude call
        park_for_approval(path)
        return

    attempts = int(meta.get("attempts") or 0) + 1
    max_attempts = int(meta["max_attempts"])
    meta["attempts"] = str(attempts)
    meta["tier"] = str(tier_of(meta))  # computed, never taken from the file
    write_task(path, meta, body)

    timeout_min = float(meta.get("timeout_minutes") or cfg("DEFAULT_TIMEOUT_MINUTES", "30"))
    transcript = LOGS / f"{path.stem}.attempt-{attempts}.log"
    if meta.get("repo"):
        try:
            cwd = str(prepare_worktree(meta, path.stem))
        except GitError as exc:
            handle_failure(path, meta, body, attempts, max_attempts, f"Could not prepare the git worktree:\n{exc}")
            return
    else:
        cwd = task_cwd(meta)
        Path(cwd).mkdir(parents=True, exist_ok=True)

    parked = load_parked_report(meta)
    if parked is None:
        meta.pop("pending_publish", None)  # nothing saved to publish: the worker has to run again
    if parked is not None:
        worker_out, denials = parked
        log.info("Task %s: attempt %d/%d reuses the saved worker report and runs only the review",
                 name, attempts, max_attempts)
    else:
        model = meta["model"] if attempts == 1 else escalation_model(meta)
        tools = meta.get("allowed_tools") or cfg("DEFAULT_ALLOWED_TOOLS", "Read,Glob,Grep,Edit,Write")
        log.info(
            "Task %s: attempt %d/%d, model=%s, tools=[%s], timeout=%.0f min, cwd=%s",
            name, attempts, max_attempts, model, tools, timeout_min, cwd,
        )
        worker_cmd = [
            claude_bin, "-p",
            "--model", model,
            "--allowedTools", tools,
            "--output-format", "stream-json", "--verbose",
        ]
        mcp = mcp_config_path(meta)
        if mcp is not None:
            worker_cmd += ["--mcp-config", str(mcp)]
            log.info("Task %s: MCP servers from %s", name, mcp)
        note = ""
        if meta.get("repo"):  # the dispatcher publishes; the worker may neither push nor call gh
            worker_cmd += ["--disallowedTools", "Bash(git push*),Bash(gh *)"]
            note = REPO_NOTE.format(branch=branch_name(path.stem), base=meta.get("base") or "main")
        if meta.get("propose") == "true":  # its own staging folder, and only that
            staging = staging_dir(path.stem)
            worker_cmd[worker_cmd.index("--allowedTools") + 1] = f"{tools},Write(/{staging}/**)"
            note += PROPOSE_NOTE.format(staging=staging, limit=proposal_limits()[1])
        try:
            reply = run_claude(
                worker_cmd, WORKER_PREAMBLE + "\n\n" + note + body, timeout_min * 60, cwd, transcript, "worker",
                watcher=status or QuietWatcher(),
            )
        except RateLimited as exc:
            requeue_unconsumed(path, meta, body, attempts)
            log.warning("Task %s: usage limit; requeued without consuming an attempt", name)
            pause_queue(usage_limit_until(str(exc)), "usage limit")
            return
        except AuthError as exc:
            requeue_unconsumed(path, meta, body, attempts)
            log.warning("Task %s: requeued without consuming an attempt", name)
            mark_auth_failed(str(exc))
            return
        except CallTimeout:
            handle_failure(
                path, meta, body, attempts, max_attempts,
                f"Worker timed out after {timeout_min:g} minutes without completing. "
                f"Finish faster or the task may need a larger timeout_minutes.",
            )
            return
        except ClaudeError as exc:
            handle_failure(path, meta, body, attempts, max_attempts, f"Worker invocation failed:\n{tail(str(exc))}")
            return
        worker_out, denials = reply.text, reply.envelope.get("permission_denials") or []

    if meta.get("repo") and (problem := diff_guard(Path(cwd), repo_path(meta), meta)):
        meta.pop("pending_review", None)  # the saved report described files that may not be published
        meta.pop("review_failures", None)
        meta.pop("pending_publish", None)
        log.warning("Task %s: diff guard refused the change", name)
        handle_failure(path, meta, body, attempts, max_attempts, problem)
        return

    verified, verification = None, ""
    if meta.get("verify"):
        verify_timeout = float(cfg("VERIFY_TIMEOUT_MINUTES", "10")) * 60
        verified = run_verify(
            meta["verify"], cwd, verify_timeout, LOGS / f"{path.stem}.attempt-{attempts}.verify.log", transcript
        )
        if verified.problem:  # a bad attempt costs the worker call only: no review call is made
            meta.pop("pending_review", None)
            meta.pop("review_failures", None)
            handle_failure(path, meta, body, attempts, max_attempts, verify_feedback(verified))
            return
        if meta.get("review") == "skip":
            meta.pop("pending_review", None)
            meta.pop("review_failures", None)
            conclude_pass(path, meta, body, attempts, max_attempts, worker_out, denials, cwd, verified, skipped=True)
            return
        verification = VERIFICATION_BLOCK.format(command=verified.command, output=verified.output or "(no output)")

    if meta.get("pending_publish"):  # the review already passed on an earlier run; only publishing is left
        conclude_pass(path, meta, body, attempts, max_attempts, worker_out, denials, cwd, verified)
        return

    review_cmd = [
        claude_bin, "-p",
        "--model", meta["review_model"],
        "--allowedTools", "Read,Glob,Grep",
        "--output-format", "json",
    ]
    review_prompt = REVIEW_TEMPLATE.format(
        criteria=extract_criteria(body), verification=verification, output=tail(worker_out, 20000)
    )
    review_timeout = float(cfg("REVIEW_TIMEOUT_MINUTES", "10")) * 60
    try:
        review_out = run_claude(review_cmd, review_prompt, review_timeout, cwd, transcript, "review").text
    except RateLimited as exc:
        park_report(path, meta, body, attempts, worker_out, denials)
        log.warning("Task %s: usage limit during review; worker report saved, no attempt used", name)
        pause_queue(usage_limit_until(str(exc)), "usage limit")
        return
    except AuthError as exc:
        park_report(path, meta, body, attempts, worker_out, denials)
        log.warning("Task %s: review could not run; worker report saved, no attempt used", name)
        mark_auth_failed(str(exc))
        return
    except (CallTimeout, ClaudeError) as exc:
        failures = int(meta.get("review_failures") or 0) + 1
        if failures < MAX_REVIEW_RETRIES:
            meta["review_failures"] = str(failures)
            park_report(path, meta, body, attempts, worker_out, denials)
            log.warning("Task %s: review failed (%s), try %d/%d; worker report saved, no attempt used",
                        name, type(exc).__name__, failures, MAX_REVIEW_RETRIES)
        else:  # a review that never works must not loop forever: charge the attempt as before
            meta.pop("pending_review", None)
            meta.pop("review_failures", None)
            handle_failure(
                path, meta, body, attempts, max_attempts,
                f"Review could not be completed after {failures} tries ({type(exc).__name__}: "
                f"{tail(str(exc), 300) or 'timed out'}).",
            )
        return

    meta.pop("pending_review", None)
    meta.pop("review_failures", None)
    verdict, feedback = parse_verdict(review_out)
    if verdict == "PASS":
        conclude_pass(path, meta, body, attempts, max_attempts, worker_out, denials, cwd, verified)
    else:
        handle_failure(path, meta, body, attempts, max_attempts, feedback)


# ---------------------------------------------------------------- main


def prune_logs():
    """Delete per-attempt transcripts older than LOG_RETENTION_DAYS, so logs don't wear out an SD card."""
    days = float(cfg("LOG_RETENTION_DAYS", "30"))
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    for path in LOGS.glob("*.attempt-*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError as exc:
            log.warning("could not prune %s: %s", path.name, exc)


def ping_healthcheck():
    """Tell the outside dead-man's switch this cycle ran. Silent when the login is expired, so its alert fires."""
    url = cfg("HEALTHCHECK_URL")
    if not url or AUTH_FAILED_FILE.exists():
        return
    try:
        httpx.get(url, timeout=10).raise_for_status()
    except httpx.HTTPError as exc:  # the URL may carry a secret: name the error type only
        log.warning("healthcheck ping failed: %s", type(exc).__name__)


def main():
    status = cycle()
    if status == 0:
        ping_healthcheck()
    return status


def cycle():
    for directory in (PENDING, ACTIVE, DONE, FAILED, RECURRING, CANCELLED, PROPOSED, LOGS, STATE):
        directory.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOGS / "dispatcher.log"), logging.StreamHandler(sys.stdout)],
    )
    for noisy in ("httpx", "httpcore"):  # httpx logs each request URL at INFO, and Telegram's contains the bot token
        logging.getLogger(noisy).setLevel(logging.WARNING)

    lock = open(LOCKFILE, "w")
    give_up = time.monotonic() + float(cfg("DISPATCH_LOCK_WAIT_SECONDS", "0") or 0)
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= give_up:  # no wait (cron), or the wait is over
                log.info("Another dispatcher run is active; exiting")
                return 0
            time.sleep(0.5)  # a run started by a new file waits for the one that is working
    lock.write(str(os.getpid()))
    lock.flush()

    if queue_is_stopped():  # a usage-limit pause, or a login that still fails its probe
        return 0
    prune_seen()
    recover_stale()
    prune_logs()
    prune_worktrees()
    spawn_recurring()
    if daily_cap_reached():
        return 0
    announce_proposals()  # anything the coordinator left in proposed/
    drain = cfg("DISPATCH_DRAIN") in ("1", "true", "yes")
    handled = set()  # a task that needs another attempt waits for the next run, not a tight loop
    while True:
        task = pick_task(skip=handled)
        if task is None:
            log.info("Queue empty; nothing to do")
            return 0
        handled.add(task.stem)
        process_task(task)
        if not drain or queue_is_stopped() or daily_cap_reached():
            return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        actions = {"cancel": cancel_task, "retry": retry_task, "check": check_file, "enqueue": enqueue_task}
        if sys.argv[1] in actions and len(sys.argv) == 3:
            ok, message = actions[sys.argv[1]](sys.argv[2])
            print(message)
            sys.exit(0 if ok else 1)
        if sys.argv[1] == "usage" and len(sys.argv) == 2:
            sys.exit(usage_report())
        print(
            "Usage: dispatcher.py                   run one queue cycle\n"
            "       dispatcher.py cancel <task-id>  archive a queued task to tasks/cancelled/\n"
            "       dispatcher.py retry <task-id>   requeue a failed/cancelled task, attempts reset\n"
            "       dispatcher.py check <file>      validate a task or recurring template file\n"
            "       dispatcher.py enqueue <file>    validate a task file and drop it into the queue\n"
            "       dispatcher.py usage             claude calls, cost and turns by day and model",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(main())
