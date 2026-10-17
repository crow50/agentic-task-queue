#!/bin/bash
test -f report.md || { echo "report.md is missing"; exit 1; }
test "$(head -n 1 report.md)" = "# Report" || { echo "report.md does not start with: # Report"; exit 1; }
echo "report.md is right"
