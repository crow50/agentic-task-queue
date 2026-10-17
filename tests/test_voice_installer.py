"""Phase 10 follow-up: scripts/install_voice.py installs the voice tools, skips what is done, and proves they work."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
sys.path.insert(0, str(REPO / "scripts"))

import install_voice  # noqa: E402


def install_double(path, **config):
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TESTS / "fake_voice.py", path)
    path.chmod(0o755)
    if config:
        Path(f"{path}.json").write_text(json.dumps(config))


class Done:
    def __init__(self, code=0):
        self.returncode = code


class Workshop(unittest.TestCase):
    """A destination folder, stand-ins for ffmpeg and a `run` that does what each real step would have done."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="taskq-vinstall-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.root.chmod(0o755)  # mkdtemp makes it 0700, which is exactly what the permission test looks for
        self.dest = self.root / "voice"
        self.ffmpeg = self.root / "ffmpeg"
        install_double(self.ffmpeg)
        self.layout = install_voice.Layout(self.dest)
        self.commands = []
        self.fail_on = None
        self.heard = "the queue is ready"

    def fake_run(self, cmd, cwd=None, capture_output=False):
        if "ensurepip" in " ".join(cmd):
            return Done()
        self.commands.append([Path(part).name if i == 0 else part for i, part in enumerate(cmd)])
        lay = self.layout
        words = " ".join(str(part) for part in cmd)
        if self.fail_on and self.fail_on in words:
            return Done(2)
        if cmd[0] == "git":
            (lay.whisper_dir / ".git").mkdir(parents=True)
        elif cmd[0] == "sh":
            lay.whisper_model.parent.mkdir(parents=True, exist_ok=True)
            lay.whisper_model.write_bytes(b"model")
        elif cmd[0] == "cmake" and cmd[1] == "-B":
            (lay.whisper_dir / "build").mkdir(parents=True, exist_ok=True)
            (lay.whisper_dir / "build" / "CMakeCache.txt").write_text("x")
        elif cmd[0] == "cmake":
            install_double(lay.whisper_bin, text=self.heard)
        elif "-m" in cmd and "venv" in cmd:
            (lay.venv / "bin").mkdir(parents=True)
            (lay.venv / "bin" / "python").write_text("x")
        elif Path(cmd[0]).name == "pip":
            install_double(lay.tts_bin)
        elif "piper.download_voices" in cmd:
            lay.tts_model.write_bytes(b"voice")
            Path(f"{lay.tts_model}.json").write_text("{}")
        return Done()

    def main(self, *extra, which=lambda name: f"/usr/bin/{name}"):
        return install_voice.main(["--dest", str(self.dest), "--ffmpeg-bin", str(self.ffmpeg), "--env", str(self.root / "queue.env"), *extra],
                                  run=self.fake_run, which=which)

    def run_main(self, *extra, **kw):
        with mock.patch.object(os, "geteuid", return_value=1000, create=True), contextlib.redirect_stdout(io.StringIO()):
            return self.main(*extra, **kw)


class PackageTests(Workshop):
    def test_a_missing_package_is_one_apt_line_and_nothing_is_run(self):
        with self.assertRaises(SystemExit) as stop:
            self.run_main(which=lambda name: None if name in ("cmake", "c++", "g++") else f"/usr/bin/{name}")
        self.assertIn("sudo apt install cmake build-essential", str(stop.exception))
        self.assertEqual(self.commands, [])
        self.assertFalse(self.dest.exists())

    def test_it_refuses_to_run_as_root(self):
        with mock.patch.object(os, "geteuid", return_value=0, create=True), self.assertRaises(SystemExit) as stop:
            self.main()
        self.assertIn("not root", str(stop.exception))
        self.assertEqual(self.commands, [])

    def test_a_destination_it_cannot_create_says_how_to_make_one(self):
        blocked = self.root / "file"
        blocked.write_text("x")
        with mock.patch.object(os, "geteuid", return_value=1000, create=True), self.assertRaises(SystemExit) as stop:
            install_voice.main(["--dest", str(blocked / "voice")], run=self.fake_run, which=lambda n: "/x")
        self.assertIn("sudo chown $USER", str(stop.exception))


class InstallTests(Workshop):
    def test_every_step_runs_once_in_order_and_the_test_passes(self):
        self.assertEqual(self.run_main(), 0)
        self.assertEqual([c[0] for c in self.commands], ["git", "sh", "cmake", "cmake", Path(sys.executable).name, "pip", "python"])
        self.assertTrue(self.layout.whisper_bin.exists() and self.layout.tts_model.exists())

    def test_a_second_run_does_nothing_but_the_test(self):
        self.run_main()
        self.commands.clear()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.commands, [])

    def test_a_failed_step_stops_and_the_next_run_resumes_there(self):
        self.fail_on = "download-ggml-model"
        with self.assertRaises(SystemExit) as stop:
            self.run_main()
        self.assertIn("download the base.en-q5_1 model", str(stop.exception))
        self.fail_on = None
        self.commands.clear()
        self.assertEqual(self.run_main(), 0)
        self.assertNotIn("git", [c[0] for c in self.commands])  # the clone was kept
        self.assertEqual(self.commands[0][0], "sh")

    def test_a_chosen_model_and_voice_are_used(self):
        self.layout = install_voice.Layout(self.dest, "tiny.en-q5_1", "en_US-amy-low")
        self.run_main("--model", "tiny.en-q5_1", "--voice", "en_US-amy-low")
        text = (self.dest / "voice.env").read_text()
        self.assertIn("ggml-tiny.en-q5_1.bin", text)
        self.assertIn("en_US-amy-low.onnx", text)


