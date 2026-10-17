"""Telegram inline buttons: callback data that always fits, and the keyboard JSON.

Telegram refuses callback_data over 64 bytes. A button's data is `act:<action>[:<arg>]`. When an argument
(a long task id, say) would not fit, it is replaced by `~<hash>` and the full text is kept in a table the caller
saves, so a tap can still be turned back into the task it was for.
"""

import hashlib
import json

MAX_CALLBACK_BYTES = 64
PREFIX = "act:"


def fits(data):
    return len(data.encode()) <= MAX_CALLBACK_BYTES


def encode(action, arg="", table=None):
    """The callback_data for an action. `table` (a dict the caller keeps) receives the full argument when it is shortened.

    Raises ValueError when even the action alone cannot fit, or the argument is long and no table was given.
    """
    if ":" in action:
        raise ValueError("an action name has no colon")
    data = f"{PREFIX}{action}" + (f":{arg}" if arg else "")
    if fits(data):
        return data
    if table is None:
        raise ValueError(f"callback data over {MAX_CALLBACK_BYTES} bytes: {data[:40]}...")
    key = "~" + hashlib.sha1(arg.encode()).hexdigest()[:12]
    short = f"{PREFIX}{action}:{key}"
    if not fits(short):
        raise ValueError(f"the action name {action} is too long")
    table[key] = arg
    return short


def decode(data, table=None):
    """(action, arg) from callback data, or None when it is not ours."""
    if not isinstance(data, str) or not data.startswith(PREFIX):
        return None
    action, _, arg = data[len(PREFIX):].partition(":")
    if not action:
        return None
    if arg.startswith("~"):
        arg = (table or {}).get(arg, "")
        if not arg:
            return None  # a button from before the table was trimmed: nothing safe to do
    return action, arg


def markup(rows, table=None):
    """The reply_markup JSON for rows of (label, action, arg) buttons. An empty list gives None."""
    keyboard = [[{"text": label, "callback_data": encode(action, arg, table)} for label, action, arg in row] for row in rows if row]
    return json.dumps({"inline_keyboard": keyboard}) if keyboard else None


def remembered(table, keep=200):
    """The table cut to its newest `keep` entries."""
    return dict(list(table.items())[-keep:])
