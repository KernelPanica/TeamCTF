#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
RUN_DOCKER=1 .venv/bin/python -m pytest -q -p no:cacheprovider
printf '%s\n' 'CHECKPOINT 7 PASSED'
