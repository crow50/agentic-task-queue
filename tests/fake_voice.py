#!/usr/bin/env python3
"""Stand-in for ffmpeg, whisper.cpp's whisper-cli and piper, used as FFMPEG_BIN, WHISPER_BIN and TTS_BIN in tests.

One script, copied to a file named `ffmpeg`, `whisper-cli` or `piper`: the file name picks the behaviour. Settings come from
`<that file>.json` next to it (a test environment passes nothing through):

    {"exit": 0, "stderr": "", "text": "turn on the light", "sleep": 0}

ffmpeg writes a stub for its last argument (RIFF for .wav, OggS for .ogg). whisper-cli prints "text". piper reads the text to
speak on stdin and writes a stub to --output_file. Every call is appended to `<that file>.calls.jsonl` with argv and, for
piper, the text it was given.
"""

import json
import sys
import time
import wave
from pathlib import Path


def main(argv):
    me = Path(sys.argv[0]).resolve()
    config_path = Path(f"{me}.json")
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    stdin = sys.stdin.read() if me.name == "piper" else ""
    with open(f"{me}.calls.jsonl", "a") as fh:
        fh.write(json.dumps({"argv": argv, "stdin": stdin, "at": time.monotonic()}) + "\n")
    time.sleep(float(config.get("sleep", 0)))
    if config.get("exit"):
        print(config.get("stderr", "boom"), file=sys.stderr)
        return int(config["exit"])
    if me.name == "ffmpeg":
        out = Path(argv[-1])
        if out.suffix == ".wav":  # two seconds of silence, so a caller can measure the length
            with wave.open(str(out), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\0\0" * 32000)
        else:
            out.write_bytes(b"OggS-fake-opus")
    elif me.name == "whisper-cli":
        print(config.get("text", "turn on the light"))
    elif me.name == "piper":
        Path(argv[argv.index("--output_file") + 1]).write_bytes(b"RIFFfake-speech")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
