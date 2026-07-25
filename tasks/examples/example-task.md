---
model: claude-sonnet-5
escalation_model: claude-opus-4-8
review_model: claude-haiku-4-5-20251001
max_attempts: 3
timeout_minutes: 20
allowed_tools: Read,Glob,Grep,Edit,Write,Bash(python3 *)
attempts: 0
---
# Write a word-frequency CLI tool

Create a Python script named `wordfreq.py` in the current directory. It should
take a text file path as its only argument and print the 10 most common words
with their counts, one per line, most frequent first. Words are compared
case-insensitively and punctuation is stripped. Also create a small sample
file `sample.txt` and show the script running against it.

## Acceptance Criteria
- `wordfreq.py` exists, runs with `python3 wordfreq.py <file>`, and uses only
  the Python standard library.
- Output lists at most 10 words, most frequent first, formatted as
  `<word> <count>` per line; counting is case-insensitive with punctuation
  stripped.
- Passing a missing file path prints a clear error message and exits with a
  non-zero status instead of a traceback.
- `sample.txt` exists and the worker's report shows the actual output of
  running the script on it.
