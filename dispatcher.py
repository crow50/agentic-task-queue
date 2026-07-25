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
Stdlib only — no pip installs needed.

Usage: python3 dispatcher.py   (typically from cron every 15 minutes)
"""

import fcntl
import json
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
TASKS = BASE / "tasks"
PENDING = TASKS / "pending"
ACTIVE = TASKS / "active"
DONE = TASKS / "done"
FAILED = TASKS / "failed"
RECURRING = TASKS / "recurring"
LOGS = BASE / "logs"
LOCKFILE = BASE / "dispatcher.lock"

RATE_LIMIT_RE = re.compile(r"rate.?limit|\b429\b|overloaded|usage limit|quota", re.I)
AUTH_ERROR_RE = re.compile(r"not logged in|please run /login|invalid api key|authentication", re.I)

WORKER_PREAMBLE = """\
You are running unattended inside an automated task queue. Complete the task \
described below.
- Sections named "## Attempt N Feedback" contain reviewer feedback from \
previous failed attempts; address every point before finishing.
- Work in the current directory unless the task says otherwise.
- When you are done, print a concise report of what you did, the files you \
created or changed (with paths), and how each acceptance criterion is met.
"""

REVIEW_TEMPLATE = """\
You are a strict automated reviewer for a task queue. Decide whether the \
worker's output satisfies every acceptance criterion below. You may use \
Read/Glob/Grep on the current directory to verify files the worker claims to \
have created.

The first line of your reply must be exactly "VERDICT: PASS" or \
"VERDICT: FAIL" (nothing else on that line). If FAIL, follow with concise \
bullet points telling the worker exactly what is missing or wrong so it can \
fix it on the next attempt.

## Acceptance Criteria
{criteria}

## Worker Report
{output}
"""

log = logging.getLogger("dispatcher")


class RateLimited(Exception):
    """Rate limit persisted through all backoff retries."""


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
        meta[key.strip()] = value.strip().strip("\"'")
    return meta, match.group(2)


def write_task(path, meta, body):
    front = "\n".join(f"{k}: {v}" for k, v in meta.items())
    path.write_text(f"---\n{front}\n---\n{body}")


def mcp_config_path(meta):
    """Resolve the task's MCP config: frontmatter, then .env default. None if unset."""
    raw = (meta or {}).get("mcp_config") or cfg("DEFAULT_MCP_CONFIG")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else BASE / path


def extract_criteria(body):
    match = re.search(
        r"^##\s*Acceptance Criteria\s*\n(.*?)(?=^##\s|\Z)", body, re.S | re.M
    )
    return match.group(1).strip() if match else None


def validate_task(meta, body):
    problems = []
    if meta is None:
        return ["missing YAML frontmatter (--- block at top of file)"]
    for key in ("model", "review_model", "max_attempts"):
        if not meta.get(key):
            problems.append(f"frontmatter is missing required key '{key}'")
    if meta.get("max_attempts"):
        try:
            int(meta["max_attempts"])
        except ValueError:
            problems.append("max_attempts must be an integer")
    if extract_criteria(body) is None:
        problems.append("no '## Acceptance Criteria' section in task body")
    mcp = mcp_config_path(meta)
    if mcp is not None and not mcp.is_file():
        problems.append(f"mcp_config file not found: {mcp}")
    return problems


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tail(text, limit=2000):
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


# ---------------------------------------------------------------- claude calls


def append_transcript(transcript, text):
    with open(transcript, "a") as fh:
        fh.write(text.rstrip("\n") + "\n")


def extract_result(stdout):
    """Pull the 'result' field out of --output-format json; fall back to raw."""
    try:
        data = json.loads(stdout)
        if isinstance(data, dict) and "result" in data:
            return str(data["result"])
    except (json.JSONDecodeError, TypeError):
        pass
    return stdout


