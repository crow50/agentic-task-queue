#!/usr/bin/env python3
"""Telegram coordinator bridge for the Claude task queue.

Long-polls the same Telegram bot the dispatcher uses for notifications and
turns the chat into a conversation with a "coordinator" Claude agent — a
headless `claude -p` run in coordinator/ (so its CLAUDE.md role loads) that
plans work and writes task files into tasks/pending/, but never implements
anything itself. Conversation continuity comes from --resume with the
session id persisted in coordinator/state.json.

Run as a daemon (see coordinator/claude-coordinator.service), not from cron:
    python3 coordinator_bot.py

Built-in commands (answered instantly, no API cost):
    /new     start a fresh coordinator conversation
    /status  queue counts straight from the tasks/ directories

Only messages from TELEGRAM_CHAT_ID are answered; everything else is
logged and ignored — the bot is publicly addressable, this is the boundary.
"""

import fcntl
import json
import logging
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import dispatcher
from dispatcher import RATE_LIMIT_RE, TELEGRAM_MAX, cfg, extract_result, resolve_claude_bin

BASE = dispatcher.BASE
COORD_DIR = BASE / "coordinator"
STATE_FILE = COORD_DIR / "state.json"
LOCKFILE = BASE / "coordinator.lock"

log = logging.getLogger("coordinator")

RUNNING = True


# ---------------------------------------------------------------- telegram


def tg_api(method, params=None, timeout=60):
    token = cfg("TELEGRAM_BOT_TOKEN")
    data = urllib.parse.urlencode(params or {}).encode()
    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/{method}", data, timeout=timeout
    ) as resp:
        payload = json.load(resp)
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API {method} returned {payload}")
    return payload["result"]


api = tg_api  # module-level indirection so tests can substitute a fake


def chunk_message(text, limit=TELEGRAM_MAX):
    """Split a long reply into <=limit chunks, preferring line boundaries."""
    chunks = []
    text = text.strip()
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip("\n"))
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def send_replies(texts):
    chat_id = cfg("TELEGRAM_CHAT_ID")
    for text in texts:
        for chunk in chunk_message(text):
            try:
                api("sendMessage", {"chat_id": chat_id, "text": chunk}, timeout=30)
            except Exception as exc:
                log.error("sendMessage failed: %s", exc)


# ---------------------------------------------------------------- state


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"offset": 0, "session_id": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


# ---------------------------------------------------------------- coordinator


def default_allowed_tools():
    tasks = BASE / "tasks"
    # A leading extra slash makes an absolute-path permission pattern (//root/...).
    return f"Read,Glob,Grep,Write(/{tasks}/**),Edit(/{tasks}/**)"


def extract_session_id(stdout):
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            return data.get("session_id")
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def run_coordinator(prompt, session_id):
    claude_bin = resolve_claude_bin()
    if claude_bin is None:
        raise RuntimeError("no claude binary found — set CLAUDE_BIN in .env")
    cmd = [
        claude_bin, "-p",
        "--model", cfg("COORDINATOR_MODEL", "claude-sonnet-5"),
        "--allowedTools", cfg("COORDINATOR_ALLOWED_TOOLS") or default_allowed_tools(),
        "--output-format", "json",
    ]
    if session_id:
        cmd += ["--resume", session_id]
    return subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        cwd=str(COORD_DIR),
        timeout=float(cfg("COORDINATOR_TIMEOUT_MINUTES", "10")) * 60,
    )


