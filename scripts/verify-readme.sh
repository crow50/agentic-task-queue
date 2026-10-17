#!/bin/bash
set -euo pipefail
test "$(cat .venv/x)" = "hi"
test -z "$(tail -n 1 README.md)"
