---
schedule: weekly on mon at 06:00
model: claude-sonnet-5
escalation_model: claude-sonnet-5
review_model: claude-haiku-4-5-20251001
max_attempts: 2
timeout_minutes: 20
quiet: true
cwd: __QUEUE_DIR__
allowed_tools: Read,Glob,Grep,Write(tasks/proposals/**),Bash(python3 scripts/open_proposal_pr.py*)
attempts: 0
---
# Weekly improver: propose fixes for what keeps going wrong

You improve this task queue from its own failure log. You never apply a change
yourself: you write one patch file, and `scripts/open_proposal_pr.py` checks it
and opens a pull request that a human reviews. If there is nothing worth
proposing, write nothing.

1. Read `logs/lessons.jsonl` in the current directory (the queue's root). Each
   line is `{task, attempt, feedback, time}` for one failed attempt. Use only
   lessons from the last 30 days.
2. Read `worker_preamble.md` (the instructions every worker gets) and
   `coordinator/CLAUDE.md.template` (how task files are written; the installer
   renders `coordinator/CLAUDE.md` from it, so propose edits to the template).
3. Group the lessons by cause: a denied command, a criterion that could not be
   verified, a report that pointed at a file the reader never sees, a timeout,
   truncated output. Propose a fix only for a cause that appears in **at least 2
   lessons**; one example is noise. Pick the single best-supported cause.
4. Write one unified diff, with paths relative to the queue's root
   (`a/worker_preamble.md`, `b/worker_preamble.md`), to
   `tasks/proposals/<YYYYmmdd>-<short-slug>.patch`. Put a short plain-text
   rationale before the first `diff --git` line: the cause, how many lessons it
   explains, and the task names. The patch may touch only:
   - `worker_preamble.md`, or `coordinator/CLAUDE.md.template`;
   - new test files named `tests/test_<something>.py` (never edit an existing test);
   - new skill files under `.claude/skills/`.
   Never touch `dispatcher.py`, `.github/`, `scripts/` or anything else; the
   checker rejects the whole patch if you do.
5. Run `python3 scripts/open_proposal_pr.py tasks/proposals/<that file>`.
   It runs the test suite on the patched tree and opens a pull request only if
   everything passes. It prints one line either way. It runs the whole test
   suite, which takes a few minutes, so give that Bash call its maximum timeout
   (600000 ms). Do not retry or edit the patch after it has run.
6. Finish with one line: the patch file name and the script's output, or exactly
   `no proposal` if fewer than 2 lessons share a cause (or the log is empty).

## Acceptance Criteria
- Either exactly one file `tasks/proposals/*.patch` exists (or has been moved to
  `tasks/proposals/opened/` or `tasks/proposals/rejected/` by the script) and
  the final line names it, or no patch was written and the final line is
  exactly `no proposal`.
- Any patch file changes only `worker_preamble.md`, `coordinator/CLAUDE.md.template`,
  new `tests/test_*.py` files, or new files under `.claude/skills/`, and never
  `dispatcher.py`.
- The proposed change is backed by at least 2 lessons from `logs/lessons.jsonl`,
  named in the rationale at the top of the patch.
