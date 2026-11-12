"""经 COM 口把 Wi-Fi 写进板子 NVS。换地方再跑一次，不用重烧，也不会把密码写进仓库。"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import fail  # noqa: E402

try:
    from hidagent.device import Device, DeviceError
except ImportError:
    Device = None
    DeviceError = OSError


def _password(given: str) -> str:
    if given:
        print("密码来自参数，可能会留在终端历史里。更稳妥的是不写 --password，让脚本来问。")
        return given
    env = os.environ.get("WIFI_PASS")
    if env is not None:
        return env
    return getpass.getpass("Wi-Fi 密码（不会显示，也不会写入日志）：")


async def _run(args: argparse.Namespace) -> None:
    if Device is None:
        fail("还没有安装依赖。请先运行 scripts/setup.sh 或 scripts/setup.ps1。")
    device = Device()
    try:
        await device.connect(args.port or None)
        if args.ap:
            print("即将改成 AP。只靠 Wi-Fi 上网的电脑连上板子热点后会断网，云端模型也会断。")
            print("只有电脑另外插了网线时才建议这样。手机上网要靠自己的蜂窝数据。")
            await device.configure_network("ap")
        else:
            if not args.ssid:
                fail("STA 需要 --ssid。这是家里或办公室的 2.4GHz 名称，不是 5GHz。")
            await device.configure_network("sta", args.ssid, _password(args.password))
        for _ in range(20):
            await asyncio.sleep(1)
            try:
                raw = await device.query("status")
                status = json.loads(raw)
            except (DeviceError, json.JSONDecodeError):
                continue
            net = status.get("net")
            ip = status.get("ip")
            print(f"net={net} ip={ip} usb={status.get('usb')}")
            if args.ap and net == "ap":
                info = json.loads(await device.query("netinfo"))
                print(f"热点名称：{info.get('ssid')}")
                print(f"热点密码：{info.get('pass')}")
                print("密码只出现在这台电脑的终端里。不要截图，不要贴到聊天。")
                print("电脑改连这个热点前，确认网线还能上网。手机也连这个热点，上网靠蜂窝。")
                return
            if not args.ap and net == "sta" and ip and ip != "0.0.0.0":
                print("板子已经连上路由器。手机和电脑也连同一个 2.4GHz，然后打开屏幕镜像，选 ESP32 Agent。")
                return
        fail("命令已经交给板子，但 20 秒内没有看到预期的网络状态。请确认是 2.4GHz、密码正确，且没有门户登录。")
    except DeviceError as exc:
        fail(str(exc))
    finally:
        device.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="经 COM 口配置板子的 Wi-Fi，写入 NVS。",
        epilog="配网不改变控制手机的方式。USB 或蓝牙 HID 用 scripts/hid-mode.sh / hid-mode.ps1。蓝牙 HID 还没有在真机上验证。",
    )
    parser.add_argument("--ssid", default="", help="2.4GHz 网络名称")
    parser.add_argument("--password", default="", help="不建议写在命令行里；留空则提示输入")
    parser.add_argument("--ap", action="store_true", help="改回热点。仅适合电脑另有网线的情况")
    parser.add_argument("--port", default="", help="例如 COM5 或 /dev/cu.usbserial-…。留空则自动找 CH343/FTDI/CP210x")
    args = parser.parse_args(argv)
    if not args.ap and not args.ssid:
        fail("请提供 --ssid，或在确认电脑另有网线后使用 --ap。")
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        if os.environ.get("HIDAGENT_DEBUG") == "1":
            raise
        fail("配网失败。请看上一行中文说明。COM 口要插在电脑上，并且没有被别的串口程序占用。")
