#!/bin/bash
python3 - <<'PY'
import re, sys
rows = [[c.strip() for c in line.split("|")] for line in open("listings.txt") if line.strip()]
fits = {n for n, price, beds in rows if int(price) <= 500000 and int(beds) >= 3}
others = {n for n, _, _ in rows} - fits
try:
    text = open("shortlist.md").read()
except OSError:
    sys.exit("shortlist.md is missing")
candidates, _, excluded = text.partition("## Excluded")
named = lambda block: {n for n, _, _ in rows if re.search(rf"^- {re.escape(n)}:", block, re.M)}
if named(candidates) != fits:
    sys.exit(f"candidates should be {sorted(fits)}, found {sorted(named(candidates))}")
if named(excluded) != others:
    sys.exit(f"excluded should be {sorted(others)}, found {sorted(named(excluded))}")
for line in excluded.splitlines():
    if line.startswith("- ") and "because" not in line:
        sys.exit(f"an exclusion has no reason: {line}")
print(f"{len(fits)} candidates, {len(others)} exclusions, each with a reason")
PY
