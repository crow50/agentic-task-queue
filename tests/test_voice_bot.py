"""Phase 10: voice notes in, spoken replies out, and answers that feel quick.

The bridge is driven the way test_limits_coordinator drives it. ffmpeg, whisper.cpp and piper are doubles
(tests/fake_voice.py) that record every call; the fake Telegram serves getFile, the download and sendVoice.
"""

import html
import json
import os
import shutil
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from test_limits import iso  # noqa: E402
from test_limits_coordinator import CHAT, USER, BotCase, message  # noqa: E402

TESTS = Path(__file__).resolve().parent
UTC = timezone.utc
TOOLS = ("ffmpeg", "whisper-cli", "piper")


def voice_msg(seconds=5, sender=CHAT, chat=CHAT, message_id=7, kind="voice", file_id="f1", reply_to=None):
    msg = {"message_id": message_id, "chat": {"id": chat}, "from": {"id": sender},
           kind: {"file_id": file_id, "duration": seconds, "mime_type": "audio/ogg"}}
    if reply_to:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 1, "message": msg}


class VoiceCase(BotCase):
    def setUp(self):
        super().setUp()
        self.bin = self.sb.base / "bin"
        self.bin.mkdir()
        for name in TOOLS:
            shutil.copy2(TESTS / "fake_voice.py", self.bin / name)
        self.sb.configure(
            FFMPEG_BIN=str(self.bin / "ffmpeg"), WHISPER_BIN=str(self.bin / "whisper-cli"),
            WHISPER_MODEL=str(self.bin / "ggml-base.en.bin"), TTS_BIN=str(self.bin / "piper"),
            TTS_MODEL=str(self.bin / "voice.onnx"),
        )
        self.say("turn on the porch light")
        self.sb.set_scenario(other=[{"result": "Queued it. It will run after your thumbs-up."}])

    def say(self, text, tool="whisper-cli", **extra):
        (self.bin / f"{tool}.json").write_text(json.dumps({"text": text, **extra}))

    def configure_tool(self, tool, **config):
        (self.bin / f"{tool}.json").write_text(json.dumps(config))

    def tool_calls(self, tool):
        path = self.bin / f"{tool}.calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def edit_texts(self):
        return [html.unescape(r.params["text"]) for r in self.sb.telegram.edits()]

    def final_edit(self):
        return self.edit_texts()[-1]

    def audio_files(self):
        folder = self.sb.base / "state" / "voice"
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


class VoiceInTests(VoiceCase):
    def test_a_voice_note_becomes_the_same_call_as_typed_text_marked_voice(self):
        self.handle(voice_msg())
        self.handle(message("turn on the porch light"))
        spoken, typed = self.others()
        self.assertEqual(spoken["prompt"], "[voice] turn on the porch light")
        self.assertEqual(typed["prompt"], "turn on the porch light")
        for key in ("model", "allowed_tools"):
            self.assertEqual(spoken[key], typed[key])

    def test_the_reply_begins_with_what_was_heard(self):
        self.handle(voice_msg())
        self.assertTrue(self.final_edit().startswith("🎙 Heard: “turn on the porch light”"))
        self.assertIn("Queued it.", self.final_edit())

    def test_the_audio_goes_through_ffmpeg_and_whisper_as_argument_lists(self):
        self.handle(voice_msg())
        (to_wav,) = self.tool_calls("ffmpeg")[:1]
        self.assertIn("16000", to_wav["argv"])
        (heard,) = self.tool_calls("whisper-cli")
        self.assertEqual(heard["argv"][:2], ["-m", str(self.bin / "ggml-base.en.bin")])
        self.assertIn("-nt", heard["argv"])
        self.assertEqual(self.sb.telegram.calls("getFile")[0].params["file_id"], "f1")

    def test_an_audio_file_is_read_like_a_voice_note(self):
        self.handle(voice_msg(kind="audio"))
        self.assertEqual(self.others()[0]["prompt"], "[voice] turn on the porch light")

    def test_a_voice_note_from_the_wrong_sender_is_ignored_and_never_downloaded(self):
        self.sb.configure(TELEGRAM_USER_ID=str(USER))
        self.handle(voice_msg(sender=999))
        self.handle(voice_msg(sender=USER, chat=-100999))
        self.assertEqual(self.sb.telegram.requests, [])
        self.assertEqual(self.tool_calls("whisper-cli"), [])
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertEqual(self.audio_files(), [])

    def test_a_note_longer_than_the_limit_is_refused_before_it_is_downloaded(self):
        self.sb.configure(VOICE_MAX_SECONDS="60")
        self.handle(voice_msg(seconds=61))
        self.assertEqual(self.sb.telegram.calls("getFile"), [])
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertIn("60 seconds", " ".join(self.sent() + self.edit_texts()))

    def test_without_a_whisper_binary_it_says_voice_is_not_set_up(self):
        self.sb.configure(WHISPER_BIN=None)
        self.handle(voice_msg())
        self.assertEqual(self.sb.telegram.calls("getFile"), [])
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertIn("WHISPER_BIN", " ".join(self.sent()))

    def test_a_failed_or_empty_transcription_says_so_and_calls_no_model(self):
        self.configure_tool("whisper-cli", exit=1, stderr="model not found")
        self.handle(voice_msg())
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertIn("couldn't", self.final_edit())
        self.say("[BLANK_AUDIO]")
        self.handle(voice_msg())
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertIn("couldn't make out", self.final_edit())

    def test_a_failed_download_is_reported_not_raised(self):
        self.sb.telegram.queue_response("getFile", 400)
        self.handle(voice_msg())
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertIn("couldn't download", self.final_edit())

    def test_a_spoken_secret_is_deleted_and_never_reaches_the_model_or_the_state(self):
        token = "51c060a0971562172688f32821fc635fab8c9654f0000c33"
        self.say(f"here is the token {token}")
        state = self.handle(voice_msg(message_id=7))
        self.assertEqual(self.sb.claude_calls(), [])
        deleted = {r.params["message_id"] for r in self.sb.telegram.calls("deleteMessage")}
        self.assertIn("7", deleted)  # the voice note itself
        self.assertGreaterEqual(len(deleted), 2)  # and the acknowledgement
        self.assertNotIn(token, json.dumps(state))
        self.assertNotIn(token, " ".join(self.sent() + self.edit_texts()))
        self.assertEqual(self.audio_files(), [])


