"""Phase 10: the pure parts of voice. What counts as a voice message, which questions are answered with no model, the
one-line summary that is read aloud, cleaning a transcript, the tool command lines, and deleting old audio."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from taskq_lib import voice  # noqa: E402


class AttachmentTests(unittest.TestCase):
    def test_a_voice_note_is_found_with_its_length(self):
        msg = {"voice": {"file_id": "abc", "duration": 7, "mime_type": "audio/ogg"}}
        self.assertEqual(voice.attachment(msg), {"file_id": "abc", "seconds": 7, "kind": "voice"})

    def test_an_audio_file_counts_but_a_document_or_video_does_not(self):
        self.assertEqual(voice.attachment({"audio": {"file_id": "a", "duration": 30, "mime_type": "audio/mpeg"}})["kind"], "audio")
        self.assertIsNone(voice.attachment({"audio": {"file_id": "a", "mime_type": "application/pdf"}}))
        self.assertIsNone(voice.attachment({"document": {"file_id": "d"}}))
        self.assertIsNone(voice.attachment({"video": {"file_id": "v"}}))
        self.assertIsNone(voice.attachment({"text": "hello"}))

    def test_a_missing_or_odd_file_id_or_duration_is_safe(self):
        self.assertIsNone(voice.attachment({"voice": {"duration": 3}}))
        self.assertIsNone(voice.attachment({"voice": "x"}))
        self.assertEqual(voice.attachment({"voice": {"file_id": "a", "duration": "nope"}})["seconds"], 0)


class QuickQuestionTests(unittest.TestCase):
    def test_the_status_and_spend_questions_are_recognised_typed_or_spoken(self):
        for text in ("what's running?", "What is running", "whats running right now", "what's running now?", "Is anything running?",
                     "what's going on", "what are you working on"):
            self.assertEqual(voice.quick_kind(text), "running", text)
        for text in ("how much today?", "How much did I spend today", "how much have we spent today?", "what did it cost today",
                     "what's the spend today", "How much today."):
            self.assertEqual(voice.quick_kind(text), "usage", text)

    def test_anything_longer_or_different_goes_to_the_model(self):
        for text in ("what's running on port 8080 and can you restart it please", "how much is a raspberry pi",
                     "write a task that checks what's running", "how much would it cost to run this every hour for a month",
                     "", "status"):
            self.assertIsNone(voice.quick_kind(text), text)


class SummaryTests(unittest.TestCase):
    def test_only_the_first_sentence_is_spoken(self):
        text = "I queued the README task. It will run after your thumbs-up. Then I will report back."
        self.assertEqual(voice.spoken_summary(text), "I queued the README task.")

    def test_markdown_links_code_and_emoji_do_not_reach_the_speaker(self):
        text = "✅ **Done**: see [the PR](https://github.com/x/y/pull/3) and `foo.py`.\n\n- more"
        said = voice.spoken_summary(text)
        self.assertEqual(said, "Done: see the PR and foo.py.")
        self.assertNotIn("http", voice.spoken_summary("Open https://example.com/a/b now."))

    def test_a_long_first_sentence_is_cut_at_a_word_and_a_code_block_is_skipped(self):
        said = voice.spoken_summary("word " * 100, limit=40)
        self.assertLessEqual(len(said), 40)
        self.assertTrue(said.endswith("word"))
        self.assertEqual(voice.spoken_summary("```\nrm -rf /\n```\nAll set."), "All set.")

    def test_nothing_speakable_gives_none(self):
        for text in ("", "   ", "```\ncode only\n```", "🙂🙂", "https://example.com"):
            self.assertIsNone(voice.spoken_summary(text), text)

    def test_a_secret_in_the_reply_is_not_spoken_aloud(self):
        said = voice.spoken_summary("The key is sk-ant-oat01-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ok.", redact=lambda t: t.replace("sk-ant-oat01-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "[redacted]"))
        self.assertNotIn("sk-ant", said)


class TranscriptTests(unittest.TestCase):
    def test_whisper_output_is_one_clean_line(self):
        raw = "  turn on the\n porch light \n[BLANK_AUDIO]\n(music)\n"
        self.assertEqual(voice.clean_transcript(raw), "turn on the porch light")

    def test_silence_markers_alone_are_empty(self):
        self.assertEqual(voice.clean_transcript("[BLANK_AUDIO]\n"), "")
        self.assertEqual(voice.clean_transcript("[ Silence ]"), "")
        self.assertEqual(voice.clean_transcript(""), "")

    def test_the_echo_line_quotes_what_was_heard(self):
        self.assertEqual(voice.echo("hello there"), "🎙 Heard: “hello there”")


class CommandTests(unittest.TestCase):
    def test_each_tool_gets_an_argument_list_and_no_shell(self):
        self.assertEqual(voice.to_wav_cmd("ffmpeg", "in.oga", "out.wav"),
                         ["ffmpeg", "-y", "-loglevel", "error", "-i", "in.oga", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "out.wav"])
        self.assertEqual(voice.transcribe_cmd("whisper-cli", "m.bin", "x.wav"),
                         ["whisper-cli", "-m", "m.bin", "-f", "x.wav", "-nt", "-np", "-l", "en"])
        self.assertEqual(voice.speak_cmd("piper", "v.onnx", "s.wav"), ["piper", "--model", "v.onnx", "--output_file", "s.wav"])
        self.assertEqual(voice.to_opus_cmd("ffmpeg", "s.wav", "s.ogg")[-1], "s.ogg")
        self.assertIn("libopus", voice.to_opus_cmd("ffmpeg", "s.wav", "s.ogg"))


class SweepTests(unittest.TestCase):
    def make(self, folder, name, hours_old):
        path = Path(folder) / name
        path.write_bytes(b"x")
        stamp = time.time() - hours_old * 3600
        os.utime(path, (stamp, stamp))
        return path

    def test_audio_older_than_the_retention_is_deleted_and_newer_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            old = self.make(folder, "voice-1.oga", 25)
            fresh = self.make(folder, "voice-2.oga", 23)
            self.assertEqual(voice.sweep(Path(folder), 24), 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())

    def test_a_retention_of_zero_deletes_everything_and_a_missing_folder_is_fine(self):
        with tempfile.TemporaryDirectory() as folder:
            self.make(folder, "a.oga", 0)
            self.assertEqual(voice.sweep(Path(folder), 0), 1)
            self.assertEqual(voice.sweep(Path(folder) / "nowhere", 24), 0)

    def test_only_files_in_the_folder_are_touched(self):
        with tempfile.TemporaryDirectory() as folder:
            sub = Path(folder) / "keep"
            sub.mkdir()
            (sub / "inner.txt").write_text("x")
            os.utime(sub, (0, 0))
            voice.sweep(Path(folder), 1)
            self.assertTrue((sub / "inner.txt").exists())


if __name__ == "__main__":
    unittest.main()
