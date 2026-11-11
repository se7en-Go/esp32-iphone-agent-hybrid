#!/usr/bin/env bash
# 开发者从源码编译 N16R8 固件。普通使用者不要运行本脚本，也不要安装 arduino-cli。
# 预编译 merged.bin 由 GitHub Actions 产出，刷机用仓库根目录的 flash.sh / flash.ps1。
# 用法：hardware/build_firmware.sh [--debug] [--sta] [--ap-tune]
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
FQBN="esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,FlashSize=16M,PSRAM=opi,PartitionScheme=app3M_fat9M_16MB"
CLI="${ARDUINO_CLI:-arduino-cli}"
DEFS=""
for a in "$@"; do
  case "$a" in
    --debug) DEFS+=" -DDEBUG_SERIAL=1" ;;
    --sta)
      if [[ ! -f hid_bridge/secrets.h ]]; then
        echo "缺少 hid_bridge/secrets.h。那是开发者测试开关，从 secrets.example.h 复制。普通配网请用 scripts/wifi_config.py。" >&2
        exit 1
      fi
      DEFS+=" -DSTA_FROM_SECRETS=1"
      ;;
    --ap-tune) DEFS+=" -DAP_TUNE=1" ;;
    *)
      echo "未知参数：$a（只接受 --debug --sta --ap-tune）" >&2
      exit 1
      ;;
  esac
done
case "$ROOT" in
  *" "*) echo "固件目录不能包含空格：$ROOT" >&2; exit 1 ;;
esac
if ! command -v "$CLI" >/dev/null 2>&1; then
  echo "没有找到 arduino-cli。这是开发者工具。普通刷机请用仓库根目录的 flash.sh / flash.ps1，它们只需要 Python 和 esptool。" >&2
  exit 1
fi
DEFS+=" -include $ROOT/hid_bridge/nimble_cfg.h"
mkdir -p build dist
"$CLI" compile --config-file "$ROOT/arduino-cli.yaml" --fqbn "$FQBN" \
  --build-property "compiler.cpp.extra_flags=$DEFS" \
  --build-property "compiler.c.extra_flags=$DEFS" \
  --output-dir "$ROOT/build" "$ROOT/hid_bridge"
MERGED="$ROOT/build/hid_bridge.ino.merged.bin"
if [[ ! -f "$MERGED" ]]; then
  echo "编译结束了，但没有找到 $MERGED。请把上面的编译日志保留下来。" >&2
  exit 1
fi
cp "$MERGED" "$ROOT/dist/esp32s3-n16r8-merged.bin"
echo "已生成 $ROOT/dist/esp32s3-n16r8-merged.bin"
ls -lh "$ROOT/dist/esp32s3-n16r8-merged.bin"
