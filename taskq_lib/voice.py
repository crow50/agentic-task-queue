"""Voice (Phase 10): what is a voice message, questions answered with no model, the line read aloud, tool command lines.

Nothing here runs a program or reads a setting. The bridge builds the argument lists below and runs them with
no shell, so a file name can never become a command.
"""

import re
import time
from pathlib import Path

# whisper.cpp prints these for sound that is not speech; they are not part of what was said.
NOISE = re.compile(r"[\[(]\s*(blank[_ ]audio|silence|music|noise|inaudible|applause|laughter)\s*[\])]", re.I)

QUICK = {
    "running": [
        r"(what is|whats) (currently |actually )?(running|going on|active|in progress|happening)( right now| at the moment| now)?",
        r"(is )?(anything|something) (currently )?(running|in progress|active)( right now| now)?",
        r"what are you (currently |now )?(working on|doing)( right now| now)?",
    ],
    "usage": [
        r"how much (today|so far today|(did|have|has) (i|we|it) (spend|spent|cost|used) (so far )?today)",
        r"what (did it|has it|have i|have we) (cost|spent|spend) today",
        r"(whats|what is) the (spend|cost|usage) (so far )?today",
    ],
}
MAX_QUICK_WORDS = 10


def attachment(msg):
    """{file_id, seconds, kind} for a voice note or audio file in a Telegram message, or None."""
    for kind in ("voice", "audio"):
        item = msg.get(kind)
        if not isinstance(item, dict) or not item.get("file_id"):
            continue
        if kind == "audio" and not str(item.get("mime_type") or "audio/").startswith("audio/"):
            continue
        try:
            seconds = int(item.get("duration") or 0)
        except (TypeError, ValueError):
            seconds = 0
        return {"file_id": str(item["file_id"]), "seconds": seconds, "kind": kind}
    return None


def quick_kind(text):
    """'running' or 'usage' when `text` is one of the short questions answered from the state files, else None."""
    words = re.sub(r"[^a-z0-9 ]", "", " ".join(text.lower().replace("'", "").replace("’", "").split()))
    if not words or len(words.split()) > MAX_QUICK_WORDS:
        return None
    for kind, patterns in QUICK.items():
        if any(re.fullmatch(pattern, words) for pattern in patterns):
            return kind
    return None


def clean_transcript(raw):
    """whisper.cpp's output as one line of what was said: sound markers dropped, whitespace joined."""
    return " ".join(NOISE.sub(" ", raw or "").split())


def echo(transcript):
    """The line that opens a reply to a voice note, so a mishearing is obvious."""
    return f"🎙 Heard: “{transcript}”"


def spoken_summary(text, limit=200, redact=None):
    """The one sentence of a reply that is read aloud, or None when nothing in it can be spoken.

    Code blocks, links and symbols are dropped first. A long report is never read: only its first sentence, cut at a word
    if it is longer than `limit`. `redact` (a function) runs first so a key in the reply is never said.
    """
    text = redact(text) if redact else text
    text = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)  # fenced code
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*_`#>~|]", "", text)
    text = re.sub(r"^\s*([-•]|\d+[.)])\s+", "", text, flags=re.M)
    text = re.sub(r"[^\w\s.,;:!?'\"()%$/&@+=-]", "", text)  # emoji and other symbols
    paragraph = next((p for p in re.split(r"\n\s*\n", text) if p.strip()), "")
    paragraph = " ".join(paragraph.split())
    sentence = re.split(r"(?<=[.!?])\s+", paragraph, maxsplit=1)[0].strip()
    if len(sentence) > limit:
        sentence = sentence[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return sentence if re.search(r"\w", sentence) else None


# ---------------------------------------------------------------- the tools

def to_wav_cmd(ffmpeg, source, target):
    """ffmpeg: any audio to the 16 kHz mono wav whisper.cpp reads."""
    return [ffmpeg, "-y", "-loglevel", "error", "-i", str(source), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(target)]


def transcribe_cmd(whisper, model, wav):
    """whisper.cpp: plain text on stdout, no timestamps, no progress."""
    return [whisper, "-m", str(model), "-f", str(wav), "-nt", "-np", "-l", "en"]


def speak_cmd(piper, model, wav):
    """piper: the text to say on stdin, a wav in `wav`."""
    return [piper, "--model", str(model), "--output_file", str(wav)]


def to_opus_cmd(ffmpeg, source, target):
    """ffmpeg: a wav to the Opus-in-Ogg that Telegram plays as a voice message."""
    return [ffmpeg, "-y", "-loglevel", "error", "-i", str(source), "-c:a", "libopus", "-b:a", "24k", "-application", "voip", str(target)]


# ---------------------------------------------------------------- housekeeping

def sweep(folder, hours, now=None):
    """Delete the files in `folder` last changed more than `hours` ago. Returns how many went. A missing folder is fine."""
    cutoff = (now if now is not None else time.time()) - hours * 3600
    removed = 0
    try:
        files = [p for p in Path(folder).iterdir() if p.is_file()]
    except OSError:
        return 0
    for path in files:
        try:
            if path.stat().st_mtime <= cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass  # gone already, or not ours to remove: the next sweep tries again
    return removed
