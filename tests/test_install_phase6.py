"""Phase 6: the event-driven start, installed.

A systemd .path unit watches tasks/pending/ and starts taskq-dispatch.service, a
oneshot that drains the queue. Cron stays as the fallback. Same fake host as
test_install.py.
"""

import getpass
import hashlib
import shutil
import subprocess
import unittest

from test_install import REPO, FakeSystem, InstallCase  # puts scripts/ on sys.path

import install

PATH_UNIT, SERVICE_UNIT = "taskq-dispatch.path", "taskq-dispatch.service"
TEMPLATES = ("coordinator/taskq-dispatch.path.template", "coordinator/taskq-dispatch.service.template")


class UnitSystem(FakeSystem):
    """FakeSystem that remembers each unit's state separately."""

    state = None  # unit name -> {"active", "enabled"}; set when the fake host is swapped in

    def systemctl(self, *args):
        self.systemctl_calls.append(args)
        state = self.state.setdefault(args[-1], {"active": False, "enabled": False})
        out = ""
        if args[0] == "is-active":
            out = "active" if state["active"] else "inactive"
        elif args[0] == "is-enabled":
            out = "enabled" if state["enabled"] else "disabled"
        elif args[0] == "enable":
            state.update(active=True, enabled=True)
        elif args[0] == "show":
            out = "taskq"
        return subprocess.CompletedProcess(args, 0, out, "")


class DispatchUnitCase(InstallCase):
    def setUp(self):
        super().setUp()
        for rel in TEMPLATES:
            shutil.copy2(REPO / rel, self.base / rel)
        self.system.__class__ = UnitSystem  # keep the host InstallCase built (same folders), track units one by one
        self.system.state = {}
        self.system.systemd = True

    def unit(self, name):
        return (self.system.unit_dir / name).read_text()


