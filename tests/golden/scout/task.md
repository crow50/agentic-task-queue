---
max_attempts: 2
verify: bash scripts/verify-shortlist.sh
value_class: research
---
# Shortlist the listings

`listings.txt` has one listing per line as `name | price | bedrooms`. Write `shortlist.md`: a `## Candidates` section with one
line `- <name>: <why it fits>` for each listing that costs at most 500000 and has at least 3 bedrooms, and an `## Excluded`
section with one line `- <name>: excluded because <reason>` for every other listing.

## Acceptance Criteria
- Every listing that fits appears under Candidates, and none that does not [verify]
- Every excluded listing appears under Excluded with a reason that says "because" [verify]
