#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -x server/.venv/bin/python ]]; then
  exec server/.venv/bin/python scripts/flash.py "$@"
fi
exec python3 scripts/flash.py "$@"
