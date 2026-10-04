#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
env -u ARENA_MANAGEMENT_IP .venv/bin/python -m arena.app.runtime.firewall
RUN_DOCKER=1 RUN_ARENA=1 RUN_BROWSER=1 .venv/bin/python -m pytest -q -p no:cacheprovider
echo 'STAGE 17 SOURCE/ARTIFACT TESTS PASSED'
echo 'CHECKPOINT 17 remains pending: install release on two clean hosts and complete release/README.md acceptance.'
