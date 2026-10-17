"""The hand-offs of a chain (Phase 9): build, then an independent test, then fix and test again.

Pure. The dispatcher builds each hop from the approved build task and this module's fixed wording, never from text a
worker wrote; the only worker text that travels is the tester's findings, and it travels inside the fence.
"""

import re

from . import fence

TESTER_TOOLS = "Read,Glob,Grep"
ROLES = ("tester", "fixer")
VERDICT = re.compile(r"^\s*FINDINGS:\s*(NONE\b.*)?$", re.I | re.M)

TESTER_CRITERIA = (
    "- The report's first line is `FINDINGS: NONE` or `FINDINGS:`, and in the second case each problem is a numbered "
    "line saying what is wrong, where, and what the build's acceptance criteria require instead.\n"
    "- Every acceptance criterion of the build was checked, by reading the changed files, not by trusting its report."
)


def split_findings(report):
    """(clean, findings): `clean` is True for `FINDINGS: NONE`. A report with no verdict line is not clean, and its
    whole text is the findings, so a tester that rambled still produces something the fixer can act on."""
    text = (report or "").strip()
    match = VERDICT.search(text)
    if match and match.group(1):
        return True, ""
    if match:
        return False, text[match.end():].strip() or text
    return False, text


def tester_body(playbook, build_stem, build_criteria):
    return (
        f"# Test the work of {build_stem}\n\n"
        "You are an independent tester. You did not write this code. Do not change any file.\n\n"
        f"{playbook.strip()}\n\n"
        f"## What {build_stem} was asked to do\n\n{build_criteria.strip() or '(no criteria were recorded)'}\n\n"
        f"## Acceptance Criteria\n{TESTER_CRITERIA}\n"
    )


def fixer_body(build_stem, findings, round_number):
    return (
        f"# Fix the problems found in {build_stem} (round {round_number})\n\n"
        "An independent tester checked this branch and reported the problems inside the tags below. Fix each one on this "
        "branch, change nothing else, and report what you changed, one line per problem.\n\n"
        + fence.RULE + "\n" + fence.wrap(findings, f"tester findings for {build_stem}") + "\n\n"
        "## Acceptance Criteria\n- Every problem in the findings is fixed, or you say exactly why it cannot be.\n"
        "- Nothing outside what the findings name was changed.\n"
    )


def rounds_for(declared, cap):
    """How many test rounds a build gets: what it declared, at most `cap`, at least one."""
    try:
        wanted = int(declared)
    except (TypeError, ValueError):
        wanted = 1
    return max(1, min(wanted, cap))