def ask_coordinator(text, state):
    """Run the coordinator, handling resume failure and rate limits. Returns reply."""
    max_retries = int(cfg("MAX_RATE_LIMIT_RETRIES", "5"))
    base_delay = float(cfg("RATE_LIMIT_BASE_DELAY", "30"))
    session_id = state.get("session_id")
    tried_fresh = False
    retry = 0
    while True:
        try:
            proc = run_coordinator(text, session_id)
        except subprocess.TimeoutExpired:
            log.error("Coordinator call timed out")
            return "⚠️ The coordinator timed out. Try again, or /new for a fresh session."
        except (OSError, RuntimeError) as exc:
            log.error("Coordinator call failed to start: %s", exc)
            return f"⚠️ Coordinator could not run: {exc}"
        out, err = proc.stdout or "", proc.stderr or ""
        if proc.returncode == 0:
            sid = extract_session_id(out)
            if sid:
                state["session_id"] = sid
            return extract_result(out).strip() or "(the coordinator sent an empty reply)"
        combined = f"{out}\n{err}"
        if session_id and not tried_fresh and re.search(r"session|conversation", combined, re.I):
            log.warning("Resuming session %s failed; starting fresh", session_id)
            session_id = None
            state["session_id"] = None
            tried_fresh = True
            continue
        if RATE_LIMIT_RE.search(combined) and retry < max_retries:
            delay = min(base_delay * 2**retry, 600)
            retry += 1
            log.warning("Coordinator rate limited; backing off %.0fs (retry %d/%d)", delay, retry, max_retries)
            time.sleep(delay)
            continue
        message = extract_result(out).strip() or combined.strip()
        log.error("Coordinator call failed (exit %d): %s", proc.returncode, message[:2000])
        return f"⚠️ Coordinator error: {message[:500]}"


# ---------------------------------------------------------------- handlers


def queue_status():
    lines = ["Queue status"]
    for label, directory in (
        ("pending", dispatcher.PENDING),
        ("active", dispatcher.ACTIVE),
        ("done", dispatcher.DONE),
        ("failed", dispatcher.FAILED),
        ("recurring", dispatcher.RECURRING),
    ):
        names = sorted(p.stem for p in directory.glob("*.md"))
        line = f"{label}: {len(names)}"
        if names and label in ("pending", "active", "failed", "recurring"):
            line += " — " + ", ".join(names)
        lines.append(line)
    return "\n".join(lines)


def handle_update(update, state):
    msg = update.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if chat_id != str(cfg("TELEGRAM_CHAT_ID")):
        if msg:
            log.warning("Ignoring message from unauthorized chat %s", chat_id or "(unknown)")
        return
    text = (msg.get("text") or "").strip()
    if not text:
        send_replies(["I can only read text messages."])
        return
    log.info("Received: %s", text[:300])
    if text == "/new":
        state["session_id"] = None
        send_replies(["Starting a fresh conversation."])
        return
    if text == "/status":
        send_replies([queue_status()])
        return
    try:
        api("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
    except Exception:
        pass  # cosmetic only
    reply = ask_coordinator(text, state)
    log.info("Replying: %s", reply[:300])
    send_replies([reply])


# ---------------------------------------------------------------- main loop


def _stop(signum, frame):
    global RUNNING
    RUNNING = False
    log.info("Received signal %d; shutting down after current poll", signum)


def main():
    COORD_DIR.mkdir(parents=True, exist_ok=True)
    dispatcher.LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(dispatcher.LOGS / "coordinator.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    if not cfg("TELEGRAM_BOT_TOKEN") or not cfg("TELEGRAM_CHAT_ID"):
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        return 1

    lock = open(LOCKFILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("Another coordinator bridge is already running; exiting")
        return 1

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    state = load_state()
    log.info("Coordinator bridge started (chat %s)", cfg("TELEGRAM_CHAT_ID"))
    backoff = 5
    while RUNNING:
        try:
            updates = api(
                "getUpdates",
                {"timeout": 50, "offset": state.get("offset", 0) + 1},
                timeout=70,
            )
            backoff = 5
        except Exception as exc:
            if RUNNING:
                log.warning("getUpdates failed: %s — retrying in %ds", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
            continue
        for update in updates:
            state["offset"] = max(state.get("offset", 0), update.get("update_id", 0))
            save_state(state)  # persist offset before handling: a crash must not replay
            handle_update(update, state)
            save_state(state)
    log.info("Coordinator bridge stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
