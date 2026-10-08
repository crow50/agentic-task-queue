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
    /new               start a fresh coordinator conversation
    /status            queue counts straight from the tasks/ directories
    /cancel <task-id>  archive a pending task (or recurring template) to
                       tasks/cancelled/
    /retry <task-id>   requeue a failed/cancelled task with attempts reset

Replies are rendered with Telegram HTML formatting (markdown converted via
dispatcher.md_to_telegram_html, plain-text fallback on rejection). Reacting
👍 to a coordinator message approves what it proposed, 👎 rejects it — the
bridge forwards the reaction to the coordinator as a "[Reaction]" turn.

Only messages from TELEGRAM_CHAT_ID *and* sent by TELEGRAM_USER_ID are
answered (in a private chat the user id defaults to the chat id); everything
else is logged and ignored — the bot is publicly addressable, this is the
boundary, and a group chat alone must not let other members in.

The bridge shares the dispatcher's state files: while the queue is paused for
a usage limit (state/paused_until) or the login is expired (state/auth_failed)
it says so instead of calling claude, keeps the message in coordinator/state.json
("held"), and answers it once the queue is back.
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
from dispatcher import (
    TELEGRAM_MAX,
    cfg,
    extract_result,
    md_to_telegram_html,
    resolve_claude_bin,
)

BASE = dispatcher.BASE
COORD_DIR = BASE / "coordinator"
STATE_FILE = COORD_DIR / "state.json"
LOCKFILE = BASE / "coordinator.lock"

log = logging.getLogger("coordinator")

RUNNING = True


class CoordinatorBlocked(Exception):
    """The coordinator can't run: a usage limit or an expired login. The shared state files say which."""


# ---------------------------------------------------------------- who may talk to the bot


def allowed_user_id():
    """The one Telegram user id allowed to command the bot, or None if it can't be told safely.

    TELEGRAM_USER_ID wins. Without it, a private chat's id *is* its user's id,
    so TELEGRAM_CHAT_ID serves; a group chat id (negative) does not identify
    anyone, so nobody is allowed until TELEGRAM_USER_ID is set.
    """
    configured = str(cfg("TELEGRAM_USER_ID")).strip()
    if configured:
        return configured
    chat = str(cfg("TELEGRAM_CHAT_ID")).strip()
    return chat if chat and not chat.startswith("-") else None


def is_authorized(chat_id, sender_id):
    user = allowed_user_id()
    return (
        user is not None
        and str(chat_id) == str(cfg("TELEGRAM_CHAT_ID"))
        and sender_id is not None
        and str(sender_id) == user
    )


# ---------------------------------------------------------------- telegram


def tg_api(method, params=None, timeout=60):
    data = urllib.parse.urlencode(params or {}).encode()
    with urllib.request.urlopen(dispatcher.telegram_url(method), data, timeout=timeout) as resp:
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


def send_replies(texts, state=None):
    """Send texts as formatted Telegram messages, plain-text fallback per chunk.

    When state is given, each sent message is recorded in
    state["sent_messages"] as [message_id, snippet] so a later reaction to it
    can be traced back to what it said (caller persists via save_state).
    """
    chat_id = cfg("TELEGRAM_CHAT_ID")
    for text in texts:
        for chunk in chunk_message(text):
            try:
                sent = api(
                    "sendMessage",
                    {"chat_id": chat_id, "text": md_to_telegram_html(chunk), "parse_mode": "HTML"},
                    timeout=30,
                )
            except Exception as exc:
                log.warning("Formatted send failed (%s); retrying as plain text", exc)
                try:
                    sent = api("sendMessage", {"chat_id": chat_id, "text": chunk}, timeout=30)
                except Exception as exc:
                    log.error("sendMessage failed: %s", exc)
                    continue
            if state is not None and isinstance(sent, dict) and sent.get("message_id"):
                record = state.setdefault("sent_messages", [])
                record.append([sent["message_id"], chunk[:200]])
                del record[:-20]


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
    memory = COORD_DIR / "memory"
    # A leading extra slash makes an absolute-path permission pattern (//root/...).
    return (
        f"Read,Glob,Grep,Write(/{tasks}/**),Edit(/{tasks}/**),"
        f"Write(/{memory}/**),Edit(/{memory}/**),"
        f"Bash(python3 {BASE}/dispatcher.py cancel:*),"
        f"Bash(python3 {BASE}/dispatcher.py retry:*),"
        f"Bash(python3 {BASE}/dispatcher.py check:*)"
    )


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


