"""scripts/install.py end to end, against a fake host, FakeTelegram and fake_claude.

The installer's `System` (users, crontab, systemd, chown) is replaced by FakeSystem, so
nothing here touches the real machine. TELEGRAM_API_BASE points at FakeTelegram, as in
the dispatcher tests. The brief's checks: a second run changes nothing (same checksums,
one queue cron line), .env is mode 600, no `/root/` or fixed home path is left in the
generated files, the unit passes `systemd-analyze verify`, and `--check` is all PASS.
"""

import contextlib
import getpass
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fake_telegram import FakeTelegram

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
sys.path.insert(0, str(REPO / "scripts"))
import install  # noqa: E402

BOT_TOKEN = "123456:TEST-bot-token"
OAUTH = "sk-ant-oat01-TESTTESTTESTTESTTESTTEST"
UPDATE = {"update_id": 7, "message": {"chat": {"id": 4242, "type": "private"}, "from": {"id": 4242}, "text": "hi"}}


class FakeSystem(install.System):
    def __init__(self, root, systemd=False):
        self.root = root
        self.unit_dir = root / "units"
        self.unit_dir.mkdir()
        self.crontabs = {}
        self.systemd = systemd
        self.units = {"active": False, "enabled": False}
        self.systemctl_calls = []
        self.clock = {"Timezone": "America/New_York", "NTPSynchronized": "yes"}

    def is_root(self):
        return True

    def must_switch(self, user):
        return False

    def user_exists(self, user):
        return True

    def home(self, user):
        return self.root / "home"

    def group(self, user):
        return user

    def chown(self, path, user, recursive=False):
        pass

    def is_user(self, user):
        return True

    def crontab_read(self, user):
        return self.crontabs.get(user, "")

    def crontab_write(self, user, text):
        self.crontabs[user] = text

    def has_systemd(self):
        return self.systemd

    def systemctl(self, *args):
        self.systemctl_calls.append(args)
        out = ""
        if args[0] == "is-active":
            out = "active" if self.units["active"] else "inactive"
        elif args[0] == "is-enabled":
            out = "enabled" if self.units["enabled"] else "disabled"
        elif args[0] == "enable":
            self.units.update(active=True, enabled=True)
        elif args[0] == "show":
            out = "taskq"
        return subprocess.CompletedProcess(args, 0, out, "")

    def timedatectl(self):
        return self.clock


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="taskq-install-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.base = self.root / "queue"
        (self.root / "home").mkdir()
        for rel in ("dispatcher.py", "coordinator_bot.py", "requirements.txt", ".env.example",
                    "coordinator/CLAUDE.md.template", "coordinator/claude-coordinator.service.template",
                    "scripts/install.py"):
            (self.base / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, self.base / rel)
        python = self.base / ".venv" / "bin" / "python3"  # the repo's venv has the libraries
        python.parent.mkdir(parents=True)
        python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        python.chmod(0o755)
        self.claude = self.root / "bin" / "claude"
        self.claude.parent.mkdir()
        self.claude.symlink_to(TESTS / "fake_claude.py")  # it finds its fixtures next to itself

        self.telegram = FakeTelegram()
        self.telegram.start()
        self.addCleanup(self.telegram.stop)
        self.telegram.updates.append(UPDATE)
        scenario = self.root / "scenario.json"
        scenario.write_text("{}")
        self.calls = Path(f"{scenario}.calls.jsonl")
        patcher = mock.patch.dict(os.environ, {
            "TELEGRAM_API_BASE": self.telegram.url, "FAKE_CLAUDE_SCENARIO": str(scenario),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.system = FakeSystem(self.root)

    def run_main(self, *extra, system=None, defaults=True):
        argv = ["--allow-non-root", "--user", "taskq", "--base", str(self.base)]
        if defaults:
            argv += ["--non-interactive", "--bot-token", BOT_TOKEN, "--oauth-token", OAUTH,
                     "--claude-bin", str(self.claude)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = install.main(argv + list(extra), system=system or self.system)
        return code, out.getvalue(), err.getvalue()

    def install_ok(self, *extra):
        code, out, err = self.run_main(*extra)
        self.assertEqual(code, 0, f"{out}\n{err}")
        return out

    def digests(self):
        files = [self.base / ".env", self.base / "coordinator" / "CLAUDE.md",
                 self.system.unit_dir / "claude-coordinator.service",
                 self.base / "state" / "claude-token-renewal.ics",
                 self.root / "home" / ".claude" / "settings.json"]
        found = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
        found["crontab"] = hashlib.sha256(self.system.crontabs["taskq"].encode()).hexdigest()
        return found

    def env(self):
        return install.parse_env((self.base / ".env").read_text())

    def claude_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []


class EndToEndTests(InstallCase):
    def test_first_run_sets_everything_up(self):
        out = self.install_ok()

        env = self.env()
        self.assertEqual(env["TELEGRAM_BOT_TOKEN"], BOT_TOKEN)
        self.assertEqual(env["TELEGRAM_CHAT_ID"], "4242")
        self.assertEqual(env["CLAUDE_BIN"], str(self.claude))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], OAUTH)
        self.assertRegex(env["CLAUDE_CODE_OAUTH_TOKEN_CREATED"], r"^\d{4}-\d\d-\d\d$")
        self.assertEqual(stat.S_IMODE((self.base / ".env").stat().st_mode), 0o600)
        self.assertIn("MAX_RATE_LIMIT_RETRIES=2", (self.base / ".env").read_text())  # seeded from .env.example
        self.assertEqual(self.telegram.texts(), ["setup complete"])
        self.assertEqual(self.telegram.calls("sendMessage")[0].params["chat_id"], "4242")
        self.assertEqual(self.telegram.calls("getMe")[0].token, BOT_TOKEN)
        offsets = [r.params.get("offset") for r in self.telegram.calls("getUpdates")]
        self.assertIn("8", offsets)  # the first message was consumed
        self.assertIn("smoke test: dispatcher ran", out)
        self.assertNotIn(OAUTH, out)
        self.assertNotIn(BOT_TOKEN, out)

    def test_the_login_is_proved_with_a_real_claude_call_using_the_token(self):
        self.install_ok()

        (probe,) = self.claude_calls()
        self.assertEqual(probe["oauth_token"], OAUTH)
        self.assertEqual(probe["model"], install.PROBE_MODEL)

    def test_a_second_run_changes_nothing(self):
        self.install_ok()
        before = self.digests()
        messages = len(self.telegram.texts())

        out = self.install_ok()

        self.assertEqual(self.digests(), before)
        self.assertEqual(len(self.telegram.texts()), messages)  # no second "setup complete"
        self.assertIn("Nothing to change", out)
        crontab = self.system.crontabs["taskq"]
        self.assertEqual(crontab.count("dispatcher.py"), 1)
        self.assertEqual(crontab.count("install.py --check"), 1)

    def test_it_reads_the_chat_id_from_the_first_message_and_a_group_also_records_the_user(self):
        self.telegram.updates[:] = [{"update_id": 9, "message": {
            "chat": {"id": -100555, "type": "supergroup"}, "from": {"id": 31337}, "text": "hi"}}]
        self.install_ok()

        self.assertEqual(self.env()["TELEGRAM_CHAT_ID"], "-100555")
        self.assertEqual(self.env()["TELEGRAM_USER_ID"], "31337")

    def test_generated_files_have_no_root_path_and_only_the_chosen_base(self):
        self.install_ok()
        generated = [self.base / "coordinator" / "CLAUDE.md", self.system.unit_dir / "claude-coordinator.service"]

        for path in generated:
            text = path.read_text()
            self.assertNotIn("/root/", text, path.name)
            self.assertNotIn("{{", text, path.name)
            self.assertNotIn("/home/crow50", text, path.name)
            self.assertIn(str(self.base), text, path.name)
        unit = generated[1].read_text()
        self.assertIn("User=taskq", unit)
        self.assertIn(f"ExecStart={self.base}/.venv/bin/python3 {self.base}/coordinator_bot.py", unit)
        self.assertIn(f"PATH={self.claude.parent}:", unit)

    def test_the_repo_templates_hold_no_fixed_path(self):
        for name in ("coordinator/CLAUDE.md.template", "coordinator/claude-coordinator.service.template"):
            text = (REPO / name).read_text()
            self.assertNotIn("/root/", text, name)
            self.assertNotIn("/home/", text, name)
            self.assertIn("{{BASE}}", text, name)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is not installed")
    def test_systemd_analyze_accepts_the_unit(self):
        user = getpass.getuser()
        self.install_ok("--user", user)  # the later --user wins; the unit is rendered for a real account
        proc = subprocess.run(
            ["systemd-analyze", "verify", str(self.system.unit_dir / "claude-coordinator.service")],
            capture_output=True, text=True)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_deny_rules_protect_the_secrets_and_keep_the_users_own_settings(self):
        settings = self.root / "home" / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"theme": "dark", "permissions": {"deny": ["Read(//etc/shadow)"]}}))
        self.install_ok()

        data = json.loads(settings.read_text())
        self.assertEqual(data["theme"], "dark")
        deny = data["permissions"]["deny"]
        self.assertIn("Read(//etc/shadow)", deny)
        for rule in (f"Read(/{self.base}/.env)", f"Edit(/{self.base}/.env)", f"Read(/{self.base}/state/**)",
                     "Read(~/.claude/.credentials.json)", "Edit(~/.claude/.credentials.json)"):
            self.assertIn(rule, deny)

    def test_the_renewal_reminder_is_a_calendar_file_eleven_months_out(self):
        self.install_ok()

        ics = (self.base / "state" / "claude-token-renewal.ics").read_text()
        self.assertIn("BEGIN:VEVENT", ics)
        self.assertRegex(ics, r"DTSTART;VALUE=DATE:\d{8}")