class RenderTests(DispatchUnitCase):
    def test_install_writes_both_units_for_this_base_and_user(self):
        self.install_ok()

        path_unit, service = self.unit(PATH_UNIT), self.unit(SERVICE_UNIT)
        self.assertIn(f"PathChanged={self.base}/tasks/pending", path_unit)
        self.assertIn(f"Unit={SERVICE_UNIT}", path_unit)
        self.assertIn("User=taskq", service)
        self.assertIn(f"ExecStart={self.base}/.venv/bin/python3 {self.base}/dispatcher.py", service)
        self.assertIn(f"WorkingDirectory={self.base}", service)
        self.assertIn(f"PATH={self.claude.parent}:", service)
        for text in (path_unit, service):
            self.assertNotIn("{{", text)
            self.assertNotIn("/root/", text)

    def test_the_service_drains_the_queue_waits_for_the_lock_and_has_no_time_limit(self):
        self.install_ok()

        service = self.unit(SERVICE_UNIT)
        self.assertIn("Type=oneshot", service)
        self.assertIn("Environment=DISPATCH_DRAIN=1", service)
        self.assertIn("Environment=DISPATCH_LOCK_WAIT_SECONDS=", service)
        self.assertIn("TimeoutStartSec=0", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertNotIn("[Install]", service)  # only the path unit is enabled; it starts the service

    def test_the_path_unit_watches_for_changes_not_for_a_non_empty_folder(self):
        self.install_ok()

        path_unit = self.unit(PATH_UNIT)
        self.assertIn("PathChanged=", path_unit)
        self.assertNotIn("DirectoryNotEmpty", path_unit)  # tasks waiting on a dependency would restart it forever
        self.assertIn("WantedBy=multi-user.target", path_unit)

    def test_the_repo_templates_hold_no_fixed_path(self):
        for name in TEMPLATES:
            text = (REPO / name).read_text()
            self.assertNotIn("/root/", text, name)
            self.assertNotIn("/home/", text, name)
            self.assertIn("{{BASE}}", text, name)

    def test_the_cron_fallback_is_kept(self):
        self.install_ok()
        crontab = self.system.crontabs["taskq"]
        self.assertEqual(crontab.count("dispatcher.py"), 1)
        self.assertIn("*/15 * * * *", crontab)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is not installed")
    def test_systemd_analyze_accepts_both_units(self):
        self.install_ok("--user", getpass.getuser())
        proc = subprocess.run(
            ["systemd-analyze", "verify", str(self.system.unit_dir / PATH_UNIT), str(self.system.unit_dir / SERVICE_UNIT)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_second_run_changes_nothing(self):
        self.install_ok()
        before = {n: hashlib.sha256((self.system.unit_dir / n).read_bytes()).hexdigest() for n in (PATH_UNIT, SERVICE_UNIT)}
        calls = len(self.system.systemctl_calls)
        out = self.install_ok()

        after = {n: hashlib.sha256((self.system.unit_dir / n).read_bytes()).hexdigest() for n in (PATH_UNIT, SERVICE_UNIT)}
        self.assertEqual(before, after)
        self.assertIn("Nothing to change", out)
        self.assertFalse([c for c in self.system.systemctl_calls[calls:] if c[0] in ("enable", "daemon-reload", "restart")])


class EnableTests(DispatchUnitCase):
    def test_with_systemd_the_path_unit_is_enabled_and_started_but_the_service_is_not(self):
        self.install_ok()

        self.assertEqual(self.system.state[PATH_UNIT], {"active": True, "enabled": True})
        enabled = [c for c in self.system.systemctl_calls if c[0] == "enable"]
        self.assertIn(("enable", "--now", PATH_UNIT), enabled)
        self.assertNotIn(("enable", "--now", SERVICE_UNIT), enabled)

    def test_without_systemd_the_units_are_written_and_nothing_is_enabled(self):
        self.system.systemd = False
        out = self.install_ok()

        self.assertTrue((self.system.unit_dir / PATH_UNIT).exists())
        self.assertEqual(self.system.systemctl_calls, [])
        self.assertIn("not enabled", out)

    def test_a_changed_unit_triggers_a_daemon_reload(self):
        self.install_ok()
        (self.system.unit_dir / SERVICE_UNIT).write_text("stale")
        calls = len(self.system.systemctl_calls)
        self.install_ok()

        self.assertIn(("daemon-reload",), self.system.systemctl_calls[calls:])
        self.assertIn("DISPATCH_DRAIN=1", self.unit(SERVICE_UNIT))

    def test_the_queue_folders_for_phase_6_exist_and_are_checked(self):
        self.install_ok()

        for rel in ("tasks/proposed", "worktrees"):
            self.assertTrue((self.base / rel).is_dir(), rel)
            self.assertIn(rel, install.QUEUE_DIRS)


class CheckTests(DispatchUnitCase):
    def setUp(self):
        super().setUp()
        self.install_ok()
        self.telegram.requests.clear()

    def check(self, *extra):
        return self.run_main("--check", *extra, defaults=False)

    def test_check_passes_the_event_units(self):
        code, out, _ = self.check()

        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"PASS +dispatch path unit")
        self.assertRegex(out, r"PASS +dispatch units match templates")

    def test_check_warns_when_the_path_unit_is_not_running_and_cron_still_covers_it(self):
        self.system.state[PATH_UNIT]["active"] = False
        code, out, _ = self.check("--notify")

        self.assertEqual(code, 0, out)  # a warning, not a failure: cron is the fallback
        self.assertRegex(out, r"WARN +dispatch path unit")
        self.assertTrue(any("dispatch path unit" in m.params["text"] for m in self.telegram.calls("sendMessage")))

    def test_check_flags_a_unit_that_no_longer_matches_its_template(self):
        (self.system.unit_dir / SERVICE_UNIT).write_text("edited by hand")
        _, out, _ = self.check()
        self.assertRegex(out, r"(WARN|FAIL) +dispatch units match templates")

    def test_check_warns_when_the_approval_gate_is_off(self):
        text = (self.base / ".env").read_text().replace("APPROVAL_MIN_TIER=1", "APPROVAL_MIN_TIER=0")
        (self.base / ".env").write_text(text)
        _, out, _ = self.check()
        self.assertRegex(out, r"WARN +approval gate")

    def test_check_passes_the_approval_gate_when_it_is_on(self):
        _, out, _ = self.check()
        self.assertRegex(out, r"PASS +approval gate")


class UpgradeTests(DispatchUnitCase):
    def test_an_existing_env_without_the_gate_gets_it_turned_on_once(self):
        self.install_ok()
        env = (self.base / ".env").read_text()
        (self.base / ".env").write_text("\n".join(
            line for line in env.splitlines() if not line.startswith("APPROVAL_MIN_TIER=")) + "\n")
        self.assertNotIn("APPROVAL_MIN_TIER", self.env())

        self.install_ok()
        self.assertEqual(self.env()["APPROVAL_MIN_TIER"], "1")

    def test_an_explicit_choice_is_left_alone(self):
        self.install_ok()
        (self.base / ".env").write_text((self.base / ".env").read_text().replace("APPROVAL_MIN_TIER=1", "APPROVAL_MIN_TIER=2"))
        self.install_ok()
        self.assertEqual(self.env()["APPROVAL_MIN_TIER"], "2")

    def test_a_base_without_the_new_templates_still_installs(self):
        for rel in TEMPLATES:
            (self.base / rel).unlink()
        self.install_ok()
        self.assertFalse((self.system.unit_dir / PATH_UNIT).exists())


if __name__ == "__main__":
    unittest.main()
