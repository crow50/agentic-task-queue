---
model: claude-sonnet-5
escalation_model: claude-sonnet-5
review_model: claude-haiku-4-5-20251001
max_attempts: 2
timeout_minutes: 15
quiet: true
cwd: __QUEUE_DIR__
allowed_tools: Read,Glob,Grep,Write(tasks/proposals/**),Bash(python3 scripts/open_proposal_pr.py*)
attempts: 0
---
# Turn a finished task into a skill

Task `__TASK_ID__` finished well. Write a reusable skill from it, so the next time something like it comes up the
steps are already known. You never apply a change yourself: you write one patch file, and
`scripts/open_proposal_pr.py` checks it and opens a pull request that a human reviews.

1. Read `tasks/done/__TASK_ID__.md`: the task as asked, and its `## Result` section as delivered. If the file is not
   there, write nothing and finish with exactly `no proposal`.
2. Decide what generalises. A skill is the method, not the one-off content: the order of steps, the commands that
   worked, the checks that proved it, and the traps the task ran into. Leave out names, ids and paths that belong to
   this one task. If nothing generalises, write nothing and finish with exactly `no proposal`.
3. Write the skill as one new file, `.claude/skills/<short-kebab-name>/SKILL.md`, with frontmatter:
   `name` (the same kebab-case name) and `description` (one sentence that says when to use it), then the steps as a
   short numbered list. Keep it under 60 lines.
4. Write one unified diff that creates only that file, with paths relative to the queue's root
   (`--- /dev/null`, `+++ b/.claude/skills/<name>/SKILL.md`), to
   `tasks/proposals/skill-__TASK_ID__.patch`.
5. Run `python3 scripts/open_proposal_pr.py tasks/proposals/skill-__TASK_ID__.patch`. It prints one line saying
   whether it opened a pull request or why it refused; do not retry a refusal with a different file.
6. Finish with one line: the patch file name and the script's output, or exactly `no proposal`.

## Acceptance Criteria
- Either exactly one file `tasks/proposals/skill-__TASK_ID__.patch` exists (or has been moved to
  `tasks/proposals/opened/` or `tasks/proposals/rejected/` by the script) and the final line names it, or no patch
  was written and the final line is exactly `no proposal`.
- The patch creates only one new file under `.claude/skills/`, and nothing else.