def run_claude(cmd, prompt, timeout_s, cwd, transcript, label):
    """Run one claude -p invocation with exponential backoff on rate limits."""
    max_retries = int(cfg("MAX_RATE_LIMIT_RETRIES", "5"))
    base_delay = float(cfg("RATE_LIMIT_BASE_DELAY", "30"))
    for retry in range(max_retries + 1):
        append_transcript(transcript, f"\n=== {label} @ {now_iso()} ===\n$ {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:  # binary vanished or isn't executable
            raise ClaudeError(f"cannot execute {cmd[0]!r}: {exc}")
        try:
            out, err = proc.communicate(prompt, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            append_transcript(transcript, f"[{label}] TIMED OUT after {timeout_s:.0f}s")
            raise CallTimeout(timeout_s)
        append_transcript(
            transcript,
            f"[{label} stdout]\n{out}\n[{label} stderr]\n{err}\n[{label} exit {proc.returncode}]",
        )
        if proc.returncode == 0:
            return extract_result(out)
        if RATE_LIMIT_RE.search(out + err):
            if retry < max_retries:
                delay = min(base_delay * 2**retry + random.uniform(0, base_delay / 3), 600)
                log.warning(
                    "%s: rate limited (exit %d); backing off %.1fs (retry %d/%d)",
                    label, proc.returncode, delay, retry + 1, max_retries,
                )
                time.sleep(delay)
                continue
            raise RateLimited()
        # Prefer the concise "result" message from the JSON envelope over the raw blob.
        message = extract_result(out).strip() or (out + "\n" + err).strip()
        if AUTH_ERROR_RE.search(message) or AUTH_ERROR_RE.search(err):
            raise AuthError(message)
        raise ClaudeError(message)


# ---------------------------------------------------------------- telegram


TELEGRAM_MAX = 4096


def truncate_for_telegram(text):
    """Fit text into one Telegram message, cutting on a line break with a marker."""
    if len(text) <= TELEGRAM_MAX:
        return text
    marker = "\n\n[… truncated — full report is in the task file]"
    text = text[: TELEGRAM_MAX - len(marker)]
    cut = text.rfind("\n")
    if cut > TELEGRAM_MAX // 2:
        text = text[:cut]
    return text + marker


def send_telegram(text):
    token = cfg("TELEGRAM_BOT_TOKEN")
    chat_id = cfg("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID); skipping notification")
        return
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": truncate_for_telegram(text)}).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30
        )
        log.info("Telegram notification sent")
    except Exception as exc:  # notification failure must never fail the task
        log.warning("Telegram notification failed: %s", exc)


# ---------------------------------------------------------------- recurring

DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
LAST_RUN_FMT = "%Y-%m-%dT%H:%M:%S"


def is_due(spec, last_run, now):
    """True when a recurring schedule should fire. All times are server-local.

    Supported specs: "every 30m|6h|2d", "daily at 06:30",
    "weekly on mon at 09:00". Raises ValueError on anything else.
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
        if meta is None or not meta.get("schedule"):
            log.warning("Recurring template %s has no 'schedule' key; skipping", template.name)
            continue
        last_run = None
        if meta.get("last_run"):
            try:
                last_run = datetime.strptime(meta["last_run"], LAST_RUN_FMT)
            except ValueError:
                log.warning("Recurring template %s has malformed last_run %r; treating as never run",
                            template.name, meta["last_run"])
        try:
            due = is_due(meta["schedule"], last_run, now)
        except ValueError as exc:
            log.error("Recurring template %s: %s", template.name, exc)
            continue
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
    """Where a dependency currently lives: done, failed, waiting, or missing.

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
    if present(PENDING) or present(ACTIVE) or present(RECURRING):
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
    send_telegram(f"❌ Task not run: {path.name}\nIts dependency '{failed_dep}' failed permanently.")


def pick_task():
    candidates = sorted(PENDING.glob("*.md"), key=lambda p: (p.stat().st_mtime, p.name))
    waiting = 0
    for src in candidates:
        meta, _ = parse_task(src)
        blocked = None
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
            if state == "missing":
                log.warning(
                    "Task %s is waiting on dependency %r, which is not in any queue "
                    "directory — typo, or queue it later", src.name, dep,
                )
            else:
                log.info("Task %s is waiting on dependency %r", src.name, dep)
            blocked = "waiting"
            break
        if blocked is None:
            dest = ACTIVE / src.name
            shutil.move(str(src), str(dest))
            return dest
        if blocked == "waiting":
            waiting += 1
    if waiting:
        log.info("%d task(s) waiting on dependencies; nothing runnable this cycle", waiting)
    return None


def handle_failure(path, meta, body, attempts, max_attempts, feedback):
    body = body.rstrip("\n") + f"\n\n## Attempt {attempts} Feedback ({now_iso()})\n\n{feedback.strip()}\n"
    write_task(path, meta, body)
    if attempts >= max_attempts:
        shutil.move(str(path), str(FAILED / path.name))
        log.error("Task %s FAILED permanently after %d/%d attempts", path.name, attempts, max_attempts)
        send_telegram(
            f"❌ Task failed: {path.name}\nExhausted {max_attempts} attempts. Last feedback:\n{feedback}"
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


def process_task(path):
    name = path.name
    meta, body = parse_task(path)

    problems = validate_task(meta, body)
    if problems:
        detail = "\n".join(f"- {p}" for p in problems)
        log.error("Task %s is invalid:\n%s", name, detail)
        body = body.rstrip("\n") + f"\n\n## Invalid Task ({now_iso()})\n\n{detail}\n"
        write_task(path, meta or {}, body)
        shutil.move(str(path), str(FAILED / name))
        send_telegram(f"❌ Task rejected as invalid: {name}\n{detail}")
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

    attempts = int(meta.get("attempts") or 0) + 1
    max_attempts = int(meta["max_attempts"])
    meta["attempts"] = str(attempts)
    write_task(path, meta, body)

    model = meta["model"] if attempts == 1 else (meta.get("escalation_model") or meta["model"])
    tools = meta.get("allowed_tools") or cfg("DEFAULT_ALLOWED_TOOLS", "Read,Glob,Grep,Edit,Write")
    timeout_min = float(meta.get("timeout_minutes") or cfg("DEFAULT_TIMEOUT_MINUTES", "30"))
    cwd = meta.get("cwd") or cfg("DEFAULT_CWD") or str(BASE / "workspace")
    Path(cwd).mkdir(parents=True, exist_ok=True)
    transcript = LOGS / f"{path.stem}.attempt-{attempts}.log"

    log.info(
        "Task %s: attempt %d/%d, model=%s, tools=[%s], timeout=%.0f min, cwd=%s",
        name, attempts, max_attempts, model, tools, timeout_min, cwd,
    )

    worker_cmd = [
        claude_bin, "-p",
        "--model", model,
        "--allowedTools", tools,
        "--output-format", "json",
    ]
    mcp = mcp_config_path(meta)
    if mcp is not None:
        worker_cmd += ["--mcp-config", str(mcp)]
        log.info("Task %s: MCP servers from %s", name, mcp)
    try:
        worker_out = run_claude(
            worker_cmd, WORKER_PREAMBLE + "\n\n" + body, timeout_min * 60, cwd, transcript, "worker"
        )
    except RateLimited:
        meta["attempts"] = str(attempts - 1)  # rate limit doesn't consume an attempt
        write_task(path, meta, body)
        shutil.move(str(path), str(PENDING / name))
        log.warning("Task %s: rate limit persisted through backoff; requeued without consuming an attempt", name)
        return
    except AuthError as exc:
        meta["attempts"] = str(attempts - 1)  # config problem, not a task failure
        write_task(path, meta, body)
        shutil.move(str(path), str(PENDING / name))
        log.error(
            "Task %s: Claude CLI is not authenticated (%s). Run 'claude' once "
            "interactively (or 'claude setup-token') as the cron user. "
            "Task requeued without consuming an attempt.", name, exc,
        )
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

    review_cmd = [
        claude_bin, "-p",
        "--model", meta["review_model"],
        "--allowedTools", "Read,Glob,Grep",
        "--output-format", "json",
    ]
    review_prompt = REVIEW_TEMPLATE.format(criteria=extract_criteria(body), output=tail(worker_out, 20000))
    review_timeout = float(cfg("REVIEW_TIMEOUT_MINUTES", "10")) * 60
    try:
        review_out = run_claude(review_cmd, review_prompt, review_timeout, cwd, transcript, "review")
    except (RateLimited, AuthError, CallTimeout, ClaudeError) as exc:
        review_out = (
            f"VERDICT: FAIL\nReview could not be completed ({type(exc).__name__}). "
            f"Worker output is preserved in logs/{transcript.name}."
        )

    verdict, feedback = parse_verdict(review_out)
    if verdict == "PASS":
        body = body.rstrip("\n") + f"\n\n## Result (attempt {attempts}, {now_iso()})\n\n{worker_out.strip()}\n"
        write_task(path, meta, body)
        shutil.move(str(path), str(DONE / name))
        log.info("Task %s PASSED review on attempt %d/%d; moved to done/", name, attempts, max_attempts)
        send_telegram(f"✅ Task done: {name} (attempt {attempts}/{max_attempts})\n\n{worker_out.strip()}")
    else:
        handle_failure(path, meta, body, attempts, max_attempts, feedback)


# ---------------------------------------------------------------- main


def main():
    for directory in (PENDING, ACTIVE, DONE, FAILED, RECURRING, LOGS):
        directory.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOGS / "dispatcher.log"), logging.StreamHandler(sys.stdout)],
    )

    lock = open(LOCKFILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Another dispatcher run is active; exiting")
        return 0
    lock.write(str(os.getpid()))
    lock.flush()

    recover_stale()
    spawn_recurring()
    task = pick_task()
    if task is None:
        log.info("Queue empty; nothing to do")
        return 0
    process_task(task)
    return 0


if __name__ == "__main__":
    sys.exit(main())