class EnvTests(Workshop):
    def test_a_writable_env_gets_the_four_keys_once_and_keeps_the_rest(self):
        env = self.root / "queue.env"
        env.write_text("TELEGRAM_BOT_TOKEN=abc\nWHISPER_BIN=/old\n")
        self.run_main()
        self.run_main()
        text = env.read_text()
        self.assertIn("TELEGRAM_BOT_TOKEN=abc", text)
        for key in ("WHISPER_BIN", "WHISPER_MODEL", "TTS_BIN", "TTS_MODEL"):
            self.assertEqual(sum(1 for line in text.splitlines() if line.startswith(f"{key}=")), 1, key)
        self.assertIn(f"WHISPER_BIN={self.layout.whisper_bin}", text)
        self.assertNotIn("/old", text)

    def test_a_env_it_cannot_write_is_left_alone_and_the_command_is_printed(self):
        env = self.root / "queue.env"
        env.write_text("A=1\n")
        env.chmod(0o400)
        shown = io.StringIO()
        with mock.patch.object(os, "geteuid", return_value=1000, create=True), contextlib.redirect_stdout(shown):
            self.main()
        shown = shown.getvalue()
        self.assertEqual(env.read_text(), "A=1\n")
        self.assertIn("sudo -u taskq tee -a", shown)
        self.assertIn(f"TTS_MODEL={self.layout.tts_model}", (self.dest / "voice.env").read_text())

    def test_the_test_failing_writes_nothing(self):
        env = self.root / "queue.env"
        env.write_text("A=1\n")
        self.heard = "banana"
        with mock.patch.object(sys, "stderr"):
            code = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(env.read_text(), "A=1\n")
        self.assertFalse((self.dest / "voice.env").exists())


class SelfTestTests(Workshop):
    def lay_out(self, **whisper):
        lay = self.layout
        install_double(lay.whisper_bin, **({"text": "the queue is ready"} | whisper))
        install_double(lay.tts_bin)
        for path in (lay.whisper_model, lay.tts_model):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        return lay

    def test_a_working_set_passes(self):
        self.assertEqual(install_voice.selftest(self.lay_out(), str(self.ffmpeg)), [])

    def test_the_speaker_is_given_the_sentence(self):
        self.lay_out()
        install_voice.selftest(self.layout, str(self.ffmpeg))
        call = json.loads(Path(f"{self.layout.tts_bin}.calls.jsonl").read_text().splitlines()[0])
        self.assertEqual(call["stdin"], install_voice.SENTENCE)

    def test_a_wrong_transcript_is_a_problem(self):
        problems = install_voice.selftest(self.lay_out(text="banana"), str(self.ffmpeg))
        self.assertTrue(any("heard 'banana'" in p for p in problems), problems)

    def test_ffmpeg_without_opus_is_named(self):
        Path(f"{self.ffmpeg}.json").write_text(json.dumps({"exit": 1, "stderr": "Unknown encoder libopus"}))
        problems = install_voice.selftest(self.lay_out(), str(self.ffmpeg))
        self.assertTrue(any("ffmpeg could not convert" in p for p in problems), problems)

    def test_a_folder_the_queue_user_cannot_enter_is_named(self):
        lay = self.lay_out()
        self.dest.chmod(0o700)
        self.addCleanup(self.dest.chmod, 0o755)
        problems = install_voice.selftest(lay, str(self.ffmpeg))
        self.assertTrue(any(str(self.dest) in p and "cannot be entered" in p for p in problems), problems)
        self.assertEqual(len(problems), len(set(problems)))

    def test_a_model_other_users_cannot_read_is_named(self):
        lay = self.lay_out()
        lay.whisper_model.chmod(0o600)
        problems = install_voice.selftest(lay, str(self.ffmpeg))
        self.assertTrue(any("ggml-" in p and "not readable" in p for p in problems), problems)

    def test_mode_bits_helper(self):
        path = self.root / "tool"
        path.write_text("x")
        path.chmod(0o750)
        self.assertTrue(any("not executable" in p for p in install_voice.reachable(path, need_exec=True)))
        path.chmod(0o755)
        self.assertEqual(install_voice.reachable(path, need_exec=True), [])


class EnvMergeTests(unittest.TestCase):
    def test_merged_env(self):
        self.assertEqual(install_voice.merged_env("A=1\nB=2\n", {"B": "9", "C": "3"}), "A=1\nB=9\nC=3\n")
        self.assertEqual(install_voice.merged_env("", {"C": "3"}), "C=3\n")


if __name__ == "__main__":
    unittest.main()