class CronTests(InstallCase):
    def test_an_existing_dispatcher_line_is_replaced_not_appended_to(self):
        self.system.crontabs["taskq"] = (
            "MAILTO=me@example.com\n"
            f"*/15 * * * * /usr/bin/python3 {self.base}/dispatcher.py >> /tmp/old.log 2>&1\n"
            "0 1 * * * /usr/local/bin/other-job\n"
        )
        self.install_ok()

        crontab = self.system.crontabs["taskq"]
        self.assertEqual(crontab.count("dispatcher.py"), 1)
        self.assertNotIn("/tmp/old.log", crontab)
        self.assertIn("MAILTO=me@example.com", crontab)
        self.assertIn("other-job", crontab)
        self.assertIn(f"{self.base}/.venv/bin/python3 {self.base}/dispatcher.py >> {self.base}/logs/cron.log 2>&1",
                      crontab)

    def test_merge_is_stable_when_applied_twice(self):
        once = install.merge_crontab("", self.base)
        self.assertEqual(install.merge_crontab(once, self.base), once)


class PureHelperTests(unittest.TestCase):
    def test_upsert_env_updates_in_place_and_appends_what_is_missing(self):
        text = "# comment\nTELEGRAM_BOT_TOKEN=\nKEEP=1\n"
        out = install.upsert_env(text, {"TELEGRAM_BOT_TOKEN": "t", "NEW": "n"})

        self.assertEqual(out, "# comment\nTELEGRAM_BOT_TOKEN=t\nKEEP=1\nNEW=n\n")
        self.assertEqual(install.upsert_env(out, {"TELEGRAM_BOT_TOKEN": "t", "NEW": "n"}), out)

    def test_render_refuses_an_unfilled_placeholder(self):
        self.assertEqual(install.render("a {{X}} b", {"X": 1}), "a 1 b")
        with self.assertRaises(install.InstallError):
            install.render("a {{X}} {{Y}}", {"X": 1})

    def test_extract_token_finds_it_in_noisy_output(self):
        noisy = "\x1b[1mYour token:\x1b[0m\nsk-ant-oat01-AbC_dEf-1234567890abcdefXYZ\nkeep it safe"
        self.assertEqual(install.extract_token(noisy), "sk-ant-oat01-AbC_dEf-1234567890abcdefXYZ")
        self.assertIsNone(install.extract_token("no token here"))