def coordinator_call(text, session_id):
    """One coordinator call with usage logging, retried with backoff on a transient rate limit.

    Returns (proc, status, message) as classify_call judged it. Retries used
    up, the last result is returned for the caller to treat as a limit.
    """
    model = cfg("COORDINATOR_MODEL", "claude-sonnet-5")

    def attempt():
        proc = run_coordinator(text, session_id)
        out, err = proc.stdout or "", proc.stderr or ""
        status, message = dispatcher.classify_call(proc.returncode, out, err)
        dispatcher.log_usage("coordinator", model, proc.returncode, status, dispatcher.parse_envelope(out))
        if status != "ok":
            dispatcher.log_failed_call("coordinator", proc.returncode, out, err)
        result = (proc, status, message)
        if status == "rate_limited" and dispatcher.parse_reset_time(message) is None:
            raise dispatcher.TransientLimit(message, result)
        return result

    try:
        return dispatcher.rate_limit_retrying("coordinator")(attempt)
    except dispatcher.TransientLimit as exc:
        return exc.payload


def ask_coordinator(text, state):
    """Run the coordinator, handling resume failure and rate limits. Returns the reply.

    Raises CoordinatorBlocked, after writing the shared pause or auth-failed
    file, when a usage limit or an expired login means nothing can run: the
    caller keeps the message instead of losing it.
    """
    session_id = state.get("session_id")
    tried_fresh = False
    while True:
        try:
            proc, status, message = coordinator_call(text, session_id)
        except subprocess.TimeoutExpired:
            log.error("Coordinator call timed out")
            return "⚠️ The coordinator timed out. Try again, or /new for a fresh session."
        except (OSError, RuntimeError) as exc:
            log.error("Coordinator call failed to start: %s", exc)
            return f"⚠️ Coordinator could not run: {exc}"
        out, err = proc.stdout or "", proc.stderr or ""
        if status == "auth":
            dispatcher.mark_auth_failed(message, notify=False)  # the reply to the user is the notice
            raise CoordinatorBlocked("auth")
        if status == "rate_limited":
            dispatcher.pause_queue(dispatcher.usage_limit_until(message), "usage limit", notify=False)
            raise CoordinatorBlocked("paused")
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
        message = extract_result(out).strip() or combined.strip()
        log.error("Coordinator call failed (exit %d): %s", proc.returncode, message[:2000])
        return f"⚠️ Coordinator error: {message[:500]}"


def stop_text(stop):
    """What to tell the user while the queue is stopped. `stop` is dispatcher.current_stop()."""
    if stop and stop[0] == "auth":
        return ("🔑 Claude login expired, so I can't run anything until it is renewed. "
                "I kept your message and will answer it once the queue is back.")
    if stop:
        return (f"⏸ Queue paused until {dispatcher.format_when(stop[1])} ({stop[2]}). "
                "I kept your message and will answer it then.")
    return "⏸ The queue is stopped. I kept your message and will answer it once it is back."


def converse(prompt, state):
    """One coordinator turn: run it and send the reply, or keep the prompt while the queue is stopped.

    A held prompt waits in state["held"] and replay_held() answers it after
    the pause or the expired login clears. Returns True when the turn ran.
    """
    stop = dispatcher.current_stop()
    if stop is None:
        try:
            reply = ask_coordinator(prompt, state)
        except CoordinatorBlocked:
            stop = dispatcher.current_stop()
        else:
            log.info("Replying: %s", reply[:300])
            send_replies([reply], state)
            return True
    state.setdefault("held", []).append({"text": prompt})
    log.info("Queue stopped (%s); holding the message until it is back", stop[0] if stop else "unknown")
    send_replies([stop_text(stop)], state)
    return False


def replay_held(state):
    """Answer held messages, oldest first, once nothing stops the queue.

    Stops at the first one that is blocked again; it stays at the head of the line.
    """
    held = state.get("held") or []
    while held and dispatcher.current_stop() is None:
        text = held[0]["text"]
        try:
            reply = ask_coordinator(text, state)
        except CoordinatorBlocked:
            return
        held.pop(0)
        snippet = " ".join(text.split())[:60]
        log.info("Replaying held message: %s", snippet)
        send_replies([f"↩️ Re: “{snippet}”\n\n{reply}"], state)
        save_state(state)


# ---------------------------------------------------------------- handlers


def queue_status():
    lines = ["Queue status"]
    for label, directory in (
        ("pending", dispatcher.PENDING),
        ("active", dispatcher.ACTIVE),
        ("done", dispatcher.DONE),
        ("failed", dispatcher.FAILED),
        ("recurring", dispatcher.RECURRING),
        ("cancelled", dispatcher.CANCELLED),
    ):
        names = sorted(p.stem for p in directory.glob("*.md"))
        line = f"{label}: {len(names)}"
        if names and label in ("pending", "active", "failed", "recurring"):
            line += " — " + ", ".join(names)
        lines.append(line)
    return "\n".join(lines)


REACTION_MEANINGS = {
    "👍": "Treat this as approval of what that message proposed: go ahead.",
    "👎": ("Treat this as a rejection of what that message proposed: do not "
           "proceed, and ask what should change if it is unclear."),
}


