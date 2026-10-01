#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Explicit host prerequisite: dedicated Arena chains, no flush of host policies.
.venv/bin/python -m arena.app.runtime.firewall
RUN_DOCKER=1 RUN_ARENA=1 .venv/bin/python -m pytest -q -p no:cacheprovider
printf '%s\n' 'CHECKPOINT 8A PASSED'
