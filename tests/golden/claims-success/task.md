---
max_attempts: 2
verify: bash scripts/verify-report.sh
expect: fail
fake_only: true
---
# Write the report

Create `report.md` whose first line is `# Report`. (In the fake run the worker only says it did.)

## Acceptance Criteria
- `report.md` exists and starts with `# Report` [verify]
