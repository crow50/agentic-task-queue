"""Turn what happened after a task finished into candidate follow-ups (Phase 9). Pure: the dispatcher fetches the JSON
and records what it has seen; this decides what counts and words the instruction.

Everything a person or a bot wrote is untrusted. Only listed authors count, and their text goes into the follow-up
inside the <external_input> fence, never into the instructions around it.
"""

import re

from . import fence

FAILED_CHECKS = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ERROR"}
DURATION = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.I)
UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def _plain(name):
    """A login in lower case without GitHub's `[bot]` suffix, so `copilot-pull-request-reviewer` matches the bot either way."""
    return str(name or "").strip().lower().removesuffix("[bot]")


def parse_authors(raw):
    """The logins in FOLLOWUP_AUTHORS, lower case. Empty means nobody counts."""
    return {_plain(name) for name in (raw or "").split(",") if name.strip()}


def login(item):
    person = item.get("user") or item.get("author") or {}
    return _plain(person.get("login"))


def _say(comment):
    where = comment.get("path") or ""
    if where and comment.get("line"):
        where += f":{comment['line']}"
    return f"- {where + ': ' if where else ''}{(comment.get('body') or '').strip()}"


def review_batches(reviews, inline_comments, authors, seen):
    """One batch per submitted review by an allowed author that is not in `seen`: its body and its inline comments.
    A review still pending is not complete, so it waits; seven comments become one batch, not seven."""
    batches = []
    for review in reviews or []:
        key, state = f"review:{review.get('id')}", str(review.get("state") or "").upper()
        if key in seen or state == "PENDING" or login(review) not in authors:
            continue
        mine = [c for c in inline_comments or [] if str(c.get("pull_request_review_id")) == str(review.get("id"))]
        body = (review.get("body") or "").strip()
        if not body and not mine:
            continue  # an approval with nothing to say needs no work
        batches.append({"key": f"review:{review.get('id')}", "keys": [key], "author": login(review), "kind": "review",
                        "text": "\n".join(filter(None, [body, *map(_say, mine)])), "count": len(mine)})
    return batches


def talk_batch(comments, authors, seen, kind="comment"):
    """The not-yet-seen plain comments from allowed authors as one batch (or none)."""
    fresh = [c for c in comments or [] if f"talk:{c.get('id')}" not in seen and login(c) in authors
             and (c.get("body") or "").strip()]
    if not fresh:
        return []
    return [{"key": f"talk:{fresh[0].get('id')}", "keys": [f"talk:{c.get('id')}" for c in fresh],
             "author": ", ".join(dict.fromkeys(login(c) for c in fresh)), "kind": kind,
             "text": "\n".join(f"- {login(c)}: {c['body'].strip()}" for c in fresh), "count": len(fresh)}]


def checks_batch(rollup, sha, seen):
    """A batch naming the failing checks of one commit, once per commit. Names come from CI config, not a person."""
    failing = []
    for check in rollup or []:
        outcome = str(check.get("conclusion") or check.get("state") or "").upper()
        if outcome in FAILED_CHECKS:
            failing.append(str(check.get("name") or check.get("context") or "a check"))
    key = f"checks:{sha or ','.join(sorted(failing))}"
    if not failing or key in seen:
        return []
    return [{"key": key, "keys": [key], "author": "ci", "kind": "checks", "text": "\n".join(f"- {n} failed" for n in failing),
             "count": len(failing)}]


REVIEW_CRITERIA = (
    "- Each point in the feedback is either fixed on this branch or recorded in the backlog file with the reason.\n"
    "- The report says, one line each, which points were fixed and which were deferred."
)
CHECKS_CRITERIA = "- The failing checks are fixed, or the report says exactly why they cannot be."
ISSUE_CRITERIA = "- Each new comment is answered by what was done, or by why nothing was."


def review_spec(url, batch, backlog_file):
    return (
        f"New feedback came in on {url} ({batch['kind']}, {batch['count']} comment{'s' if batch['count'] != 1 else ''}). "
        f"For each point below, either fix it on this branch, or, if it is not worth fixing now, record it in "
        f"{backlog_file} with the reason. Then report which points you fixed and which you deferred, one line each.\n\n"
        + fence.RULE + "\n" + fence.wrap(batch["text"], f"{batch['kind']} by {batch['author']} on {url}")
    )


def checks_spec(url, batch):
    return (f"Checks are failing on {url}. Find out why from the repository itself, fix what is wrong on this branch, "
            "and report what you changed.\n\n" + fence.RULE + "\n" + fence.wrap(batch["text"], f"checks on {url}"))


def issue_spec(url, batch):
    return (f"There are new comments on the issue {url}. Read them, do what they ask if it is within this task's tools, "
            "and report what you did or why you did not.\n\n" + fence.RULE + "\n"
            + fence.wrap(batch["text"], f"comments on {url}"))


def duration_seconds(spec):
    """Seconds in a `revisit:` value like 12h, 7d or 2w, or None when it is not one."""
    match = DURATION.match(spec or "")
    return int(match.group(1)) * UNITS[match.group(2).lower()] if match else None


def remember(seen, keys, keep=300):
    """`seen` (a list) with `keys` added, cut to the newest `keep`."""
    return (list(seen) + [k for k in keys if k not in seen])[-keep:]
