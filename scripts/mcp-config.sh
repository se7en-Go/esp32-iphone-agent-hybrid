#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x server/.venv/bin/python ]]; then
  echo "请先运行 ./scripts/setup.sh" >&2
  exit 1
fi
exec server/.venv/bin/python scripts/mcp_config.py "$@"
