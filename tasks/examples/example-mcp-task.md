---
model: claude-sonnet-5
review_model: claude-haiku-4-5-20251001
max_attempts: 2
timeout_minutes: 15
mcp_config: mcp/fetch.json
allowed_tools: Read,Write,mcp__fetch__fetch
attempts: 0
---
# Summarize the current Python release notes

Using the `fetch` MCP tool, retrieve https://docs.python.org/3/whatsnew/index.html
and follow it to the newest stable release's "What's New" page. Write a file
`python-whatsnew-summary.md` in the current directory containing the release
version, its release date if stated, and a bullet list of the five most
significant changes with one-sentence explanations aimed at a sysadmin.

## Acceptance Criteria
- `python-whatsnew-summary.md` exists and names a real, current stable Python
  3.x version.
- It lists exactly five changes, each with a one-sentence plain-English
  explanation (no raw HTML or navigation text pasted in).
- The content clearly comes from the fetched pages, not from memory alone —
  it should reference specifics that appear on the "What's New" page.
