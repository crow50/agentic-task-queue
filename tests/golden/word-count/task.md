---
max_attempts: 2
verify: bash scripts/verify-counts.sh
---
# Count the words

Read `input.txt` and write `counts.json`: a JSON object mapping each lower-case word to how many times it appears.
Words are separated by whitespace and punctuation is not part of a word.

## Acceptance Criteria
- `counts.json` is valid JSON [verify]
- Every word in `input.txt` is counted correctly [verify]
