"""经 COM 口切换 USB / 蓝牙 HID，或打开配对窗口。电脑始终走串口，手机才走蓝牙。

蓝牙 HID 还没有在真机上验证。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import fail  # noqa: E402

try:
    from hidagent.device import Device, DeviceError, candidate_ports
except ImportError:  # 还没装电脑端时，--help 仍然能看
    Device = None  # type: ignore[misc, assignment]
    DeviceError = OSError  # type: ignore[misc, assignment]
    candidate_ports = None  # type: ignore[assignment]


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="选择手机控制走 USB 还是蓝牙 HID，或打开 60 秒配对窗口。",
        epilog="默认 auto：插着 USB 就走 USB，否则走已经连上的蓝牙 HID。蓝牙 HID 还没有在真机上验证。",
    )
    parser.add_argument("mode", nargs="?", choices=("usb", "ble", "auto"), help="写入板子 NVS 的传输偏好")
    parser.add_argument("--pair", action="store_true", help="打开 60 秒 HID 配对窗口")
    parser.add_argument("--unpair", action="store_true", help="让板子忘掉 HID 绑定。手机系统蓝牙里也要忽略此设备")
    parser.add_argument("--status", action="store_true", help="只查询当前模式和蓝牙状态")
    parser.add_argument("--port", default="", help="例如 COM5 或 /dev/cu.usbserial-…。留空则自动找 CH343/FTDI/CP210x")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    if Device is None or candidate_ports is None:
        fail("还没有安装电脑端。先运行 scripts/setup.sh（Windows 用 setup.ps1）。")
    if args.unpair and (args.mode or args.pair):
        fail("--unpair 单独使用。设好模式之后再打开配对。")
    device = Device()
    port = args.port or None
    if port is None:
        found = candidate_ports()
        print("自动查找 COM 口：" + ("、".join(found) if found else "没有找到，仍会再试一次。"))
    try:
        await device.connect(port)
    except DeviceError as exc:
        fail(str(exc))
    try:
        if args.unpair:
            await device.send("hid unpair")
            print("板子已忘掉 HID 绑定。请在 iPhone 的系统蓝牙里忽略此设备，然后重新配对。")
        elif args.mode:
            await device.send(f"hid {args.mode}")
            print(f"传输已设为 {args.mode}，并写入板子，重启后仍然有效。")
        if args.pair:
            await device.send("hid pair")
            print("配对窗口已打开，大约 60 秒。")
            print("在 iPhone 上打开 设置 → 蓝牙 → 选 ESP32 Agent（名字后面可能有四位字母）。")
            print("然后打开辅助触控。屏幕镜像里的名字也是 ESP32 Agent，是同一块板。")
        mode_line = await device.query("hid")
        raw = await device.query("status")
    except DeviceError as exc:
        fail(str(exc))
    finally:
        device.close()
    print(mode_line)
    try:
        hid = json.loads(raw).get("hid") or {}
    except json.JSONDecodeError:
        print(raw)
        return
    ble = hid.get("ble") or {}
    print(
        f"mode={hid.get('mode')} path={hid.get('path')} pace={hid.get('pace')} "
        f"adv={ble.get('adv')} conn={ble.get('conn')} bond={ble.get('bond')} pair={ble.get('pair')}"
    )
    if hid.get("path") in ("usb", "ble"):
        print("控制通道已经通。打开辅助触控后，可以在网页里校准。")
    elif hid.get("mode") == "usb":
        print("现在要求 USB。请把标着 USB 的口接到 iPhone，解锁，并打开辅助触控。")
    else:
        print("还没有控制通道。插上 USB，或按上面的步骤在 iPhone 蓝牙里选择 ESP32 Agent。")
    print("这一步还没有在真机上验证。")


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
