#!/usr/bin/env python3
"""Install, update and health-check the Claude task queue (stdlib only, idempotent).

    sudo python3 scripts/install.py            set everything up; a second run changes nothing
    python3 scripts/install.py --check         PASS/FAIL for every moving part (exit 1 on a FAIL)

Install does, each step skipped when already in place:
  - creates the `taskq` user, hands it the checkout and a .venv with the hashed requirements
  - asks for the Telegram bot token (validated with getMe) and learns the chat id from your
    first message to the bot
  - stores a one-year `claude setup-token` login in .env (mode 600) and proves it with one `claude -p`
  - renders coordinator/CLAUDE.md and the systemd unit from their .template files ({{BASE}} etc.)
  - installs the dispatcher and the daily `--check` cron lines for taskq, replacing any old ones
  - smoke-tests one run on an empty queue and sends "setup complete" to Telegram

Everything that touches the machine (users, crontab, systemd) goes through `System`, so the
tests run the whole flow against a fake.
"""

import argparse
import datetime
import getpass
import grp
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import time
import typing
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MIN_PYTHON = (3, 11)
REPO = Path(__file__).resolve().parent.parent
SERVICE = "claude-coordinator"
CRON_BEGIN = "# agentic-task-queue: managed by scripts/install.py (replaced on every install)"
CRON_END = "# end agentic-task-queue"
CLAUDE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"
DEFAULT_API = "https://api.telegram.org"
PROBE_MODEL = "claude-haiku-4-5-20251001"
TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")
TOKEN_WARN_DAYS = 335  # 11 months; setup-token logins last a year
QUEUE_DIRS = ("tasks/pending", "tasks/active", "tasks/done", "tasks/failed",
              "tasks/recurring", "tasks/cancelled", "logs", "state", "workspace")
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


class InstallError(Exception):
    pass


class Result(typing.NamedTuple):
    level: str  # PASS, WARN or FAIL
    name: str
    detail: str
    notify: bool  # worth a Telegram message from the daily --check

    def line(self):
        return f"{self.level:<4}  {self.name}: {self.detail}"


# ---------------------------------------------------------------- the machine


