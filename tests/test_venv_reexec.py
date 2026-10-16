"""The README, the coordinator and cron all say `python3 dispatcher.py ...`.

The dispatcher needs filelock, httpx and tenacity, which live in the queue's own
.venv, not in the system python. Run with the system python it used to stop with
"No module named 'filelock'", and the coordinator, which may only run
`python3 .../dispatcher.py check`, read that as a broken venv. When the libraries are missing and
`<base>/.venv/bin/python3` exists, a script run now hands over to it once. Importing
dispatcher as a module (the Telegram bridge does) never does.
"""

import stat
import subprocess
import sys
import unittest

from test_baseline import Sandbox


class VenvCase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.marker = self.sb.root / "venv-python-ran"

    def fake_venv_python(self, extra_flags=""):
        """A stand-in for .venv/bin/python3 that records its use, then runs the test interpreter (which has the libraries)."""
        path = self.sb.base / ".venv" / "bin" / "python3"
        path.parent.mkdir(parents=True)
        path.write_text(f'#!/bin/sh\necho "$@" >> "{self.marker}"\nexec "{sys.executable}" {extra_flags} "$@"\n')
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def without_libraries(self, *args):
        """Run the sandbox's dispatcher under an interpreter that cannot see the installed libraries (-S drops site-packages)."""
        return subprocess.run(
            [sys.executable, "-S", str(self.sb.base / "dispatcher.py"), *args],
            cwd=self.sb.base, env=self.sb.env(), capture_output=True, text=True, timeout=60,
        )

    def task_path(self):
        self.sb.add_task("t")
        return str(self.sb.base / "tasks" / "pending" / "t.md")


class HandOverTests(VenvCase):
    def test_check_works_from_an_interpreter_without_the_libraries_when_a_venv_exists(self):
        self.fake_venv_python()
        proc = self.without_libraries("check", self.task_path())

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("needs the libraries", proc.stdout + proc.stderr)
        self.assertTrue(self.marker.exists(), "the venv python was never used")

    def test_the_arguments_arrive_unchanged(self):
        self.fake_venv_python()
        path = self.task_path()
        self.without_libraries("check", path)

        self.assertIn(f"{self.sb.base}/dispatcher.py check {path}", self.marker.read_text())

    def test_the_exit_code_of_the_real_command_is_kept(self):
        self.fake_venv_python()
        proc = self.without_libraries("check", str(self.sb.base / "nope.md"))

        self.assertEqual(proc.returncode, 1)
        self.assertIn("no such file", proc.stdout)

    def test_cancel_and_retry_work_the_same_way(self):
        self.fake_venv_python()
        self.sb.add_task("t")
        proc = self.without_libraries("cancel", "t")

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.sb.names("cancelled"), ["t"])

    def test_a_venv_python_that_also_lacks_the_libraries_is_tried_once_not_in_a_loop(self):
        self.fake_venv_python(extra_flags="-S")  # the hand-over target is broken too
        proc = self.without_libraries("check", self.task_path())

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("needs the libraries", proc.stdout + proc.stderr)
        self.assertEqual(len(self.marker.read_text().splitlines()), 1)


class NoVenvTests(VenvCase):
    def test_without_a_venv_the_message_says_what_to_do_and_nothing_runs(self):
        proc = self.without_libraries("check", self.task_path())

        self.assertNotEqual(proc.returncode, 0)
        text = proc.stdout + proc.stderr
        self.assertIn("needs the libraries", text)
        self.assertIn(".venv/bin/pip install --require-hashes", text)

    def test_importing_the_module_never_hands_over(self):
        """The Telegram bridge imports dispatcher; replacing that process with the dispatcher script would kill the bot."""
        self.fake_venv_python()
        proc = subprocess.run(
            [sys.executable, "-S", "-c", "import dispatcher"],
            cwd=self.sb.base, env=self.sb.env(), capture_output=True, text=True, timeout=60,
        )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("needs the libraries", proc.stdout + proc.stderr)
        self.assertFalse(self.marker.exists())

    def test_with_the_libraries_present_the_venv_is_not_involved(self):
        self.fake_venv_python()
        proc = self.sb.python(str(self.sb.base / "dispatcher.py"), "check", self.task_path())

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
