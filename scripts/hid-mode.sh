#!/usr/bin/env bash
# 切换 USB / 蓝牙 HID，或打开配对。电脑始终用 COM 口。
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x server/.venv/bin/python ]]; then
  echo "请先运行 ./scripts/setup.sh" >&2
  exit 1
fi
exec server/.venv/bin/python scripts/hid_mode.py "$@"