def handle_reaction(reaction, state):
    """Turn a 👍/👎 on a bot message into an approval/rejection coordinator turn."""
    chat_id = str((reaction.get("chat") or {}).get("id", ""))
    if not is_authorized(chat_id, (reaction.get("user") or {}).get("id")):  # anonymous reactions have no user
        log.warning("Ignoring reaction from unauthorized chat %s or user", chat_id or "(unknown)")
        return
    emojis = [r.get("emoji") for r in reaction.get("new_reaction") or [] if r.get("type") == "emoji"]
    emoji = next((e for e in emojis if e in REACTION_MEANINGS), None)
    if emoji is None:
        return  # reaction removed, or an emoji we give no meaning to
    snippet = next(
        (s for mid, s in reversed(state.get("sent_messages", []))
         if mid == reaction.get("message_id")),
        None,
    )
    target = f'your message: "{snippet}"' if snippet else "one of your recent messages"
    prompt = f"[Reaction] The human reacted {emoji} to {target}. {REACTION_MEANINGS[emoji]}"
    log.info("Reaction %s on message %s", emoji, reaction.get("message_id"))
    if dispatcher.current_stop() is None:
        try:
            api("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
        except Exception:
            pass  # cosmetic only
    converse(prompt, state)


def handle_update(update, state):
    if update.get("message_reaction"):
        handle_reaction(update["message_reaction"], state)
        return
    msg = update.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if not is_authorized(chat_id, (msg.get("from") or {}).get("id")):
        if msg:
            log.warning("Ignoring message from unauthorized chat %s or sender", chat_id or "(unknown)")
        return
    text = (msg.get("text") or "").strip()
    if not text:
        send_replies(["I can only read text messages."], state)
        return
    log.info("Received: %s", text[:300])
    if text == "/new":
        state["session_id"] = None
        send_replies(["Starting a fresh conversation."], state)
        return
    if text == "/status":
        send_replies([queue_status()], state)
        return
    if text == "/cancel" or text.startswith("/cancel "):
        target = text[len("/cancel"):].strip()
        if not target:
            pending = sorted(p.stem for p in dispatcher.PENDING.glob("*.md"))
            send_replies([
                "Usage: /cancel <task-id>\nPending: "
                + (", ".join(pending) if pending else "(none)")
            ], state)
            return
        ok, result = dispatcher.cancel_task(target)
        send_replies([("🗑 " if ok else "⚠️ ") + result], state)
        return
    if text == "/retry" or text.startswith("/retry "):
        target = text[len("/retry"):].strip()
        if not target:
            failed = sorted(p.stem for p in dispatcher.FAILED.glob("*.md"))
            cancelled = sorted(p.stem for p in dispatcher.CANCELLED.glob("*.md"))
            send_replies([
                "Usage: /retry <task-id>"
                + "\nFailed: " + (", ".join(failed) if failed else "(none)")
                + "\nCancelled: " + (", ".join(cancelled) if cancelled else "(none)")
            ], state)
            return
        ok, result = dispatcher.retry_task(target)
        send_replies([("🔁 " if ok else "⚠️ ") + result], state)
        return
    # 👀 on the message plus a typing indicator while the coordinator runs.
    if dispatcher.current_stop() is None:
        for method, params in (
            ("setMessageReaction", {
                "chat_id": chat_id, "message_id": msg.get("message_id"),
                "reaction": json.dumps([{"type": "emoji", "emoji": "👀"}]),
            }),
            ("sendChatAction", {"chat_id": chat_id, "action": "typing"}),
        ):
            try:
                api(method, params, timeout=10)
            except Exception:
                pass  # cosmetic only
    converse(text, state)


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
    if allowed_user_id() is None:
        log.error(
            "TELEGRAM_CHAT_ID %s is a group chat, which does not say who is talking. Set "
            "TELEGRAM_USER_ID to the one Telegram user id allowed to command the bot.",
            cfg("TELEGRAM_CHAT_ID"),
        )
        return 1

    lock = open(LOCKFILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("Another coordinator bridge is already running; exiting")
        return 1

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:  # register the / command menu; cosmetic, never fatal
        api("setMyCommands", {"commands": json.dumps([
            {"command": "status", "description": "Queue counts and task names"},
            {"command": "cancel", "description": "Archive a pending task: /cancel <task-id>"},
            {"command": "retry", "description": "Requeue a failed/cancelled task: /retry <task-id>"},
            {"command": "new", "description": "Start a fresh coordinator conversation"},
        ])}, timeout=30)
    except Exception as exc:
        log.warning("setMyCommands failed: %s", exc)

    state = load_state()
    log.info("Coordinator bridge started (chat %s)", cfg("TELEGRAM_CHAT_ID"))
    backoff = 5
    while RUNNING:
        replay_held(state)  # messages kept while the queue was paused or the login expired
        try:
            updates = api(
                "getUpdates",
                {
                    "timeout": 50,
                    "offset": state.get("offset", 0) + 1,
                    "allowed_updates": json.dumps(["message", "message_reaction"]),
                },
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
