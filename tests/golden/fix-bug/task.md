---
max_attempts: 2
verify: python3 -m unittest
allowed_tools: Read,Glob,Grep,Edit,Write,Bash(python3 *)
---
# Fix the adding function

`calc.py` has a bug: `add` gives the wrong answer. Fix it without changing `test_calc.py`.

## Acceptance Criteria
- `python3 -m unittest` passes in the working directory [verify]
- `test_calc.py` is unchanged [verify]