class System:
    """Everything the installer does to the host. Tests substitute a fake."""

    unit_dir = Path("/etc/systemd/system")

    def is_root(self):
        return os.geteuid() == 0

    def must_switch(self, user):
        return self.is_root() and pwd.getpwnam(user).pw_uid != 0

    def user_exists(self, user):
        try:
            pwd.getpwnam(user)
            return True
        except KeyError:
            return False

    def create_user(self, user):
        subprocess.run(["useradd", "--system", "--create-home", "--shell", "/bin/bash", user], check=True)

    def home(self, user):
        return Path(pwd.getpwnam(user).pw_dir)

    def group(self, user):
        return grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name

    def chown(self, path, user, recursive=False):
        if not self.must_switch(user):
            return
        args = ["chown", "-R" if recursive else "-h", f"{user}:{self.group(user)}", str(path)]
        subprocess.run(args, check=True)

    def prepare(self, user, cmd, extra_env=None):
        """(argv, env) to run `cmd` as `user`; the environment carries credentials, never argv."""
        env = dict(os.environ)
        env.update(extra_env or {})
        if self.must_switch(user):
            env.update(HOME=str(self.home(user)), USER=user, LOGNAME=user)
            cmd = ["runuser", "-u", user, "--"] + list(cmd)
        return list(cmd), env

    def run_as(self, user, cmd, input=None, extra_env=None, cwd=None, timeout=120):
        argv, env = self.prepare(user, cmd, extra_env)
        try:
            return subprocess.run(argv, input=input, capture_output=True, text=True,
                                  cwd=cwd, env=env, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(argv, 127, "", f"{type(exc).__name__}: {exc}")

    def popen_as(self, user, cmd, extra_env=None):
        argv, env = self.prepare(user, cmd, extra_env)
        return subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, env=env)

    def crontab_read(self, user):
        cmd = ["crontab", "-l"] if self.is_user(user) else ["crontab", "-u", user, "-l"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.stdout if proc.returncode == 0 else ""  # rc 1 = "no crontab for user"

    def crontab_write(self, user, text):
        cmd = ["crontab", "-"] if self.is_user(user) else ["crontab", "-u", user, "-"]
        subprocess.run(cmd, input=text, text=True, check=True)

    def is_user(self, user):
        return pwd.getpwuid(os.geteuid()).pw_name == user

    def has_systemd(self):
        return Path("/run/systemd/system").is_dir() and shutil.which("systemctl") is not None

    def systemctl(self, *args):
        return subprocess.run(["systemctl", *args], capture_output=True, text=True)

    def timedatectl(self):
        if not shutil.which("timedatectl"):
            return None
        proc = subprocess.run(["timedatectl", "show", "-p", "Timezone", "-p", "NTPSynchronized"],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)


class Ctx:
    def __init__(self, args, system):
        self.args = args
        self.system = system
        self.base = Path(args.base).resolve()
        self.user = args.user
        self.changes = []
        self.api = os.environ.get("TELEGRAM_API_BASE", "")

    def say(self, text):
        if not self.args.quiet:
            print(text)

    def changed(self, what):
        self.changes.append(what)
        self.say(f"  changed: {what}")

    @property
    def env_path(self):
        return self.base / ".env"

    @property
    def venv_python(self):
        return self.base / ".venv" / "bin" / "python3"

    def env(self):
        return parse_env(self.env_path.read_text()) if self.env_path.exists() else {}

    def api_base(self, env=None):
        return (self.api or (env or self.env()).get("TELEGRAM_API_BASE") or DEFAULT_API).rstrip("/")


# ---------------------------------------------------------------- pure helpers


def parse_env(text):
    """KEY=value lines, as dispatcher.load_env reads them."""
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip("\"'")
    return env


def upsert_env(text, updates):
    """Set KEY=value in place (first active line), append missing keys; comments and other keys stay."""
    lines = text.splitlines()
    for key, value in updates.items():
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for index, line in enumerate(lines):
            if pattern.match(line):
                lines[index] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def render(template, values):
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", str(value))
    left = sorted(set(re.findall(r"\{\{\w+\}\}", out)))
    if left:
        raise InstallError(f"template has unfilled placeholders: {', '.join(left)}")
    return out


def extract_token(text):
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    match = TOKEN_RE.search(text)
    return match.group(0) if match else None


def cron_block(base):
    py, root = f"{base}/.venv/bin/python3", str(base)
    return [
        CRON_BEGIN,
        f"*/15 * * * * {py} {root}/dispatcher.py >> {root}/logs/cron.log 2>&1",
        f"17 6 * * * {py} {root}/scripts/install.py --check --quiet --notify >> {root}/logs/check.log 2>&1",
        CRON_END,
    ]


def merge_crontab(existing, base):
    """Drop our block and any stray dispatcher line, then append the current block."""
    kept, inside = [], False
    for line in existing.splitlines():
        if line == CRON_BEGIN:
            inside = True
            continue
        if inside:
            inside = line != CRON_END
            continue
        if f"{base}/dispatcher.py" in line:
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept + cron_block(base)) + "\n"


def deny_rules(base):
    # A leading extra slash makes an absolute-path pattern in Claude Code permission rules.
    paths = [f"/{base}/.env", f"/{base}/state/**", "~/.claude/.credentials.json"]
    return sorted(f"{tool}({path})" for path in paths for tool in ("Read", "Edit"))


def renewal_ics(created):
    due = created + datetime.timedelta(days=TOKEN_WARN_DAYS)
    stamp = created.strftime("%Y%m%d") + "T000000Z"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//agentic-task-queue//install.py//EN",
        "BEGIN:VEVENT", f"UID:taskq-token-{created:%Y%m%d}@{socket.gethostname()}", f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{due:%Y%m%d}",
        "SUMMARY:Renew the Claude task queue login token",
        "DESCRIPTION:Run: sudo python3 scripts/install.py  (it runs claude setup-token when the login fails)",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def write_file(path, text, mode=0o644):
    """Atomically write text; True if the content or mode changed."""
    path = Path(path)
    data = text.encode()
    same = path.exists() and path.read_bytes() == data  # bytes: an .ics uses CRLF
    if same and (path.stat().st_mode & 0o777) == mode:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return not same


