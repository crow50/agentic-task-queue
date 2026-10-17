#!/bin/bash
python3 - <<'PY'
import json, re, sys
from collections import Counter
try:
    got = json.load(open("counts.json"))
except (OSError, ValueError) as exc:
    sys.exit(f"counts.json is missing or not JSON: {exc}")
want = Counter(re.findall(r"[a-z]+", open("input.txt").read().lower()))
if got != dict(want):
    sys.exit(f"counts differ: expected {dict(want)}, got {got}")
print(f"{len(want)} words counted correctly")
PY
