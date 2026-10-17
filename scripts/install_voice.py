#!/usr/bin/env python3
"""Install the voice tools (Phase 10): whisper.cpp with a model, Piper with a voice, then prove they work.

    python3 scripts/install_voice.py                          install under /opt/voice, test, set .env or print the lines
    python3 scripts/install_voice.py --model tiny.en-q5_1     a smaller whisper model (faster, less accurate)
    python3 scripts/install_voice.py --dest ~/voice           somewhere else (the queue's user must be able to reach it)
    python3 scripts/install_voice.py --check-only             install nothing, only test what is there

Run it as your own user, not root and not with sudo: it downloads and builds code, and the queue's `taskq` user only has to
read and run the result. The one thing it cannot do is `apt install`: if a system package is missing it prints the single
command and stops. Every step is skipped when it is already done, so after a failure fix the cause and run it again.

The test says a sentence with Piper, listens to it with whisper.cpp and encodes it to Opus, so it needs no recording. It
also checks that the `taskq` user can reach every file (a home directory or a umask of 077 would hide them).
"""

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from taskq_lib import voice  # noqa: E402

DEFAULT_DEST = "/opt/voice"
DEFAULT_MODEL = "base.en-q5_1"
DEFAULT_VOICE = "en_US-lessac-medium"
QUEUE_ENV = Path("/opt/agentic-task-queue/.env")
SENTENCE = "The queue is ready."
HEARD = "ready"  # what has to appear in the transcript of SENTENCE
PACKAGES = {"ffmpeg": "ffmpeg", "git": "git", "cmake": "cmake", "a C++ compiler": "build-essential",
            "python3 venv": "python3-venv"}


class Layout:
    """Where everything goes, from one destination folder."""

    def __init__(self, dest, model=DEFAULT_MODEL, speaker=DEFAULT_VOICE):
        self.dest = Path(dest)
        self.model_name = model
        self.voice_name = speaker
        self.whisper_dir = self.dest / "whisper.cpp"
        self.whisper_bin = self.whisper_dir / "build" / "bin" / "whisper-cli"
        self.whisper_model = self.whisper_dir / "models" / f"ggml-{model}.bin"
        self.venv = self.dest / "piper"
        self.tts_bin = self.venv / "bin" / "piper"
        self.tts_model = self.dest / f"{speaker}.onnx"

    def env(self):
        return {"WHISPER_BIN": self.whisper_bin, "WHISPER_MODEL": self.whisper_model,
                "TTS_BIN": self.tts_bin, "TTS_MODEL": self.tts_model}


def missing_packages(which=shutil.which, run=subprocess.run):
    """The apt packages this host lacks for the steps below, in a stable order."""
    lacking = []
    if not which("ffmpeg"):
        lacking.append("ffmpeg")
    if not which("git"):
        lacking.append("git")
    if not which("cmake"):
        lacking.append("cmake")
    if not any(which(name) for name in ("c++", "g++")):
        lacking.append("a C++ compiler")
    venv = run([sys.executable, "-c", "import ensurepip, venv"], capture_output=True)
    if venv.returncode != 0:
        lacking.append("python3 venv")
    return lacking


def steps(layout, jobs):
    """(name, already done?, command, folder) in the order they run."""
    wh = layout.whisper_dir
    voice_files = (layout.tts_model, Path(f"{layout.tts_model}.json"))
    return [
        ("fetch whisper.cpp", lambda: (wh / ".git").exists(),
         ["git", "clone", "--depth", "1", "https://github.com/ggml-org/whisper.cpp", str(wh)], layout.dest),
        (f"download the {layout.model_name} model", lambda: layout.whisper_model.is_file(),
         ["sh", "./models/download-ggml-model.sh", layout.model_name], wh),
        ("configure whisper.cpp", lambda: (wh / "build" / "CMakeCache.txt").exists() or layout.whisper_bin.exists(),
         ["cmake", "-B", "build"], wh),
        ("build whisper.cpp (a few minutes)", lambda: layout.whisper_bin.exists(),
         ["cmake", "--build", "build", f"-j{jobs}", "--config", "Release"], wh),
        ("create the Piper environment", lambda: (layout.venv / "bin" / "python").exists(),
         [sys.executable, "-m", "venv", str(layout.venv)], layout.dest),
        ("install Piper", lambda: layout.tts_bin.exists(),
         [str(layout.venv / "bin" / "pip"), "install", "piper-tts"], layout.dest),
        (f"download the {layout.voice_name} voice", lambda: all(path.is_file() for path in voice_files),
         [str(layout.venv / "bin" / "python"), "-m", "piper.download_voices", layout.voice_name, "--data-dir", str(layout.dest)],
         layout.dest),
    ]


def reachable(path, need_exec=False):
    """Can a user who is neither the owner nor in the group use `path`? Every folder above it must allow entering."""
    problems = []
    path = Path(path).resolve()
    if not path.exists():
        return [f"{path} does not exist"]
    mode = path.stat().st_mode
    if not mode & stat.S_IROTH:
        problems.append(f"{path} is not readable by other users")
    if need_exec and not mode & stat.S_IXOTH:
        problems.append(f"{path} is not executable by other users")
    for parent in path.parents:
        if not parent.stat().st_mode & stat.S_IXOTH:
            problems.append(f"{parent} cannot be entered by other users (chmod o+x)")
    return problems


def permission_problems(layout):
    problems = []
    for path, need_exec in ((layout.whisper_bin, True), (layout.whisper_model, False), (layout.tts_bin, True), (layout.tts_model, False)):
        problems += reachable(path, need_exec)
    return list(dict.fromkeys(problems))  # the same folder is named once