class FailureTests(InstallCase):
    def test_a_rejected_bot_token_stops_the_install_without_echoing_it(self):
        self.telegram.queue_response("getMe", 401)
        code, out, err = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("getMe", err)
        self.assertNotIn(BOT_TOKEN, out + err)
        self.assertFalse((self.base / ".env").exists())

    def test_non_interactive_without_a_token_says_which_flag_to_pass(self):
        code, _, err = self.run_main("--bot-token", "", defaults=False, *("--non-interactive",))

        self.assertEqual(code, 1)
        self.assertIn("--bot-token", err)

    def test_a_login_token_that_fails_a_claude_call_is_refused(self):
        scenario = Path(os.environ["FAKE_CLAUDE_SCENARIO"])
        scenario.write_text(json.dumps({"other": [{"fixture": "auth-expired.txt", "exit": 1}]}))
        code, _, err = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("--oauth-token does not work", err)

    def test_a_missing_claude_prints_the_install_command(self):
        with mock.patch.object(install.shutil, "which", return_value=None):
            code, _, err = self.run_main("--claude-bin", str(self.root / "nope"), defaults=False,
                                         *("--non-interactive", "--bot-token", BOT_TOKEN))
        self.assertEqual(code, 1)
        self.assertIn("claude.ai/install.sh", err)

    def test_the_old_user_override_dropin_is_called_out(self):
        dropin = self.system.unit_dir / "claude-coordinator.service.d"
        dropin.mkdir()
        (dropin / "override.conf").write_text("[Service]\nUser=crow50\n")
        out = self.install_ok()

        self.assertIn("override.conf", out)
        self.assertIn("sets User=", out)

    def test_the_smoke_test_does_not_run_a_real_task(self):
        (self.base / "tasks" / "pending").mkdir(parents=True)
        (self.base / "tasks" / "pending" / "t.md").write_text("---\nmodel: m\n---\nbody")
        out = self.install_ok()

        self.assertIn("smoke test skipped", out)
        self.assertTrue((self.base / "tasks" / "pending" / "t.md").exists())

    def test_setup_token_output_is_captured_when_no_token_is_given(self):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--allow-non-root", "--user", "taskq", "--base", str(self.base), "--bot-token", BOT_TOKEN,
                "--claude-bin", str(self.claude)]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = install.main(argv, system=self.system)

        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertEqual(self.env()["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-FAKEFAKEFAKEFAKEFAKE0123456789")


class CheckTests(InstallCase):
    def check(self, *extra):
        code, out, err = self.run_main("--check", *extra, defaults=False)
        return code, out, err

    def setUp(self):
        super().setUp()
        self.install_ok()
        self.telegram.requests.clear()

    def test_check_is_all_pass_after_an_install(self):
        code, out, _ = self.check()

        self.assertEqual(code, 0, out)
        for name in ("claude binary", "claude login probe", ".env mode", "telegram getMe",
                     "cron: dispatcher line", "cron: daily check line", "queue folders writable",
                     "venv libraries", "unit matches template", "claude token age"):
            self.assertRegex(out, rf"PASS +{name}")
        self.assertNotIn("FAIL", out)

    def test_check_with_systemd_checks_the_service_and_its_user(self):
        self.system.systemd = True
        self.system.units.update(active=True, enabled=True)
        code, out, _ = self.check()

        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"PASS +service active")
        self.assertRegex(out, r"PASS +service user")

    def test_quiet_check_prints_and_sends_nothing_when_all_is_well(self):
        self.system.systemd = True
        self.system.units.update(active=True, enabled=True)
        code, out, err = self.check("--quiet", "--notify")

        self.assertEqual((code, out.strip(), err), (0, "", ""))
        self.assertEqual(self.telegram.calls("sendMessage"), [])

    def test_a_wide_open_env_file_fails_and_messages_telegram(self):
        (self.base / ".env").chmod(0o644)
        code, out, _ = self.check("--quiet", "--notify")

        self.assertEqual(code, 1)
        self.assertRegex(out, r"FAIL +\.env mode")
        (message,) = self.telegram.calls("sendMessage")
        self.assertIn(".env mode", message.params["text"])

    def test_a_lost_cron_line_fails(self):
        self.system.crontabs["taskq"] = ""
        code, out, _ = self.check()

        self.assertEqual(code, 1)
        self.assertRegex(out, r"FAIL +cron: dispatcher line")

    def test_a_dead_login_fails_the_live_probe(self):
        scenario = Path(os.environ["FAKE_CLAUDE_SCENARIO"])
        scenario.write_text(json.dumps({"other": [{"fixture": "auth-expired.txt", "exit": 1}]}))
        code, out, _ = self.check()

        self.assertEqual(code, 1)
        self.assertRegex(out, r"FAIL +claude login probe")

    def test_a_token_older_than_eleven_months_warns_and_notifies_but_does_not_fail(self):
        old = "2020-01-01"
        env_path = self.base / ".env"
        env_path.write_text(install.upsert_env(env_path.read_text(), {"CLAUDE_CODE_OAUTH_TOKEN_CREATED": old}))
        code, out, _ = self.check("--quiet", "--notify")

        self.assertEqual(code, 0)
        self.assertRegex(out, r"WARN +claude token age")
        (message,) = self.telegram.calls("sendMessage")
        self.assertIn("claude token age", message.params["text"])

    def test_an_unsynced_clock_is_a_warning_only(self):
        self.system.clock = {"Timezone": "Etc/UTC", "NTPSynchronized": "no"}
        code, out, _ = self.check()

        self.assertEqual(code, 0)
        self.assertRegex(out, r"WARN +clock and timezone")


if __name__ == "__main__":
    unittest.main()
