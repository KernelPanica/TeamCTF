#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
env -u ARENA_MANAGEMENT_IP .venv/bin/python -m arena.app.runtime.firewall
RUN_DOCKER=1 RUN_ARENA=1 RUN_BROWSER=1 .venv/bin/python -m pytest -q -p no:cacheprovider
printf '%s\n' 'CHECKPOINT 11 PASSED'
