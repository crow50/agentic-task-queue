---
model: claude-sonnet-5
review_model: claude-haiku-4-5-20251001
max_attempts: 2
timeout_minutes: 10
allowed_tools: Read,Glob,Grep,Write
attempts: 0
---
# Create a changelog-entry skill

Author an Agent Skill at `.claude/skills/changelog-entry/SKILL.md` in the
current directory. The skill should teach Claude to append a well-formed
entry to a `CHANGELOG.md` (Keep a Changelog style): correct section
(`Added`/`Changed`/`Fixed`/`Removed`), imperative one-line description,
today's date on new version headings, and no duplicate entries. Include
concrete formatting examples in the skill body.

## Acceptance Criteria
- `.claude/skills/changelog-entry/SKILL.md` exists with valid YAML
  frontmatter containing `name: changelog-entry` and a `description`.
- The `description` says both what the skill does and when to use it,
  specific enough for automatic triggering (mentions changelogs/release
  notes, not just "helps with documentation").
- The skill body gives imperative step-by-step instructions and at least one
  concrete example of a correctly formatted changelog entry.
- The file is under 100 lines — skills should be concise.
