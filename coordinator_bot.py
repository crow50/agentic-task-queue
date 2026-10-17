#!/usr/bin/env python3
"""Telegram coordinator bridge for the Claude task queue.

Long-polls the same Telegram bot the dispatcher uses for notifications and
turns the chat into a conversation with a "coordinator" Claude agent — a
headless `claude -p` run in coordinator/ (so its CLAUDE.md role loads) that
plans work and writes task files into tasks/pending/, but never implements
anything itself. Conversation continuity comes from --resume with the
session id persisted in coordinator/state.json.

Run as a daemon (see coordinator/claude-coordinator.service.template), not from cron:
    python3 coordinator_bot.py

Built-in commands (answered instantly, no API cost):
    /new               start a fresh coordinator conversation
    /menu              buttons: status, usage, pause/resume, learning review, skills, naming
    /status [task-id]  queue counts, or one task's state, tier, attempts and outcome (no model)
    /usage             today's and this week's spend from logs/usage.jsonl (no model)
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
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import dispatcher
from taskq_lib import buttons, memory, naming
from dispatcher import (
    TELEGRAM_MAX,
    cfg,
    extract_result,
    md_to_telegram_html,
    resolve_claude_bin,
)

BASE = dispatcher.BASE
dispatcher.ACTION_SOURCE = "telegram"  # cancels, retries and approvals made here are logged as coming from the chat
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


def send_replies(texts, state=None):
    """Send texts as formatted Telegram messages, plain-text fallback per message.

    A text that does not fit one message goes out as a preview plus reply.md, never as a run of fragments.
    When state is given, each sent message is recorded in state["sent_messages"] as [message_id, snippet]
    so a later reaction to it can be traced back to what it said (caller persists via save_state).
    """
    chat_id = cfg("TELEGRAM_CHAT_ID")

    def send_one(chunk):
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
                return
        if state is not None and isinstance(sent, dict) and sent.get("message_id"):
            record = state.setdefault("sent_messages", [])
            record.append([sent["message_id"], chunk[:200]])
            del record[:-20]

    for text in texts:
        text = dispatcher.redact_secrets(text)
        if dispatcher.fits_in_message(text, limit=TELEGRAM_MAX):
            send_one(text)
            continue
        name = "reply.md"
        send_one(dispatcher.fit_preview("", text, f"\n\n[Preview - the full reply is attached as {name}]"))
        if not dispatcher.post_document(name, text.encode()):
            send_one(f"⚠️ Couldn't attach the full reply ({name}): Telegram refused the upload.\nnot delivered: {name}")


# ---------------------------------------------------------------- state


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"offset": 0, "session_id": None}


def save_state(state):
    dispatcher.write_atomic(STATE_FILE, json.dumps(state))


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


def run_coordinator(prompt, session_id, model=None):
    claude_bin = resolve_claude_bin()
    if claude_bin is None:
        raise RuntimeError("no claude binary found — set CLAUDE_BIN in .env")
    cmd = [
        claude_bin, "-p",
        "--model", model or cfg("COORDINATOR_MODEL", "claude-sonnet-5"),
        "--allowedTools", cfg("COORDINATOR_ALLOWED_TOOLS") or default_allowed_tools(),
        # Claude only touches files under its working directory (coordinator/) and these folders: without them
        # a write to tasks/pending/ is refused as outside the sandbox, whatever --allowedTools says.
        "--add-dir", str(BASE / "tasks"), "--add-dir", str(BASE / "logs"),
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
        env=dispatcher.claude_env(),
        timeout=float(cfg("COORDINATOR_TIMEOUT_MINUTES", "10")) * 60,
    )


def coordinator_call(text, session_id):
    """One coordinator call with usage logging, retried with backoff on a transient rate limit.

    Returns (proc, status, message, record) as classify_call judged it, record being the usage line logged for
    the call. Retries used up, the last result is returned for the caller to treat as a limit.
    """
    def attempt(model):
        proc = run_coordinator(text, session_id, model)
        out, err = proc.stdout or "", proc.stderr or ""
        status, message = dispatcher.classify_call(proc.returncode, out, err)
        record = dispatcher.log_usage("coordinator", model, proc.returncode, status, dispatcher.parse_envelope(out),
                                      resumed_from=session_id)
        if status != "ok":
            dispatcher.log_failed_call("coordinator", proc.returncode, out, err)
        result = (proc, status, message, record)
        if status == "rate_limited" and dispatcher.parse_reset_time(message) is None:
            raise dispatcher.TransientLimit(message, result)
        return result

    wanted = cfg("COORDINATOR_MODEL", "claude-sonnet-5")
    for _ in range(4):  # a model the account cannot use hands over to the next one in models.json
        model = dispatcher.resolve_model(wanted, "coordinator")
        if model is None:
            raise RuntimeError("no usable model for the coordinator in models.json")
        try:
            result = dispatcher.rate_limit_retrying("coordinator")(attempt, model)
        except dispatcher.TransientLimit as exc:
            return exc.payload
        if result[1] != "model":
            return result
        dispatcher.model_gone(model, "coordinator")
    return result


ROTATE_PROMPT = (
    "[Session rotation] This conversation is nearly out of room, so a fresh session starts with the next message. "
    "Rewrite memory/session.md (keep it under 60 lines) with what the fresh session must not forget: decisions made, "
    "tasks you queued or are waiting on, anything the human asked for that is not finished, and how they said they "
    "want things done. Durable preferences also belong in memory/MEMORY.md as usual. Reply with only the word: saved"
)
MEMORY_NOTES = "session.md"  # the rolling notes a rotation writes; CLAUDE.md imports it


def cap_memory():
    """Hold coordinator/memory/ to COORDINATOR_MEMORY_MAX_KB (0 = no limit) by dropping the oldest lines of the
    rolling notes. The curated files are never touched. Returns False when they alone are over the limit."""
    limit = int(dispatcher.number(cfg("COORDINATOR_MEMORY_MAX_KB")) * 1024)
    folder = COORD_DIR / "memory"
    if limit <= 0 or not folder.is_dir():
        return True
    notes = folder / MEMORY_NOTES
    others = sum(p.stat().st_size for p in folder.iterdir() if p.is_file() and p != notes)
    if notes.is_file():
        text = notes.read_text()
        trimmed = memory.trim_to_fit(text, limit - others)
        if trimmed != text:
            dispatcher.write_atomic(notes, trimmed)
            log.info("Trimmed %s to fit COORDINATOR_MEMORY_MAX_KB=%s", MEMORY_NOTES, cfg("COORDINATOR_MEMORY_MAX_KB"))
    return others <= limit


def rotation_due(state):
    line = dispatcher.number(cfg("CONTEXT_ROTATE_PCT"))
    pct = state.get("context_pct")
    return bool(line > 0 and state.get("session_id") and pct is not None and pct >= line)


def rotate_session(state):
    """Save the session's key facts, then forget the session. Returns the one line to tell the human, or None if
    the saving call failed (the session is kept, and nothing is retried until the next call reads its context again).

    Raises CoordinatorBlocked when a usage limit or an expired login stopped the saving call."""
    pct = state.get("context_pct")
    state["context_pct"] = None  # whatever happens, this crossing is dealt with once
    try:
        proc, status, message, _ = coordinator_call(ROTATE_PROMPT, state["session_id"])
    except (subprocess.TimeoutExpired, OSError, RuntimeError) as exc:
        log.warning("Session rotation: the saving call failed (%s); keeping the session", type(exc).__name__)
        return None
    if status == "auth":
        dispatcher.mark_auth_failed(message, notify=False)
        raise CoordinatorBlocked("auth")
    if status == "rate_limited":
        dispatcher.pause_queue(dispatcher.usage_limit_until(message), "usage limit", notify=False)
        raise CoordinatorBlocked("paused")
    if proc.returncode != 0:
        log.warning("Session rotation: the saving call exited %d; keeping the session", proc.returncode)
        return None
    state["session_id"] = None
    fits = cap_memory()
    log.info("Rotated the coordinator session (context was %.0f%%)", pct)
    return (f"Started a fresh session (context was {pct:.0f}%)."
            + ("" if fits else " The memory folder is over its limit; trim coordinator/memory/."))


def ask_coordinator(text, state):
    """Run the coordinator, handling resume failure, rate limits and session rotation. Returns the reply.

    Raises CoordinatorBlocked, after writing the shared pause or auth-failed
    file, when a usage limit or an expired login means nothing can run: the
    caller keeps the message instead of losing it.
    """
    note = rotate_session(state) if rotation_due(state) else None
    reply = ask_in_session(text, state)
    return f"{note}\n\n{reply}" if note else reply


def ask_in_session(text, state):
    session_id = state.get("session_id")
    tried_fresh = False
    while True:
        try:
            proc, status, message, record = coordinator_call(text, session_id)
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
            state["context_pct"] = record.get("context_pct")
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


def announce_waiting():
    """Tell the human about tasks waiting for approval (the dispatcher does the same on its runs)."""
    try:
        dispatcher.announce_proposals()
    except Exception as exc:  # noqa: BLE001 - announcing must never stop the bridge
        log.warning("Could not announce tasks waiting for approval: %s", exc)


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
        ("proposed", dispatcher.PROPOSED),
        ("active", dispatcher.ACTIVE),
        ("done", dispatcher.DONE),
        ("failed", dispatcher.FAILED),
        ("recurring", dispatcher.RECURRING),
        ("cancelled", dispatcher.CANCELLED),
    ):
        names = sorted(p.stem for p in directory.glob("*.md"))
        line = f"{label}: {len(names)}"
        if names and label in ("pending", "proposed", "active", "failed", "recurring"):
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
    if (verdict := dispatcher.apply_reaction(reaction.get("message_id"), emoji)) is not None:
        send_replies([verdict], state)  # an approval announcement: handled in code, no coordinator turn
        return
    if emoji == "👎" and (stem := dispatcher.outcome_for_message(reaction.get("message_id"))):
        state["awaiting_followup"] = {"task": stem, "at": dispatcher.now_iso()}  # the next message says what was wrong
        send_replies([f"What was wrong with {stem}? Reply with what to change, or send /skip."], state)
        return
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


# ---------------------------------------------------------------- the menu and its buttons
#
# A tap arrives as a callback_query. It passes the same sender check as a message, is always answered (so the button
# stops spinning), and runs code that reads or writes the queue's files: no tap calls a model, except the naming
# exchange, which is a conversation by design. callback_data is `act:<action>[:<arg>]`, at most 64 bytes.


class Reply(NamedTuple):
    text: str
    rows: list = None  # inline buttons: rows of (label, action, arg)
    new: bool = False  # send as a new message instead of editing the one that was tapped


BACK = [("Menu", "menu", "")]
KEEP_MENU_TASKS = 8
NAME_PROMPT = (
    "[Naming] You are about to be given a name. Propose three names for yourself. Reply with exactly three lines, each "
    "`Name | one short reason`, and nothing else. A name is 3 to 12 letters, easy to say aloud, not a common English word. "
    "{avoid}"
)
NAME_SELF_PROMPT = (
    "[Naming] Choose the one name you want, for yourself. Reply with exactly one line, `Name | one short reason`, and "
    "nothing else. A name is 3 to 12 letters, easy to say aloud, not a common English word. {avoid}"
)


def menu_rows():
    paused = dispatcher.read_pause()
    paused = bool(paused and paused.until > datetime.now(timezone.utc))
    return [
        [("Status", "status", ""), ("Usage", "usage", "")],
        [("Resume", "resume", "") if paused else ("Pause", "pause", "")],
        [("Run learning review", "learn", "")],
        [("Create skill from a task", "skillpick", "")],
        [("Name your assistant", "name", "")],
    ]


def menu_reply():
    return Reply(f"{dispatcher.assistant_name()} menu", menu_rows())


def spend_prompt(what, prefix):
    average = dispatcher.average_run_cost(prefix)
    cost = (f"The last {average[1]} run{'s' if average[1] != 1 else ''} averaged ${average[0]:.2f}."
            if average else "There is no earlier run to average yet.")
    return f"{what}? {cost}"


def do_resume():
    pause = dispatcher.read_pause()
    if pause:
        dispatcher.PAUSED_FILE.unlink(missing_ok=True)
    lines = ["Resumed: the queue runs again." if pause else "The queue was not paused."]
    if dispatcher.AUTH_FAILED_FILE.exists():
        if dispatcher.login_works():
            dispatcher.AUTH_FAILED_FILE.unlink(missing_ok=True)
            dispatcher.clear_reported("auth")
            lines.append("The Claude login works again.")
        else:
            lines.append("The Claude login still fails: run `claude` on the queue host and sign in.")
    return Reply("\n".join(lines), [BACK])


def do_retry(stem):
    """Retry a failed or cancelled task, or make a finished-but-unpublished one try publishing at once."""
    path = dispatcher.PENDING / f"{stem}.md"
    if path.is_file():
        meta, body = dispatcher.parse_task(path)
        if meta and meta.get("pending_publish"):
            for key in ("held_until", "publish_blocked", "publish_failures"):
                meta.pop(key, None)
            dispatcher.write_task(path, meta, body)
            return Reply(f"{stem} will try to publish on the next run.")
    ok, message = dispatcher.retry_task(stem)
    return Reply(("🔁 " if ok else "⚠️ ") + message)


def do_pick_skill():
    done = dispatcher.recent_done(KEEP_MENU_TASKS)
    if not done:
        return Reply("No finished tasks yet.", [BACK])
    return Reply("Which finished task should become a skill?",
                 [[(stem if len(stem) <= 40 else stem[:37] + "...", "skill", stem)] for stem in done] + [BACK])


def do_skill_ok(stem):
    if not stem or "/" in stem or not (dispatcher.DONE / f"{stem}.md").is_file():
        return Reply(f"'{stem}' is not a finished task.", [BACK])
    if dispatcher.queued_like("skill-from-task-", f"tasks/done/{stem}.md"):
        return Reply(f"A skill for {stem} is already queued.", [BACK])
    name = f"skill-from-task-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    ok, message = dispatcher.enqueue_example("skill-from-task.md", name, {"__TASK_ID__": stem})
    return Reply((f"Queued a skill for {stem} as {name}. If it generalises, a pull request opens; if not, you hear nothing."
                  if ok else f"Could not queue it: {message}"), [BACK])


def do_learn_ok():
    if existing := dispatcher.queued_like("weekly-improver-"):
        return Reply(f"A learning review is already queued: {existing.stem}.", [BACK])
    name = f"weekly-improver-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    ok, message = dispatcher.enqueue_example("weekly-improver.md", name)
    return Reply((f"Queued {name}. If it finds nothing worth proposing, you hear nothing."
                  if ok else f"Could not queue it: {message}"), [BACK])


# --- naming the assistant


def ask_for_names(avoid, one=False):
    """A fresh, throwaway coordinator session asked for names. Returns (pairs, error text)."""
    note = ("Do not suggest: " + ", ".join(avoid) + ".") if avoid else ""
    prompt = (NAME_SELF_PROMPT if one else NAME_PROMPT).format(avoid=note)
    try:
        proc, status, message, _ = coordinator_call(prompt, None)
    except (subprocess.TimeoutExpired, OSError, RuntimeError) as exc:
        return [], f"I could not ask for names: {type(exc).__name__}."
    if status != "ok":
        return [], "I could not ask for names right now (the queue is paused or the login needs renewing)."
    return naming.parse_proposals(extract_result(proc.stdout or "")), ""


def offer_names(state, more=False):
    avoid = state.get("name_offered", []) if more else []
    pairs, error = ask_for_names(avoid)
    if not pairs:
        return Reply(error or "No usable names came back. Tap again to retry.", [BACK])
    state["name_offered"] = (avoid + [n for n, _ in pairs])[-12:]
    lines = ["Three names for me:"] + [f"{n}. {name}: {reason}" for n, (name, reason) in enumerate(pairs, 1)]
    return Reply("\n".join(lines),
                 [[(name, "name_pick", name) for name, _ in pairs],
                  [("More names", "name_more", ""), ("Choose for me", "name_self", "")]])


def open_name_pr(patch_path):
    """Run the opener for the naming patch. Returns its one line, or why it could not run."""
    env = dispatcher.publish_env()
    env[open_proposal_name_env()] = "1"
    cmd = [sys.executable, str(BASE / "scripts" / "open_proposal_pr.py"), str(patch_path.relative_to(BASE)),
           "--allow", naming.IDENTITY_PATH, "--allow", naming.ENV_EXAMPLE_PATH,
           "--test-cmd", f"{sys.executable} -m unittest discover -s tests -p test_naming.py"]
    if cfg("GH_BIN"):
        cmd += ["--gh", cfg("GH_BIN")]
    try:
        proc = subprocess.run(cmd, cwd=str(BASE), env=env, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not run the opener ({type(exc).__name__})"
    return (proc.stdout.strip().splitlines() or [proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "no output"])[-1]


def open_proposal_name_env():
    return "TASKQ_NAME_PR"


def save_name(name):
    """Record the chosen name: ASSISTANT_NAME in .env, and a pull request for identity.md and the .env.example default."""
    name = naming.clean(name)
    if name is None:
        return Reply("That is not a name I can use.", [BACK])
    try:
        patch = naming.name_patch((COORD_DIR / "identity.md").read_text(), (BASE / ".env.example").read_text(), name)
    except (OSError, ValueError) as exc:
        return Reply(f"I could not record the name: {exc}", [BACK])
    dispatcher.set_env_value("ASSISTANT_NAME", name)
    if not patch:
        return Reply(f"I am already {name} in the repo; .env now says so too.", [BACK])
    target = BASE / "tasks" / "proposals" / "assistant-name.patch"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(patch)
    return Reply(f"I am {name} from now on (set in .env). The repo change: {open_name_pr(target)}", [BACK])


def do_name_self(state):
    pairs, error = ask_for_names(state.get("name_offered", []), one=True)
    if not pairs:
        return Reply(error or "No usable name came back. Tap again to retry.", [BACK])
    return save_name(pairs[0][0])


def do_name_pick(state, name):
    if name not in state.get("name_offered", []):
        return Reply("That name was not one I offered. Tap Name your assistant to start again.", [BACK])
    return save_name(name)


def run_action(action, arg, state):
    """The Reply for a button. Raises KeyError for an action that does not exist."""
    simple = {
        "menu": lambda: menu_reply(),
        "status": lambda: Reply(queue_status(), [BACK]),
        "usage": lambda: Reply(dispatcher.usage_summary(), [BACK]),
        "pause": lambda: pause_from_menu(),
        "resume": lambda: do_resume(),
        "learn": lambda: (Reply(f"A learning review is already queued: {existing.stem}.", [BACK])
                          if (existing := dispatcher.queued_like("weekly-improver-")) else
                          Reply(spend_prompt("Run a learning review", "weekly-improver-"),
                                [[("Confirm", "learn_ok", ""), ("Cancel", "menu", "")]])),
        "learn_ok": lambda: do_learn_ok(),
        "skillpick": lambda: do_pick_skill(),
        "skill": lambda: Reply(spend_prompt(f"Create a skill from {arg}", "skill-from-task-"),
                               [[("Confirm", "skill_ok", arg), ("Cancel", "menu", "")]]),
        "skill_ok": lambda: do_skill_ok(arg),
        "approve": lambda: acted(dispatcher.approve_task(arg), "✅ "),
        "reject": lambda: acted(dispatcher.reject_task(arg), "🗑 "),
        "retry": lambda: do_retry(arg),
        "cancel": lambda: acted(dispatcher.cancel_task(arg), "🗑 "),
        "open": lambda: Reply(dispatcher.task_status(arg)[1], new=True),
        "name": lambda: offer_names(state),
        "name_more": lambda: offer_names(state, more=True),
        "name_self": lambda: do_name_self(state),
        "name_pick": lambda: do_name_pick(state, arg),
    }
    return simple[action]()


def acted(result, icon):
    ok, message = result
    return Reply((icon if ok else "⚠️ ") + message)


def pause_from_menu():
    until = datetime.now(timezone.utc) + timedelta(hours=dispatcher.MENU_PAUSE_HOURS)
    dispatcher.pause_queue(until, "paused from the menu", notify=False)
    return Reply(f"Paused until {dispatcher.format_when(until)}. Nothing new starts; a running task finishes.",
                 [[("Resume", "resume", "")], BACK])


def edit_message(chat_id, message_id, text, rows=None):
    params = {"chat_id": chat_id, "message_id": message_id,
              "text": dispatcher.clip_for_telegram(dispatcher.redact_secrets(text))}
    if rows:
        params["reply_markup"] = dispatcher.keyboard(rows)
    try:
        api("editMessageText", params, timeout=30)
    except Exception as exc:  # an unchanged text is refused with a 400; the tap was still handled
        log.info("editMessageText did not apply (%s)", type(exc).__name__)


def handle_callback(query, state):
    """A button tap. Ignored without a word unless it comes from the one authorised sender in the one chat."""
    message = query.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if not is_authorized(chat_id, (query.get("from") or {}).get("id")):
        log.warning("Ignoring a button tap from unauthorized chat %s or sender", chat_id or "(unknown)")
        return
    answer = {"callback_query_id": query.get("id")}
    decoded = buttons.decode(query.get("data"), dispatcher.callback_table())
    try:
        if decoded is None:
            reply = Reply("I don't know that button any more. Send /menu for a fresh one.", [BACK])
        else:
            action, arg = decoded
            try:
                reply = run_action(action, arg, state)
            except KeyError:
                reply = Reply(f"I don't know the action '{action}'.", [BACK])
    except Exception as exc:  # noqa: BLE001 - a tap must always be answered
        log.exception("Button %s failed", query.get("data"))
        reply = Reply(f"Something went wrong ({type(exc).__name__}). Nothing was changed that I know of.", [BACK])
    try:
        api("answerCallbackQuery", answer, timeout=10)
    except Exception:
        pass  # the spinner stops by itself after a while
    if reply.new:
        send_replies([reply.text], state)
        return
    original = (message.get("text") or "").strip()
    keep_original = decoded is not None and decoded[0] in ("approve", "reject", "retry", "cancel") and original
    edit_message(chat_id, message.get("message_id"), f"{original}\n\n{reply.text}" if keep_original else reply.text, reply.rows)


def send_menu():
    reply = menu_reply()
    try:
        api("sendMessage", {"chat_id": cfg("TELEGRAM_CHAT_ID"), "text": reply.text,
                            "reply_markup": dispatcher.keyboard(reply.rows)}, timeout=30)
    except Exception as exc:
        log.error("Could not send the menu: %s", exc)


FOLLOWUP_WAIT = timedelta(minutes=60)  # how long a 👎 waits for its answer


def continue_from(stem, text):
    """The reply to /continue, to a reply on a result and to the answer to a 👎: queue a follow-up. No model is called."""
    ok, message = dispatcher.create_followup(stem, text, source="user")
    return ("↪️ " if ok else "⚠️ ") + message


def awaited_task(state):
    """The task a 👎 is waiting to hear about, or None when nothing is awaited or it has gone stale."""
    waiting = state.get("awaiting_followup")
    try:
        asked = datetime.strptime(waiting["at"], dispatcher.ISO_FMT).replace(tzinfo=timezone.utc)
        return waiting["task"] if datetime.now(timezone.utc) - asked < FOLLOWUP_WAIT else None
    except (TypeError, KeyError, ValueError):
        return None


def refuse_secret(msg, chat_id, kinds, state):
    """A message that holds a key or token: not logged, not stored, not sent to the model. Delete it and say why, once."""
    log.warning("Dropped message %s: it looked like a %s", msg.get("message_id"), " and a ".join(kinds))
    record = state.setdefault("redacted", [])
    record.append({"message_id": msg.get("message_id"), "kinds": kinds, "at": dispatcher.now_iso()})
    del record[:-20]
    try:
        api("deleteMessage", {"chat_id": chat_id, "message_id": msg.get("message_id")}, timeout=10)
    except Exception:
        pass  # Telegram may refuse (an old message, a group without the right); the reply still tells the user to rotate it
    send_replies([f"That looked like a {kinds[0]}. I did not read or keep it. Put it in `.env` and rotate it, "
                  "since it sat in this chat."], state)


def handle_update(update, state):
    if update.get("message_reaction"):
        handle_reaction(update["message_reaction"], state)
        return
    if update.get("callback_query"):
        handle_callback(update["callback_query"], state)
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
    if kinds := dispatcher.secret_kinds(text):  # before the log line, the model and the state file see it
        refuse_secret(msg, chat_id, kinds, state)
        return
    log.info("Received: %s", text[:300])
    if text == "/new":
        state["session_id"] = None
        state["context_pct"] = None
        send_replies(["Starting a fresh conversation."], state)
        return
    if text == "/menu":
        send_menu()
        return
    if text == "/usage":
        send_replies([dispatcher.usage_summary()], state)
        return
    if text == "/status":
        send_replies([queue_status()], state)
        return
    if text.startswith("/status "):
        send_replies([dispatcher.task_status(text[len("/status"):])[1]], state)
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
    if text == "/tasks":
        send_replies([dispatcher.finished_tasks_text()], state)
        return
    if text == "/continue" or text.startswith("/continue "):
        stem, _, change = text[len("/continue"):].strip().partition(" ")
        send_replies([continue_from(stem, change) if stem else dispatcher.finished_tasks_text()], state)
        return
    if text == "/skip":
        dropped = state.pop("awaiting_followup", None)
        send_replies(["Dropped it." if dropped else "Nothing to skip."], state)
        return
    replied_to = (msg.get("reply_to_message") or {}).get("message_id")
    if not text.startswith("/"):  # a command inside a reply is still a command
        if replied_to and (stem := dispatcher.outcome_for_message(replied_to)):  # a reply to a result: continue that task
            state.pop("awaiting_followup", None)
            send_replies([continue_from(stem, text)], state)
            return
        if stem := awaited_task(state):  # the answer to "what was wrong?"
            state.pop("awaiting_followup", None)
            send_replies([continue_from(stem, text)], state)
            return
    for command, act, icon in (("/approve", dispatcher.approve_task, "✅ "), ("/reject", dispatcher.reject_task, "🗑 ")):
        if text == command or text.startswith(command + " "):
            target = text[len(command):].strip()
            if not target:
                waiting = sorted(p.stem for p in dispatcher.PROPOSED.glob("*.md"))
                send_replies([f"Usage: {command} <task-id>\nWaiting for approval: " + (", ".join(waiting) if waiting else "(none)")], state)
                return
            ok, result = act(target)
            send_replies([(icon if ok else "⚠️ ") + result], state)
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
            {"command": "menu", "description": "Buttons: status, usage, pause, learning review, skills, naming"},
            {"command": "status", "description": "Queue counts, or one task: /status <task-id>"},
            {"command": "usage", "description": "What today and this week cost"},
            {"command": "cancel", "description": "Archive a pending task: /cancel <task-id>"},
            {"command": "retry", "description": "Requeue a failed/cancelled task: /retry <task-id>"},
            {"command": "tasks", "description": "The last finished tasks and what each made"},
            {"command": "continue", "description": "Carry on from a finished task: /continue <task-id> <what to change>"},
            {"command": "approve", "description": "Let a waiting task run: /approve <task-id>"},
            {"command": "reject", "description": "Drop a waiting task: /reject <task-id>"},
            {"command": "new", "description": "Start a fresh coordinator conversation"},
        ])}, timeout=30)
    except Exception as exc:
        log.warning("setMyCommands failed: %s", exc)

    state = load_state()
    log.info("Coordinator bridge started (chat %s)", cfg("TELEGRAM_CHAT_ID"))
    backoff = 5
    while RUNNING:
        replay_held(state)  # messages kept while the queue was paused or the login expired
        announce_waiting()
        try:
            updates = api(
                "getUpdates",
                {
                    "timeout": 50,
                    "offset": state.get("offset", 0) + 1,
                    "allowed_updates": json.dumps(["message", "message_reaction", "callback_query"]),
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
            announce_waiting()  # the coordinator may just have written a task that needs approval
    log.info("Coordinator bridge stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
