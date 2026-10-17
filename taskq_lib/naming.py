"""Give the assistant a name: validate it, and write the patch that records it in the repo.

The name lives in three places: ASSISTANT_NAME in `.env` (what messages use, set on the box), its default in
`.env.example`, and a line in `coordinator/identity.md` (how the coordinator introduces itself). The patch touches only
the last two, which are tracked; `.env` is never part of a diff.
"""

import difflib
import re

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,23}$")
IDENTITY_PATH = "coordinator/identity.md"
ENV_EXAMPLE_PATH = ".env.example"
ENV_LINE = re.compile(r"^ASSISTANT_NAME=.*$", re.M)
IDENTITY_LINE = re.compile(r"^My name is \*\*.*?\*\*\.$", re.M)


def clean(name):
    """The name stripped of surrounding space and quotes, or None when it is not a plain short name."""
    name = (name or "").strip().strip("\"'`*").strip()
    return name if NAME_RE.match(name) else None


def _diff(path, before, after):
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        f"a/{path}", f"b/{path}"))


def name_patch(identity_text, env_example_text, name):
    """A unified diff that sets the name in identity.md and in the .env.example default; "" when nothing changes.

    Raises ValueError for a name that is not plain, or a file that has no line to change.
    """
    name = clean(name)
    if name is None:
        raise ValueError("a name is letters, digits, spaces, '_' or '-', starting with a letter, up to 24 characters")
    if not IDENTITY_LINE.search(identity_text):
        raise ValueError(f"{IDENTITY_PATH} has no 'My name is **...**.' line")
    if not ENV_LINE.search(env_example_text):
        raise ValueError(f"{ENV_EXAMPLE_PATH} has no ASSISTANT_NAME= line")
    identity = IDENTITY_LINE.sub(lambda _: f"My name is **{name}**.", identity_text, count=1)
    env_example = ENV_LINE.sub(lambda _: f"ASSISTANT_NAME={name}", env_example_text, count=1)
    return _diff(IDENTITY_PATH, identity_text, identity) + _diff(ENV_EXAMPLE_PATH, env_example_text, env_example)


def parse_proposals(text):
    """Up to three (name, reason) pairs from lines like `Juno | calm and short`; anything else is ignored."""
    found = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line)
        name, sep, reason = line.partition("|")
        if sep and (name := clean(name)) and name.lower() not in {n.lower() for n, _ in found}:
            found.append((name, reason.strip()))
    return found[:3]
