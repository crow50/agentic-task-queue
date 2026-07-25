---
model: claude-haiku-4-5-20251001
review_model: claude-haiku-4-5-20251001
max_attempts: 2
timeout_minutes: 10
allowed_tools: Read,Glob,Grep,Edit,Write,Skill(changelog-entry)
depends_on: example-skill-creation-task
attempts: 0
---
# Record recent work in the changelog

Use /changelog-entry to add entries to `CHANGELOG.md` in the current
directory (create the file with an `## Unreleased` section if it doesn't
exist) recording two changes: "Added word-frequency CLI tool (wordfreq.py)"
and "Fixed dispatcher crash when the claude binary is missing".

The `depends_on` key above holds this task back until
`example-skill-creation-task` has completed, so both files can be dropped
into `pending/` at the same time.

## Acceptance Criteria
- `CHANGELOG.md` exists and follows the format defined by the
  changelog-entry skill (correct section headings, imperative one-liners).
- Both changes are recorded: the wordfreq addition under `Added` and the
  dispatcher fix under `Fixed`.
- No duplicate or unrelated entries were introduced.
