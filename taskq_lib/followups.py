"""Build a task that continues another (Phase 9): what it inherits, what it is named, what the worker is told.

Pure: callers pass in the parent's frontmatter, body and outcome and get a new frontmatter and body back. The
dispatcher decides where the file goes and whether it needs approval.
"""

import re

from . import fence

# What a follow-up takes from its parent: the same tools and places, so it is the same job by the same rules. The
# dispatcher's own keys (tier, pr_url, branch, attempts, session ids, schedule, proposals) are never copied, and
# neither is `review`: skipping the review was a choice for the parent's exact criteria.
INHERITED = ("model", "review_model", "escalation_model", "max_attempts", "timeout_minutes", "allowed_tools",
             "mcp_config", "repo", "base", "cwd", "deliver", "verify", "max_turns", "quiet", "value_class")
CYCLE_DEPTH = 99  # what depth_of reports for a chain that loops back on itself
SUFFIX = re.compile(r"(-f\d+)+$")


def _criteria(body):
    match = re.search(r"^##\s*Acceptance Criteria\s*\n(.*?)(?=^##\s|\Z)", body or "", re.S | re.M)
    return match.group(1).strip() if match else ""


DEFAULT_CRITERION = "- The requested change is made."


def build(parent_meta, parent_body, text, parent_stem, criteria=None):
    """(frontmatter, body) of a task that continues `parent_stem` and asks for `text`. `criteria` are the new acceptance
    criteria (bullet lines); the parent's still hold after them."""
    meta = {key: parent_meta[key] for key in INHERITED if parent_meta.get(key)}
    meta.update({"follows": parent_stem, "attempts": "0"})
    earlier = _criteria(parent_body)
    heading = re.search(r"^#\s+(.+)$", parent_body or "", re.M)
    body = (
        f"# Follow-up to {parent_stem}\n\n"
        + (f"The earlier task was: {heading.group(1).strip()}\n\n" if heading else "")
        + f"{text.strip()}\n\n## Acceptance Criteria\n{(criteria or DEFAULT_CRITERION).strip()}\n"
        + (earlier + "\n" if earlier else "")
    )
    return meta, body


def name_for(parent_stem, taken):
    """`<root>-f<n>` for the first n not in `taken`, where root is the parent without its own -f suffixes."""
    root = SUFFIX.sub("", parent_stem) or parent_stem
    number = 1
    while f"{root}-f{number}" in taken:
        number += 1
    return f"{root}-f{number}"


def depth_of(stem, parent_of):
    """How many links lie between `stem` and the first task of its chain; `parent_of(stem)` is its parent or None."""
    seen, depth = {stem}, 0
    while (parent := parent_of(stem)):
        if parent in seen:
            return CYCLE_DEPTH
        seen.add(parent)
        stem, depth = parent, depth + 1
    return depth


def prompt_note(outcome, report_text):
    """What the worker of a follow-up is told about the task it continues. The earlier report is the worker's own
    output from another run, which may hold text it read on the web, so it travels inside the fence."""
    parent, refs = outcome.get("task", "?"), outcome.get("refs") or {}
    lines = [f"This task continues {parent}, which ended {outcome.get('status', '?')}. Start from what is true below "
             "and do not redo finished work."]
    if pr := refs.get("pr"):
        lines.append(f"It opened the pull request {pr['url']} on branch {pr.get('branch')}. You are working on that "
                     "branch: the dispatcher pushes your changes to the same pull request, so do not open another.")
    for item in refs.get("file") or []:
        lines.append(f"It delivered the file {item['path']}.")
    for item in refs.get("issue") or []:
        lines.append(f"It reported the issue {item['url']}.")
    data = []
    if outcome.get("summary"):
        data.append(f"Summary: {outcome['summary']}")
    if report_text.strip():
        data.append(f"Report:\n{report_text.strip()}")
    if data:
        lines += [fence.RULE, fence.wrap("\n\n".join(data), f"what {parent} reported")]
    return "\n".join(lines) + "\n\n"
