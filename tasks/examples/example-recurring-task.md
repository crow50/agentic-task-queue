---
schedule: daily at 06:30
model: claude-haiku-4-5-20251001
escalation_model: claude-sonnet-5
review_model: claude-haiku-4-5-20251001
max_attempts: 2
timeout_minutes: 10
allowed_tools: Read,Glob,Grep,Write,Bash(df *),Bash(du -sh*),Bash(uptime),Bash(free *)
attempts: 0
---
# Daily server health report

Write a short server health report to `reports/health-<YYYY-MM-DD>.md` in the
current directory (create the `reports/` folder if needed). Include disk usage
(`df -h`), the five largest directories under the current directory
(`du -sh`), memory (`free -h`), and load average (`uptime`), each with a
one-line plain-English assessment. Flag anything concerning (e.g. a
filesystem over 85% full) in a "Warnings" section at the top, or state that
there are none.

## Acceptance Criteria
- A file `reports/health-<today's date>.md` exists and is dated correctly.
- The report contains real command output for disk usage, largest
  directories, memory, and load average — not placeholders.
- Each section has a one-line assessment, and a "Warnings" section at the
  top either lists concerns or explicitly says there are none.