# ---------------------------------------------------------------- telegram


def tg(ctx, token, method, params=None, timeout=15):
    """One Bot API call; the result, or InstallError naming the problem but never the token."""
    url = f"{ctx.api_base()}/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            description = json.load(exc).get("description", "")
        except ValueError:
            description = ""
        raise InstallError(f"Telegram {method}: HTTP {exc.code} {description}".strip())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise InstallError(f"Telegram {method}: {type(exc).__name__}")
    if not payload.get("ok"):
        raise InstallError(f"Telegram {method}: {payload.get('description', 'not ok')}")
    return payload["result"]


def ask(ctx, prompt, flag, secret=False):
    if ctx.args.non_interactive:
        raise InstallError(f"{prompt} is needed; pass {flag}")
    return (getpass.getpass if secret else input)(f"{prompt}: ").strip()


def wait_for_message(ctx, token, username):
    ctx.say(f"Send any message to @{username} now (waiting up to {ctx.args.wait}s)...")
    deadline = time.time() + ctx.args.wait
    while time.time() < deadline:
        started = time.time()
        updates = tg(ctx, token, "getUpdates", {"timeout": 20 if not ctx.args.non_interactive else 0},
                     timeout=35)
        messages = [u for u in updates if u.get("message", {}).get("chat")]
        if messages:
            latest = messages[-1]
            tg(ctx, token, "getUpdates", {"offset": latest["update_id"] + 1, "timeout": 0})  # consume it
            return latest["message"]
        if time.time() - started < 1:
            time.sleep(1)
    raise InstallError("no message arrived; send one to the bot and rerun, or pass --chat-id")


# ---------------------------------------------------------------- install steps


