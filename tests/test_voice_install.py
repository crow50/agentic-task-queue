"""Phase 10: `install.py --check` looks at the voice tools only once they are configured, and only warns."""

import sys
import unittest
from pathlib import Path

from test_install import InstallCase

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))


class VoiceCheckTests(InstallCase):
    def setUp(self):
        super().setUp()
        self.install_ok()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name in ("ffmpeg", "whisper-cli", "piper"):
            (self.tools / name).write_text("#!/bin/sh\n")
            (self.tools / name).chmod(0o755)
        (self.tools / "model.bin").write_text("x")
        (self.tools / "voice.onnx").write_text("x")

    def set_env(self, **values):
        text = (self.base / ".env").read_text()
        for key, value in values.items():
            lines = [line for line in text.splitlines() if not line.startswith(f"{key}=")]
            text = "\n".join(lines) + f"\n{key}={value}\n"
        (self.base / ".env").write_text(text)

    def check(self):
        return self.run_main("--check", defaults=False)

    def full(self):
        return dict(FFMPEG_BIN=self.tools / "ffmpeg", WHISPER_BIN=self.tools / "whisper-cli", WHISPER_MODEL=self.tools / "model.bin",
                    TTS_BIN=self.tools / "piper", TTS_MODEL=self.tools / "voice.onnx")

    def test_nothing_is_checked_while_voice_is_not_configured(self):
        _, out, _ = self.check()
        self.assertNotIn("voice:", out)

    def test_a_complete_setup_passes_every_voice_check(self):
        self.set_env(**self.full())
        code, out, _ = self.check()
        self.assertEqual(code, 0, out)
        for name in ("ffmpeg", "whisper binary", "whisper model", "speaker binary", "speaker voice"):
            self.assertRegex(out, rf"PASS +voice: {name}")

    def test_a_missing_model_or_binary_is_a_warning_not_a_failure(self):
        self.set_env(**{**self.full(), "WHISPER_MODEL": self.tools / "nope.bin", "TTS_BIN": self.tools / "gone"})
        code, out, _ = self.check()
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"WARN +voice: whisper model")
        self.assertRegex(out, r"WARN +voice: speaker binary")

    def test_a_whisper_binary_without_a_model_says_what_is_missing(self):
        self.set_env(WHISPER_BIN=self.tools / "whisper-cli", FFMPEG_BIN=self.tools / "ffmpeg")
        _, out, _ = self.check()
        self.assertRegex(out, r"WARN +voice: whisper model \(WHISPER_MODEL\): not set in .env")


if __name__ == "__main__":
    unittest.main()