class RetentionTests(VoiceCase):
    def test_audio_is_kept_for_the_retention_then_deleted(self):
        self.sb.configure(VOICE_RETENTION_HOURS="24")
        self.handle(voice_msg())
        kept = self.audio_files()
        self.assertTrue(kept)
        for name in kept:
            stamp = time.time() - 25 * 3600
            os.utime(self.sb.base / "state" / "voice" / name, (stamp, stamp))
        self.drive("bot.sweep_voice()")
        self.assertEqual(self.audio_files(), [])

    def test_a_note_newer_than_the_retention_survives_the_sweep(self):
        self.handle(voice_msg())
        before = self.audio_files()
        self.drive("bot.sweep_voice()")
        self.assertEqual(self.audio_files(), before)

    def test_a_retention_of_zero_keeps_no_audio(self):
        self.sb.configure(VOICE_RETENTION_HOURS="0")
        self.handle(voice_msg())
        self.assertEqual(self.audio_files(), [])

    def test_the_main_loop_sweeps(self):
        source = (REPO / "coordinator_bot.py").read_text()
        self.assertIn("sweep_voice()", source.split("def main():")[1])


class SpokenReplyTests(VoiceCase):
    def test_a_text_message_gets_no_audio_back(self):
        self.handle(message("turn on the porch light"))
        self.assertEqual(self.sb.telegram.voices(), [])
        self.assertEqual(self.tool_calls("piper"), [])

    def test_a_voice_note_gets_a_one_sentence_spoken_summary_and_the_full_text(self):
        self.handle(voice_msg())
        (sent,) = self.sb.telegram.voices()
        self.assertIn(b"OggS-fake-opus", sent.body)
        self.assertIn(b'name="voice"', sent.body)
        self.assertIn(b"4242", sent.body)  # the chat
        (spoke,) = self.tool_calls("piper")
        self.assertEqual(spoke["stdin"].strip(), "Queued it.")  # not the whole reply
        self.assertIn("It will run after your thumbs-up.", self.final_edit())  # the text still has everything

    def test_voice_on_makes_typed_replies_spoken_and_off_stops_it(self):
        self.handle(message("/voice on"))
        self.handle(message("hello"))
        self.assertEqual(len(self.sb.telegram.voices()), 1)
        self.handle(message("/voice off"))
        self.handle(message("hello again"))
        self.assertEqual(len(self.sb.telegram.voices()), 1)

    def test_voice_with_no_argument_says_where_it_stands_and_calls_no_model(self):
        self.handle(message("/voice"))
        self.assertIn("off", " ".join(self.sent()).lower())
        self.handle(message("/voice on"))
        self.handle(message("/voice"))
        self.assertIn("on", self.sent()[-1].lower())
        self.assertEqual(self.sb.claude_calls(), [])

    def test_voice_on_without_a_speaker_is_refused_and_stays_off(self):
        self.sb.configure(TTS_BIN=None)
        state = self.handle(message("/voice on"))
        self.assertIn("TTS_BIN", " ".join(self.sent()))
        self.assertFalse(state.get("voice"))

    def test_a_speaker_that_fails_never_loses_the_text_reply(self):
        self.configure_tool("piper", exit=1, stderr="no voice model")
        self.handle(voice_msg())
        self.assertEqual(self.sb.telegram.voices(), [])
        self.assertIn("Queued it.", self.final_edit())

    def test_an_unconfigured_speaker_just_means_no_audio(self):
        self.sb.configure(TTS_BIN=None)
        self.handle(voice_msg())
        self.assertEqual(self.sb.telegram.voices(), [])
        self.assertIn("Queued it.", self.final_edit())

    def test_a_reply_with_nothing_to_say_aloud_sends_no_audio(self):
        self.sb.set_scenario(other=[{"result": "```\ncode only\n```"}])
        self.handle(voice_msg())
        self.assertEqual(self.sb.telegram.voices(), [])

    def test_the_spoken_files_are_not_left_behind(self):
        self.sb.configure(VOICE_RETENTION_HOURS="0")
        self.handle(voice_msg())
        self.assertEqual(self.audio_files(), [])