def capture(cmd, stdin=None):
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=600)
    return proc.returncode, proc.stdout, proc.stderr


def selftest(layout, ffmpeg="ffmpeg", run=capture):
    """Say SENTENCE, hear it back, encode it: [] when all works, else one line per problem."""
    problems = permission_problems(layout)
    with tempfile.TemporaryDirectory(prefix="taskq-voice-") as folder:
        spoken, wav, opus = (Path(folder) / name for name in ("say.wav", "heard.wav", "say.ogg"))
        code, _, err = run(voice.speak_cmd(layout.tts_bin, layout.tts_model, spoken), SENTENCE)
        if code or not spoken.is_file():
            return problems + [f"Piper could not speak: {err.strip()[-300:] or 'no audio written'}"]
        code, _, err = run(voice.to_wav_cmd(ffmpeg, spoken, wav))
        if code:
            return problems + [f"ffmpeg could not convert the speech to wav: {err.strip()[-300:]}"]
        code, out, err = run(voice.transcribe_cmd(layout.whisper_bin, layout.whisper_model, wav))
        if code:
            return problems + [f"whisper.cpp could not transcribe: {err.strip()[-300:]}"]
        heard = voice.clean_transcript(out)
        if HEARD not in heard.lower():
            problems.append(f"whisper.cpp heard {heard!r} for {SENTENCE!r} (the model or the voice may be wrong)")
        code, _, err = run(voice.to_opus_cmd(ffmpeg, spoken, opus))
        if code or not opus.is_file():
            problems.append(f"ffmpeg could not encode Opus (is libopus in this build? `ffmpeg -encoders | grep opus`): {err.strip()[-300:]}")
    return problems


def env_text(layout):
    return "".join(f"{key}={value}\n" for key, value in layout.env().items())


def merged_env(existing, updates):
    """`existing` .env text with each key in `updates` set once: replaced where present, appended where not."""
    lines = existing.splitlines()
    for key, value in updates.items():
        line = f"{key}={value}"
        if any(item.startswith(f"{key}=") for item in lines):
            lines = [line if item.startswith(f"{key}=") else item for item in lines]
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def finish(layout, env_path):
    """Write the four keys into `env_path` when this user may, else leave voice.env and say how to add it."""
    snippet = layout.dest / "voice.env"
    snippet.write_text(env_text(layout))
    if env_path.exists() and os.access(env_path, os.W_OK):
        env_path.write_text(merged_env(env_path.read_text(), layout.env()))
        print(f"\nSet WHISPER_BIN, WHISPER_MODEL, TTS_BIN and TTS_MODEL in {env_path}.")
    else:
        print(f"\nAdd these to the queue's .env ({env_path}), which you cannot write; they are also in {snippet}:\n")
        print(env_text(layout))
        print(f"As the queue's user:  sudo -u taskq tee -a {env_path} < {snippet} > /dev/null")
        print("(run that once: running it twice adds the lines twice, and the later ones win)")
    print("\nThen: sudo python3 /opt/agentic-task-queue/scripts/install.py --check   (the voice: lines)")
    print("      sudo systemctl restart claude-coordinator")


def main(argv=None, run=subprocess.run, which=shutil.which, test=capture):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dest", default=DEFAULT_DEST)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="a whisper.cpp ggml model name (default %(default)s)")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="a Piper voice name (default %(default)s)")
    parser.add_argument("--ffmpeg-bin", default="ffmpeg", help="ffmpeg to use for the test (default %(default)s)")
    parser.add_argument("--env", type=Path, help="the queue's .env (default /opt/agentic-task-queue/.env)")
    parser.add_argument("--check-only", action="store_true", help="install nothing; test what is already there")
    args = parser.parse_args(argv)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise SystemExit("Run this as your own user, not root: it downloads and builds code. It prints the one sudo command it needs.")
    layout = Layout(Path(args.dest).expanduser(), args.model, args.voice)
    env_path = args.env or (QUEUE_ENV if QUEUE_ENV.exists() else Path(__file__).resolve().parent.parent / ".env")

    if not args.check_only:
        lacking = missing_packages(which, run)
        if lacking:
            apt = " ".join(PACKAGES[name] for name in lacking)
            raise SystemExit(f"Missing: {', '.join(lacking)}.\nInstall them (this is the only step that needs sudo), then run this again:\n"
                             f"  sudo apt install {apt}")
        try:
            layout.dest.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise SystemExit(f"Cannot create {layout.dest}: {error}.\nMake it yours first:  sudo mkdir {layout.dest} && sudo chown $USER {layout.dest}")
        jobs = max(1, min(4, os.cpu_count() or 1))
        for name, done, cmd, folder in steps(layout, jobs):
            if done():
                print(f"[skip] {name}: already done")
                continue
            print(f"[run ] {name}")
            if run([str(part) for part in cmd], cwd=folder).returncode != 0:
                raise SystemExit(f"\nStep failed: {name}. Fix the cause shown above and run this again; finished steps are skipped.")
    missing = [path for path in (layout.whisper_bin, layout.whisper_model, layout.tts_bin, layout.tts_model) if not path.exists()]
    if missing:
        raise SystemExit("Not installed yet: " + ", ".join(str(path) for path in missing))

    print("\nTesting: speak a sentence, transcribe it, encode it to Opus ...")
    problems = selftest(layout, args.ffmpeg_bin, test)
    if problems:
        print("\nThe test found problems:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("Everything works: the tools run, whisper.cpp heard the sentence, and the queue's user can reach the files.")
    if not args.check_only:
        finish(layout, env_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
