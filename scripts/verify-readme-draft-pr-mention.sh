#!/bin/bash
set -euo pipefail
head -n 20 README.md | grep -qi "draft pull request"