class QuickTests(VoiceCase):
    def usage_today(self, cost=1.5):
        self.sb.write_usage([{"time": iso(datetime.now(UTC)), "label": "worker", "model": "m", "exit": 0, "status": "ok",
                              "is_error": False, "usage": {}, "modelUsage": {}, "total_cost_usd": cost, "cost_usd": cost,
                              "task": "t"}])

    def test_a_status_question_makes_no_model_call(self):
        self.sb.add_task("build-the-thing", state="active")
        self.handle(message("what's running?"))
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertIn("build-the-thing", " ".join(self.sent()))

    def test_nothing_running_is_said_plainly(self):
        self.handle(message("What is running"))
        self.assertIn("Nothing is running", " ".join(self.sent()))

    def test_a_spend_question_reads_usage_and_makes_no_model_call(self):
        self.usage_today(1.5)
        self.handle(message("how much today?"))
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertIn("$1.50", " ".join(self.sent()))

    def test_a_spoken_status_question_is_answered_and_spoken_with_no_model_call(self):
        self.say("what's running?")
        self.handle(voice_msg())
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertTrue(self.final_edit().startswith("🎙 Heard: “what's running?”"))
        self.assertIn("Nothing is running", self.final_edit())
        self.assertEqual(self.tool_calls("piper")[0]["stdin"].strip(), "Nothing is running.")

    def test_a_longer_question_still_goes_to_the_model(self):
        self.handle(message("what's running on port 8080 and can you restart it"))
        self.assertEqual(len(self.others()), 1)


class AcknowledgementTests(VoiceCase):
    def test_the_acknowledgement_is_sent_before_the_model_is_called(self):
        self.handle(voice_msg())
        ack = self.sb.telegram.calls("sendMessage")[0]
        self.assertIn("Listening", ack.params["text"])
        self.assertLess(ack.at, self.others()[0]["at"])

    def test_the_acknowledgement_is_edited_into_the_answer_not_followed_by_a_new_message(self):
        self.handle(voice_msg())
        sent = self.sb.telegram.calls("sendMessage")
        self.assertEqual(len(sent), 1)
        ack_id = "101"
        self.assertTrue(all(r.params["message_id"] == ack_id for r in self.sb.telegram.edits()))
        self.assertIn("Queued it.", self.final_edit())

    def test_a_reply_to_the_answer_can_be_traced_to_what_it_said(self):
        state = self.handle(voice_msg())
        self.assertIn(101, [mid for mid, _ in state["sent_messages"]])

    def test_a_long_answer_leaves_the_heard_line_in_place_and_comes_as_a_preview_and_file(self):
        self.sb.set_scenario(other=[{"result": "Here is the report. " + "word " * 1200}])
        self.handle(voice_msg())
        self.assertEqual(self.final_edit(), "🎙 Heard: “turn on the porch light”")
        self.assertEqual(len(self.sb.telegram.calls("sendDocument")), 1)

    def test_while_the_queue_is_paused_the_note_is_kept_and_answered_later(self):
        self.sb.write_pause(self.future())
        state = self.handle(voice_msg())
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertEqual([h["text"] for h in state["held"]], ["[voice] turn on the porch light"])
        self.assertTrue(self.final_edit().startswith("🎙 Heard:"))
        self.assertIn("paused", " ".join(self.sent()).lower())

    def test_typed_messages_keep_the_eyes_reaction_and_no_extra_message(self):
        self.handle(message("hello"))
        self.assertEqual(len(self.sb.telegram.calls("sendMessage")), 1)
        self.assertEqual(self.sb.telegram.calls("setMessageReaction")[0].params["message_id"], "5")


class CommandListTests(unittest.TestCase):
    def test_voice_is_in_the_command_menu(self):
        self.assertIn('"command": "voice"', (REPO / "coordinator_bot.py").read_text())


if __name__ == "__main__":
    unittest.main()