def preflight(ctx):
    if sys.version_info < MIN_PYTHON:
        raise InstallError(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required (this is {sys.version.split()[0]})")
    machine = os.uname().machine
    ctx.say(f"Python {sys.version.split()[0]} on {machine}")
    if machine not in ("aarch64", "arm64", "x86_64"):
        ctx.say(f"  warning: Claude Code supports aarch64 and x86_64, not {machine}")
    if not ctx.system.is_root() and not ctx.args.allow_non_root:
        raise InstallError(f"run as root: sudo python3 {sys.argv[0]}")


def step_user(ctx):
    if not ctx.system.user_exists(ctx.user):
        ctx.system.create_user(ctx.user)
        ctx.changed(f"created user {ctx.user}")
    for rel in QUEUE_DIRS:
        (ctx.base / rel).mkdir(parents=True, exist_ok=True)
    ctx.system.chown(ctx.base, ctx.user, recursive=True)
    if ctx.system.run_as(ctx.user, ["test", "-r", str(ctx.base / "dispatcher.py")]).returncode != 0:
        raise InstallError(
            f"{ctx.user} cannot read {ctx.base} (a home directory is private). "
            "Clone the repo somewhere shared, e.g. /opt/agentic-task-queue, and run the installer there."
        )


def find_claude(ctx):
    home = ctx.system.home(ctx.user)
    candidates = [ctx.args.claude_bin, ctx.env().get("CLAUDE_BIN"), str(home / ".local/bin/claude"),
                  "/usr/local/bin/claude", "/usr/bin/claude", shutil.which("claude")]
    for path in candidates:
        if path and ctx.system.run_as(ctx.user, ["test", "-x", path]).returncode == 0:
            proc = ctx.system.run_as(ctx.user, [path, "--version"])
            ctx.say(f"claude: {path} ({(proc.stdout or proc.stderr).strip() or 'version unknown'})")
            return path
    raise InstallError(
        f"no claude binary that {ctx.user} can run. Install it as that user, then rerun:\n"
        f"  sudo -u {ctx.user} -H bash -c '{CLAUDE_INSTALL}'\n"
        "(or pass --claude-bin PATH)"
    )


def step_venv(ctx):
    imports = [str(ctx.venv_python), "-c", "import httpx, tenacity, filelock"]
    if ctx.venv_python.exists() and ctx.system.run_as(ctx.user, imports).returncode == 0:
        return
    steps = [[sys.executable, "-m", "venv", str(ctx.base / ".venv")],
             [str(ctx.venv_python), "-m", "pip", "install", "--quiet", "--require-hashes",
              "-r", str(ctx.base / "requirements.txt")]]
    for cmd in steps:
        proc = ctx.system.run_as(ctx.user, cmd, timeout=600)
        if proc.returncode != 0:
            hint = "  (on Debian/Ubuntu: sudo apt install python3-venv)" if "venv" in " ".join(cmd) else ""
            raise InstallError(f"{' '.join(cmd[:4])} failed: {proc.stderr.strip()[-300:]}{hint}")
    ctx.changed("created .venv with the hashed requirements")


def write_env(ctx, updates):
    """Merge updates into .env (seeded from .env.example), mode 600; True if it changed."""
    text = ctx.env_path.read_text() if ctx.env_path.exists() else (ctx.base / ".env.example").read_text()
    changed = write_file(ctx.env_path, upsert_env(text, updates), mode=0o600)
    ctx.system.chown(ctx.env_path, ctx.user)
    if changed:
        ctx.changed(".env (mode 600)")
    return changed


def step_telegram(ctx):
    env, args = ctx.env(), ctx.args
    token = args.bot_token or env.get("TELEGRAM_BOT_TOKEN", "")
    me = None
    for attempt in range(3):
        if not token:
            token = ask(ctx, "Telegram bot token from @BotFather", "--bot-token", secret=True)
        try:
            me = tg(ctx, token, "getMe")
            break
        except InstallError as exc:
            ctx.say(f"  bot token rejected ({exc})")
            if args.non_interactive:
                break
            token = ""
    if not me:
        raise InstallError("the Telegram bot token does not validate with getMe")
    ctx.say(f"Telegram bot: @{me.get('username')}")
    updates = {"TELEGRAM_BOT_TOKEN": token}
    chat_id = args.chat_id or env.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        message = wait_for_message(ctx, token, me.get("username"))
        chat_id = str(message["chat"]["id"])
        if message["chat"].get("type") != "private" and message.get("from"):
            updates["TELEGRAM_USER_ID"] = str(message["from"]["id"])
    updates["TELEGRAM_CHAT_ID"] = chat_id
    return updates


def probe(ctx, claude_bin, token):
    """One real `claude -p` call with this login; (ok, detail)."""
    cwd = ctx.base / "workspace"
    proc = ctx.system.run_as(
        ctx.user, [claude_bin, "-p", "--model", PROBE_MODEL, "--output-format", "json"],
        input="Reply with the single word: ok", extra_env={"CLAUDE_CODE_OAUTH_TOKEN": token},
        cwd=cwd, timeout=120)
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    if proc.returncode != 0:
        return False, detail[-1][:200] if detail else f"exit {proc.returncode}"
    try:
        envelope = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return True, "ok"
    if isinstance(envelope, dict) and envelope.get("is_error"):
        return False, str(envelope.get("result", "error"))[:200]
    return True, "ok"


def run_setup_token(ctx, claude_bin):
    if ctx.args.non_interactive:
        raise InstallError("the claude login token is missing or rejected; pass --oauth-token")
    ctx.say("Running `claude setup-token`: follow its prompts, then the token is stored for you.")
    proc = ctx.system.popen_as(ctx.user, [claude_bin, "setup-token"])
    seen = ""
    for chunk in iter(lambda: proc.stdout.read(1), ""):
        sys.stdout.write(chunk)
        sys.stdout.flush()
        seen += chunk
    proc.wait()
    proc.stdout.close()
    token = extract_token(seen)
    if token is None:
        token = ask(ctx, "Paste the token it printed", "--oauth-token", secret=True)
    return token


def step_token(ctx, claude_bin):
    env, args = ctx.env(), ctx.args
    token = args.oauth_token or env.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if token:
        ok, detail = probe(ctx, claude_bin, token)
        if ok:
            ctx.say("claude login: works")
            created = env.get("CLAUDE_CODE_OAUTH_TOKEN_CREATED")
            if token == env.get("CLAUDE_CODE_OAUTH_TOKEN") and created:
                ensure_renewal_ics(ctx, created)
                return {}
            if token == env.get("CLAUDE_CODE_OAUTH_TOKEN"):
                ctx.say("  token age unknown; counting its year from today")
        else:
            ctx.say(f"  stored login fails ({detail})")
            if args.oauth_token:
                raise InstallError(f"--oauth-token does not work: {detail}")
            token = ""
    if not token:
        token = run_setup_token(ctx, claude_bin)
        ok, detail = probe(ctx, claude_bin, token)
        if not ok:
            raise InstallError(f"the new token fails a claude -p call: {detail}")
    created = datetime.date.today().isoformat()
    ensure_renewal_ics(ctx, created)
    ctx.say(f"  renew by {datetime.date.fromisoformat(created) + datetime.timedelta(days=TOKEN_WARN_DAYS)}"
            f" (calendar file: state/claude-token-renewal.ics)")
    return {"CLAUDE_CODE_OAUTH_TOKEN": token, "CLAUDE_CODE_OAUTH_TOKEN_CREATED": created}


def ensure_renewal_ics(ctx, created):
    path = ctx.base / "state" / "claude-token-renewal.ics"
    if write_file(path, renewal_ics(datetime.date.fromisoformat(created))):
        ctx.system.chown(path, ctx.user)
        ctx.changed("state/claude-token-renewal.ics")


def step_deny_rules(ctx):
    path = ctx.system.home(ctx.user) / ".claude" / "settings.json"
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        raise InstallError(f"{path} is not valid JSON; fix or remove it and rerun")
    deny = data.setdefault("permissions", {}).setdefault("deny", [])
    merged = sorted(set(deny) | set(deny_rules(ctx.base)))
    if merged == deny and path.exists():
        return
    data["permissions"]["deny"] = merged
    path.parent.mkdir(parents=True, exist_ok=True)
    write_file(path, json.dumps(data, indent=2) + "\n")
    ctx.system.chown(path.parent, ctx.user)
    ctx.system.chown(path, ctx.user)
    ctx.changed(f"{path} (deny rules for .env, state/ and the claude credentials)")


def template_values(ctx, claude_bin):
    return {"BASE": ctx.base, "USER": ctx.user, "GROUP": ctx.system.group(ctx.user),
            "CLAUDE_DIR": Path(claude_bin).parent}


def rendered_unit(ctx, claude_bin):
    return render((ctx.base / "coordinator" / f"{SERVICE}.service.template").read_text(),
                  template_values(ctx, claude_bin))


def step_render(ctx, claude_bin, env_changed):
    values = template_values(ctx, claude_bin)
    guide = ctx.base / "coordinator" / "CLAUDE.md"
    if write_file(guide, render((ctx.base / "coordinator" / "CLAUDE.md.template").read_text(), values)):
        ctx.system.chown(guide, ctx.user)
        ctx.changed("coordinator/CLAUDE.md")
    unit = ctx.system.unit_dir / f"{SERVICE}.service"
    unit_changed = write_file(unit, rendered_unit(ctx, claude_bin))
    if unit_changed:
        ctx.changed(str(unit))
    dropin = ctx.system.unit_dir / f"{SERVICE}.service.d" / "override.conf"
    if dropin.exists() and re.search(r"^\s*User=", dropin.read_text(), re.M):
        ctx.say(f"  warning: {dropin} sets User= and overrides the unit; remove it:\n"
                f"    sudo rm -r {dropin.parent} && sudo systemctl daemon-reload")
    if not ctx.system.has_systemd():
        ctx.say("  systemd is not running here: unit written, not enabled")
        return
    if unit_changed:
        ctx.system.systemctl("daemon-reload")
    active = ctx.system.systemctl("is-active", SERVICE).stdout.strip() == "active"
    enabled = ctx.system.systemctl("is-enabled", SERVICE).stdout.strip() == "enabled"
    if not (active and enabled):
        ctx.system.systemctl("enable", "--now", SERVICE)
        ctx.changed(f"{SERVICE} enabled and started")
    elif unit_changed or env_changed:
        ctx.system.systemctl("restart", SERVICE)
        ctx.changed(f"{SERVICE} restarted")


def step_cron(ctx):
    existing = ctx.system.crontab_read(ctx.user)
    merged = merge_crontab(existing, ctx.base)
    if merged != existing:
        ctx.system.crontab_write(ctx.user, merged)
        ctx.changed(f"crontab for {ctx.user} (one dispatcher line, one daily check)")


def clock_problems(ctx):
    info = ctx.system.timedatectl()
    if info is None:
        return ["timedatectl is unavailable; check the timezone and NTP yourself"]
    problems = []
    if info.get("NTPSynchronized") != "yes":
        problems.append("the clock is not NTP-synchronized (a Pi has no battery clock): timedatectl set-ntp true")
    if info.get("Timezone") in ("UTC", "Etc/UTC", "Etc/UCT", "", None):
        problems.append("timezone is UTC; recurring schedules use local time: timedatectl set-timezone Area/City")
    return problems


def step_smoke(ctx):
    if any((ctx.base / "tasks" / "pending").glob("*.md")):
        ctx.say("smoke test skipped: the queue has tasks and a run would execute one")
        return
    proc = ctx.system.run_as(ctx.user, [str(ctx.venv_python), str(ctx.base / "dispatcher.py")],
                             cwd=ctx.base, timeout=180)
    if proc.returncode != 0:
        raise InstallError(f"smoke test failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[-300:]}")
    ctx.say("smoke test: dispatcher ran on the empty queue")


def install(ctx):
    preflight(ctx)
    step_user(ctx)
    claude_bin = find_claude(ctx)
    step_venv(ctx)
    updates = step_telegram(ctx)
    updates["CLAUDE_BIN"] = claude_bin
    env_changed = write_env(ctx, updates)
    token_updates = step_token(ctx, claude_bin)
    if token_updates:
        env_changed = write_env(ctx, token_updates) or env_changed
    step_deny_rules(ctx)
    step_render(ctx, claude_bin, env_changed)
    step_cron(ctx)
    for problem in clock_problems(ctx):
        ctx.say(f"  warning: {problem}")
    ctx.system.chown(ctx.base, ctx.user, recursive=True)
    step_smoke(ctx)
    if ctx.changes:
        env = ctx.env()
        try:
            tg(ctx, env["TELEGRAM_BOT_TOKEN"], "sendMessage",
               {"chat_id": env["TELEGRAM_CHAT_ID"], "text": "setup complete"})
        except InstallError as exc:
            ctx.say(f"  could not send the Telegram confirmation: {exc}")
        ctx.say("Done.")
    else:
        ctx.say("Nothing to change: already set up.")
    return 0


# ---------------------------------------------------------------- --check


def run_checks(ctx):
    """A Result for every moving part. A FAIL is always worth a message; a WARN only if it says so."""
    results = []

    def add(ok, name, detail, level=FAIL, notify=None):
        wanted = (level == FAIL) if notify is None else notify
        results.append(Result(PASS if ok else level, name, detail, wanted and not ok))

    env = ctx.env()
    add(ctx.env_path.exists(), ".env exists", str(ctx.env_path))
    if ctx.env_path.exists():
        mode = ctx.env_path.stat().st_mode & 0o777
        add(mode == 0o600, ".env mode", f"{mode:o} (want 600)")

    claude_bin = env.get("CLAUDE_BIN", "")
    runnable = bool(claude_bin) and ctx.system.run_as(ctx.user, ["test", "-x", claude_bin]).returncode == 0
    add(runnable, "claude binary", claude_bin or "CLAUDE_BIN is not set")
    token = env.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if runnable:
        if not token:
            add(False, "claude login probe", "CLAUDE_CODE_OAUTH_TOKEN is not set in .env")
        else:
            ok, detail = probe(ctx, claude_bin, token)
            add(ok, "claude login probe", detail)

    created = env.get("CLAUDE_CODE_OAUTH_TOKEN_CREATED", "")
    try:
        age = (datetime.date.today() - datetime.date.fromisoformat(created)).days
        add(age <= TOKEN_WARN_DAYS, "claude token age", f"{age} days (renew before 365; run the installer)",
            level=WARN, notify=True)
    except ValueError:
        add(False, "claude token age", "CLAUDE_CODE_OAUTH_TOKEN_CREATED is not set", level=WARN, notify=False)

    bot = env.get("TELEGRAM_BOT_TOKEN", "")
    try:
        me = tg(ctx, bot, "getMe") if bot else None
        add(bool(me), "telegram getMe", f"@{me.get('username')}" if me else "TELEGRAM_BOT_TOKEN is not set")
    except InstallError as exc:
        add(False, "telegram getMe", str(exc))

    cron = ctx.system.crontab_read(ctx.user).splitlines()
    queue_lines = [line for line in cron if f"{ctx.base}/dispatcher.py" in line and not line.startswith("#")]
    check_lines = [line for line in cron if "install.py --check" in line and not line.startswith("#")]
    add(len(queue_lines) == 1, "cron: dispatcher line", f"{len(queue_lines)} found (want 1)")
    add(len(check_lines) == 1, "cron: daily check line", f"{len(check_lines)} found (want 1)")

    if not ctx.system.has_systemd():
        add(False, "service", "systemd is not running", level=WARN)
    else:
        active = ctx.system.systemctl("is-active", SERVICE).stdout.strip()
        add(active == "active", "service active", f"{SERVICE}: {active}")
        who = ctx.system.systemctl("show", "-p", "User", "--value", SERVICE).stdout.strip()
        add(who == ctx.user, "service user", f"{who or 'root'} (want {ctx.user})")
    unit = ctx.system.unit_dir / f"{SERVICE}.service"
    try:
        add(unit.exists() and unit.read_text() == rendered_unit(ctx, claude_bin or "/usr/local/bin/claude"),
            "unit matches template", str(unit))
    except (InstallError, OSError) as exc:
        add(False, "unit matches template", str(exc))

    missing = [rel for rel in QUEUE_DIRS if ctx.system.run_as(ctx.user, ["test", "-w", str(ctx.base / rel)]).returncode]
    add(not missing, "queue folders writable", ", ".join(missing) or "all present")
    imports = ctx.system.run_as(ctx.user, [str(ctx.venv_python), "-c", "import httpx, tenacity, filelock"])
    add(imports.returncode == 0, "venv libraries", imports.stderr.strip()[-120:] or "importable")
    problems = clock_problems(ctx)
    add(not problems, "clock and timezone", "; ".join(problems) or "NTP synced, local timezone", level=WARN)
    return results


def run_check(ctx):
    results = run_checks(ctx)
    shown = [r for r in results if r.level != PASS or not ctx.args.quiet]
    if shown:
        print("\n".join(r.line() for r in shown))
    alerts = [r.line() for r in results if r.notify]
    if ctx.args.notify and alerts:
        env = ctx.env()
        try:
            tg(ctx, env.get("TELEGRAM_BOT_TOKEN", ""), "sendMessage",
               {"chat_id": env.get("TELEGRAM_CHAT_ID", ""),
                "text": f"task queue check on {socket.gethostname()}:\n" + "\n".join(alerts)})
        except InstallError as exc:
            print(f"could not notify Telegram: {exc}", file=sys.stderr)
    return 1 if any(r.level == FAIL for r in results) else 0


# ---------------------------------------------------------------- entry point


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Install or check the Claude task queue.")
    parser.add_argument("--check", action="store_true", help="health check instead of install")
    parser.add_argument("--quiet", action="store_true", help="print only problems")
    parser.add_argument("--notify", action="store_true", help="with --check: message Telegram on a failure")
    parser.add_argument("--non-interactive", action="store_true", help="never prompt; take inputs from flags")
    parser.add_argument("--bot-token", help="Telegram bot token (default: ask)")
    parser.add_argument("--chat-id", help="Telegram chat id (default: learn it from your first message)")
    parser.add_argument("--oauth-token", help="claude setup-token value (default: run setup-token)")
    parser.add_argument("--claude-bin", help="path to claude (default: search)")
    parser.add_argument("--user", default="taskq", help="user that runs the queue (default: taskq)")
    parser.add_argument("--base", default=str(REPO), help="queue directory (default: this checkout)")
    parser.add_argument("--wait", type=int, default=300, help="seconds to wait for your first Telegram message")
    parser.add_argument("--allow-non-root", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None, system=None):
    ctx = Ctx(parse_args(argv), system or System())
    try:
        return run_check(ctx) if ctx.args.check else install(ctx)
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; rerun to continue (finished steps are skipped)", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
