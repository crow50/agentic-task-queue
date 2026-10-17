"""When an alert may be sent, and how long a recurring job's schedule says it should go between runs."""

import re

PERIODS = {"m": 60, "h": 3600, "d": 86400}


def sent_today(state, key, today):
    """True when this condition was already sent on `today` (a local YYYY-MM-DD)."""
    return state.get(key) == today


def mark_sent(state, key, today):
    """The state with `key` sent on `today`, and every earlier day dropped (an alert is per day, so old ones mean nothing)."""
    return {**{k: v for k, v in state.items() if v == today}, key: today}


def schedule_period(spec):
    """Seconds between runs of a recurring schedule (see dispatcher.SCHEDULE_FORMS), or None if it is not one."""
    spec = (spec or "").strip()
    if match := re.fullmatch(r"every\s+(\d+)\s*(m|h|d)", spec, re.I):
        return int(match.group(1)) * PERIODS[match.group(2).lower()]
    if re.fullmatch(r"daily\s+at\s+\d{1,2}:\d{2}", spec, re.I):
        return 86400
    if re.fullmatch(r"weekly\s+on\s+[a-z]{3}[a-z]*\s+at\s+\d{1,2}:\d{2}", spec, re.I):
        return 7 * 86400
    return None
