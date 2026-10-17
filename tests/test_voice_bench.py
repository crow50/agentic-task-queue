"""Phase 10: scripts/voice_bench.py reports the real-time factor from the same commands the bridge runs."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent


class BenchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="taskq-bench-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for name in ("ffmpeg", "whisper-cli", "piper"):
            shutil.copy2(TESTS / "fake_voice.py", self.root / name)
        (self.root / "whisper-cli.json").write_text(json.dumps({"text": "hello there", "sleep": 0.2}))
        (self.root / "note.oga").write_bytes(b"OggS")

    def run_bench(self, *extra):
        cmd = [sys.executable, str(REPO / "scripts" / "voice_bench.py"), str(self.root / "note.oga"),
               "--ffmpeg-bin", str(self.root / "ffmpeg"), "--whisper-bin", str(self.root / "whisper-cli"),
               "--whisper-model", str(self.root / "m.bin"), *extra]
        return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, timeout=60)

    def test_it_prints_the_audio_length_the_factor_and_what_was_heard(self):
        proc = self.run_bench("--runs", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("audio: 2.0 s", proc.stdout)  # the double's wav is two seconds long
        self.assertIn("2 runs", proc.stdout)
        self.assertRegex(proc.stdout, r"real-time factor: 0\.\d\d with m\.bin")
        self.assertIn("comfortably faster than real time", proc.stdout)
        self.assertIn("heard: hello there", proc.stdout)

    def test_a_slow_model_is_called_slow(self):
        (self.root / "whisper-cli.json").write_text(json.dumps({"text": "x", "sleep": 2.5}))
        proc = self.run_bench("--runs", "1")
        self.assertIn("slower than real time", proc.stdout)

    def test_say_times_the_speaker_and_needs_it_configured(self):
        proc = self.run_bench("--runs", "1", "--say", "Queued it.", "--tts-bin", str(self.root / "piper"), "--tts-model", "v.onnx")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("speech:", proc.stdout)
        self.assertEqual(json.loads((self.root / "piper.calls.jsonl").read_text().splitlines()[0])["stdin"], "Queued it.")

    def test_a_failing_tool_is_reported_with_its_error(self):
        (self.root / "whisper-cli.json").write_text(json.dumps({"exit": 3, "stderr": "no model"}))
        proc = self.run_bench("--runs", "1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no model", proc.stderr)


if __name__ == "__main__":
    unittest.main()
