"""用 esptool 把预编译固件写进 N16R8。不安装 arduino-cli。

开发者要自己编译时加 --build-only。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import ROOT, fail, require_venv  # noqa: E402

BIN_NAME = "esp32s3-n16r8-merged.bin"


def find_bin(explicit: str) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            fail(f"找不到固件文件：{path}")
        return path
    candidates = [
        Path(os.environ["FIRMWARE_BIN"]) if os.environ.get("FIRMWARE_BIN") else None,
        ROOT / "hardware" / "dist" / BIN_NAME,
        ROOT / "hardware" / "build" / "hid_bridge.ino.merged.bin",
    ]
    for path in candidates:
        if path is not None and path.is_file():
            return path
    fail(
        "没有找到预编译固件 hardware/dist/" + BIN_NAME + "。\n"
        "小白路径：打开本仓库的 GitHub Actions，选最近一次成功的 Firmware 作业，"
        "下载名为 esp32s3-n16r8-merged.bin 的产物，放到 hardware/dist/ 里，再跑一次刷机。\n"
        "开发者路径：安装 arduino-cli 后运行 hardware/build_firmware.sh（Windows 用 build_firmware.ps1），"
        "不要让普通使用者走这一步。"
    )
    return Path()


def pick_port(explicit: str) -> str:
    if explicit:
        return explicit
    forced = os.environ.get("HIDAGENT_PORT", "").strip()
    if forced:
        return forced
    python = require_venv()
    code = (
        "from hidagent.device import candidate_ports\n"
        "print('\\n'.join(candidate_ports()))\n"
    )
    result = subprocess.run([str(python), "-c", code], capture_output=True, text=True, check=False)
    ports = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0:
        fail("列串口失败了。请设置 HIDAGENT_PORT 后再刷。\n" + result.stderr.strip())
    if len(ports) == 1:
        return ports[0]
    if not ports:
        fail(
            "没找到 COM 串口。请把标着 COM 的口接到电脑（不是标着 USB 的口），"
            "换一根能传数据的线。芯片可能是沁恒 CH343、FTDI 或 CP210x。"
            "Mac 上一般是 /dev/cu.usbserial-… 或 /dev/cu.wchusbserial…，Windows 是 COMx。"
            "Windows 若没有 COM 口，FTDI 装 https://ftdichip.com/drivers/vcp-drivers/ ，"
            "沁恒装 https://www.wch.cn/downloads/CH343SER_EXE.html 。macOS 上 FTDI 通常不用装驱动。"
        )
    fail("找到多个串口：" + ", ".join(ports) + "。请设置 HIDAGENT_PORT 指定 COM 口。")
    return ""


def build_only() -> int:
    hardware = ROOT / "hardware"
    if sys.platform == "win32":
        command = ["powershell", "-NoProfile", "-File", str(hardware / "build_firmware.ps1")]
    else:
        command = ["bash", str(hardware / "build_firmware.sh")]
    print("这是开发者编译，普通刷机不需要这一步。")
    return subprocess.call(command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 N16R8 固件刷进标着 COM 的口。")
    parser.add_argument("--port", default="", help="例如 COM5 或 /dev/cu.usbserial-…。留空则自动找 CH343/FTDI/CP210x")
    parser.add_argument("--bin", default="", help="merged.bin 的路径。留空则用 hardware/dist 里的预编译文件")
    parser.add_argument("--baud", type=int, default=460800)
    parser.add_argument("--build-only", action="store_true", help="从源码编译。需要本机已安装 arduino-cli")
    args = parser.parse_args(argv)
    if args.build_only:
        return build_only()
    firmware = find_bin(args.bin)
    port = pick_port(args.port)
    python = require_venv()
    print(f"刷写 {firmware.name} → {port}")
    print("请确认线在 COM 口。标着 USB 的口是留给 iPhone 的，刷机不要用它。")
    print("若板子上有 USB-OTG 或 IN-OUT 焊盘，确认它没有被焊上，否则 5V 可能倒灌进手机。")
    command = [
        str(python), "-m", "esptool", "--chip", "esp32s3", "--port", port,
        "--baud", str(args.baud), "write_flash", "0x0", str(firmware),
    ]
    code = subprocess.call(command)
    if code != 0:
        fail(
            "刷机失败。下一步：关掉占用这个口的串口监视器；确认是 COM 口；"
            f"再试一次并加上 --baud 115200。当前端口是 {port}。"
            "若仍然失败，按住板子上的 BOOT 再插拔 COM 口，然后立刻重跑刷机。"
        )
    print("刷好了。下一步：运行配网脚本，把 2.4GHz 的 Wi-Fi 经这个 COM 口写进板子。不用再刷机。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        fail("刷机脚本自己出错了。设置 HIDAGENT_DEBUG=1 可以看详细原因。先确认 Python 环境是用 setup 装的。")
