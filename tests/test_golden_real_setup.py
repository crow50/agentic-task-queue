"""scripts/golden.py on the Pi: started with the system python (as `sudo python3 ...` does), it must still run the
dispatcher under the checkout's venv, and --real must find the claude login in the queue's own .env."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import golden  # noqa: E402


class QueuePythonTests(unittest.TestCase):
    def test_the_dispatcher_runs_under_the_checkouts_venv_when_it_has_one(self):
        venv = REPO / ".venv" / "bin" / "python3"
        expected = str(venv) if venv.exists() else sys.executable
        self.assertEqual(golden.queue_python(), expected)

    def test_a_golden_run_works_when_started_with_a_python_that_has_no_libraries(self):
        venv = REPO / ".venv" / "bin" / "python3"
        if not venv.exists():
            self.skipTest("no .venv in this checkout")
        proc = subprocess.run(["/usr/bin/python3", "-S", str(REPO / "scripts" / "golden.py"), "--route", "baseline",
                               "--only", "hello"], capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("pip install", proc.stdout + proc.stderr)


class RealSettingsTests(unittest.TestCase):
    def test_read_env_skips_comments_and_blank_values_and_strips_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# note\nCLAUDE_BIN=/home/q/.local/bin/claude\nCLAUDE_CODE_OAUTH_TOKEN=\"tok=en\"\nEMPTY=\n\n")
            env = golden.read_env(path)
        self.assertEqual(env, {"CLAUDE_BIN": "/home/q/.local/bin/claude", "CLAUDE_CODE_OAUTH_TOKEN": "tok=en", "EMPTY": ""})
        self.assertEqual(golden.read_env(Path("/nonexistent/.env")), {})

    def test_the_environment_wins_over_the_queues_env_file(self):
        settings = golden.real_settings({"CLAUDE_BIN": "/a/claude", "CLAUDE_CODE_OAUTH_TOKEN": "env"},
                                        {"CLAUDE_BIN": "/b/claude", "CLAUDE_CODE_OAUTH_TOKEN": "file"})
        self.assertEqual(settings, {"CLAUDE_BIN": "/a/claude", "CLAUDE_CODE_OAUTH_TOKEN": "env"})

    def test_the_token_comes_from_the_env_file_when_sudo_cleared_the_environment(self):
        settings = golden.real_settings({}, {"CLAUDE_BIN": "/b/claude", "CLAUDE_CODE_OAUTH_TOKEN": "file"})
        self.assertEqual(settings["CLAUDE_CODE_OAUTH_TOKEN"], "file")

    def test_no_token_anywhere_leaves_the_key_out_rather_than_writing_an_empty_one(self):
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", golden.real_settings({"CLAUDE_BIN": "/a/claude"}, {}))


if __name__ == "__main__":
    unittest.main()
