"""What a finished task produced, as a record a follow-up can start from (Phase 9).

A PR is only one kind of result. Each kind is one small function registered with @kind: it looks at the facts the
dispatcher collected when the task ended and returns a ref (what was made, with the evidence checked) or None. Adding
a kind (a calendar draft, say) means registering one more function; nothing else here changes.
"""

import re

KINDS = {}
ISSUE_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/\d+")
SUMMARY_CHARS = 600


def kind(name):
    def register(function):
        KINDS[name] = function
        return function
    return register


@kind("pr")
def _pull_request(facts):
    if not facts.get("pr_url"):
        return None
    # publish_draft_pr only returns a URL after it checked the PR is open, a draft, on this branch and at the pushed commit
    return {"url": facts["pr_url"], "branch": facts.get("branch"), "draft": True, "checked": True}


@kind("file")
def _files(facts):
    files = [{"path": f["path"], "delivered": bool(f.get("delivered"))} for f in facts.get("files") or []]
    return files or None


@kind("issue")
def _issues(facts):
    urls = list(dict.fromkeys(ISSUE_URL.findall(facts.get("report") or "")))
    # the worker said so; nothing has checked that the issue exists
    return [{"url": url, "claimed": True} for url in urls] or None


@kind("report")
def _report(facts):
    text = (facts.get("report") or "").strip()
    return {"chars": len(text)} if text else None


def build(facts):
    """{"kinds": [...], "refs": {kind: ref}} for the facts of a finished task. A task that made nothing is ["none"]."""
    refs = {}
    for name, function in KINDS.items():
        if (ref := function(facts)) is not None:
            refs[name] = ref
    return {"kinds": list(refs) or ["none"], "refs": refs}


def summarize(text, limit=SUMMARY_CHARS):
    """The first `limit` characters of a report on one line, for a list or a prompt."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def describe(outcome):
    """One line for a phone: what the task ended as and what it made."""
    parts = []
    refs = outcome.get("refs") or {}
    for name in outcome.get("kinds") or []:
        ref = refs.get(name)
        if name == "pr" and ref:
            parts.append(f"pull request {ref['url']}")
        elif name == "file" and ref:
            parts.append("files " + ", ".join(f["path"] for f in ref))
        elif name == "issue" and ref:
            parts.append("issue " + ", ".join(i["url"] for i in ref))
        elif name == "report":
            parts.append("a report")
        elif name == "none":
            parts.append("nothing")
        else:
            parts.append(name)
    return f"{outcome.get('status', '?')}: " + (", ".join(parts) or "nothing")
