#!/usr/bin/env python3
"""How fast is voice on this host? Run it on the Pi with a real voice note before choosing a whisper model.

    python3 scripts/voice_bench.py note.oga                 transcribe a voice note three times
    python3 scripts/voice_bench.py note.oga --runs 5
    python3 scripts/voice_bench.py note.oga --say "Queued it."   also time a spoken sentence

Reads WHISPER_BIN, WHISPER_MODEL, FFMPEG_BIN, TTS_BIN and TTS_MODEL from .env (flags of the same name in lower case
override). Prints the real-time factor (seconds of work per second of audio): under 0.5 is comfortable, over 1 means a
note takes longer to read than to say and a smaller or quantized model is the next thing to try.
"""

import argparse
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dispatcher  # noqa: E402
from taskq_lib import voice  # noqa: E402

OPTIONS = ("whisper_bin", "whisper_model", "ffmpeg_bin", "tts_bin", "tts_model")


def timed(cmd, stdin=None):
    started = time.monotonic()
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"{Path(cmd[0]).name} failed ({proc.returncode}): {proc.stderr.strip()[-300:]}")
    return time.monotonic() - started, proc.stdout


def audio_seconds(wav):
    with wave.open(str(wav), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


def verdict(factor):
    if factor <= 0.5:
        return "comfortably faster than real time: keep this model"
    if factor <= 1.0:
        return "about real time: fine for short notes; try a quantized or smaller model if notes run long"
    return "slower than real time: switch to tiny.en or a quantized base model"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("audio", type=Path, help="a voice note (.oga, .ogg, .mp3 ...)")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--say", help="also time speaking this sentence")
    for name in OPTIONS:
        parser.add_argument(f"--{name.replace('_', '-')}")
    args = parser.parse_args(argv)
    setting = {name: getattr(args, name) or dispatcher.cfg(name.upper()) for name in OPTIONS}
    ffmpeg = setting["ffmpeg_bin"] or "ffmpeg"
    if not args.audio.is_file():
        raise SystemExit(f"{args.audio} is not a file")
    if not setting["whisper_bin"] or not setting["whisper_model"]:
        raise SystemExit("set WHISPER_BIN and WHISPER_MODEL in .env (or pass --whisper-bin and --whisper-model)")

    with tempfile.TemporaryDirectory(prefix="taskq-voice-") as folder:
        wav = Path(folder) / "note.wav"
        convert, _ = timed(voice.to_wav_cmd(ffmpeg, args.audio, wav))
        seconds = audio_seconds(wav)
        runs, said = [], ""
        for _ in range(max(1, args.runs)):
            took, said = timed(voice.transcribe_cmd(setting["whisper_bin"], setting["whisper_model"], wav))
            runs.append(took)
        median = statistics.median(runs)
        factor = median / seconds if seconds else float("inf")
        print(f"audio: {seconds:.1f} s (ffmpeg took {convert:.2f} s)")
        print(f"transcription, {len(runs)} run{'s' if len(runs) != 1 else ''}: " + ", ".join(f"{t:.1f} s" for t in runs))
        print(f"real-time factor: {factor:.2f} with {Path(setting['whisper_model']).name}")
        print(f"verdict: {verdict(factor)}")
        print(f"heard: {voice.clean_transcript(said)}")
        if args.say:
            if not setting["tts_bin"] or not setting["tts_model"]:
                raise SystemExit("set TTS_BIN and TTS_MODEL in .env (or pass --tts-bin and --tts-model) to time speech")
            speech, _ = timed(voice.speak_cmd(setting["tts_bin"], setting["tts_model"], Path(folder) / "say.wav"), stdin=args.say)
            opus, _ = timed(voice.to_opus_cmd(ffmpeg, Path(folder) / "say.wav", Path(folder) / "say.ogg"))
            print(f"speech: {speech:.1f} s to say {len(args.say.split())} words, {opus:.2f} s to convert to Opus")
    return 0


if __name__ == "__main__":
    sys.exit(main())
