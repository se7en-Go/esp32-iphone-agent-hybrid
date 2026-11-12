"""创建虚拟环境并安装电脑端依赖。有 uv 就用 uv，没有就用 venv + pip。"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import SERVER, VENV, fail, venv_python  # noqa: E402


def create_venv() -> Path:
    python = venv_python()
    if python.is_file():
        return python
    VENV.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("uv"):
        # --seed 装上 pip。没有 pip 的 uv venv 会让后面的 pip 回退失败。
        result = subprocess.call(["uv", "venv", "--seed", "--python", "3.12", str(VENV)])
        if result != 0:
            result = subprocess.call(["uv", "venv", "--python", "3.12", str(VENV)])
        if result == 0 and venv_python().is_file():
            return venv_python()
        print("uv 没能建好环境，改用当前的 Python。")
    if sys.version_info < (3, 10):
        fail(
            "需要 Python 3.10 或更新版本。"
            "Windows 请到 https://www.python.org/downloads/windows/ 安装，并勾选 Add python.exe to PATH。"
            "macOS 可以用 python.org 的安装包，不必先装 Homebrew。"
        )
    subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    if not venv_python().is_file():
        fail("虚拟环境创建了，但找不到 python。请把上面的输出保留下来。")
    return venv_python()


def install(python: Path, dev: bool) -> None:
    extras = ".[dev,mcp]" if dev else ".[mcp]"
    if shutil.which("uv"):
        command = ["uv", "pip", "install", "--python", str(python), "-e", extras]
        if subprocess.call(command, cwd=SERVER) == 0:
            return
        print("uv pip 失败了。如果这个环境里没有 pip，就改用标准 venv。")
    probe = subprocess.call([str(python), "-m", "pip", "--version"])
    if probe != 0:
        shutil.rmtree(VENV, ignore_errors=True)
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
        python = venv_python()
    subprocess.check_call([str(python), "-m", "pip", "install", "-U", "pip"])
    subprocess.check_call([str(python), "-m", "pip", "install", "-e", extras], cwd=SERVER)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="安装电脑端依赖（PyAV、pyserial、esptool、MCP）。",
        epilog=(
            "控制手机可以走 USB，也可以走蓝牙 HID。"
            "配对见 scripts/hid-mode.sh 或 scripts/hid-mode.ps1。"
            "蓝牙 HID 还没有在真机上验证。"
        ),
    )
    parser.add_argument("--dev", action="store_true", help="连同 pytest 和 ruff 一起装，给开发者用")
    args = parser.parse_args(argv)
    python = create_venv()
    install(python, args.dev)
    print("Python 环境好了。")
    print("下一步：把 CI 产物 esp32s3-n16r8-merged.bin 放到 hardware/dist/ ，COM 口接电脑，然后刷机。")
    print(
        "手机不方便插 USB 时，刷机并配网之后可以用蓝牙 HID："
        "scripts/hid-mode.sh（Windows 用 hid-mode.ps1）。这一步还没有在真机上验证。"
    )
    if sys.platform == "win32":
        print("刷机：powershell -ExecutionPolicy Bypass -File hardware\\flash.ps1")
    else:
        print("刷机：./hardware/flash.sh")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError:
        fail("安装依赖失败了。请看上面 pip 的最后几行。网络不通时先检查代理，再重跑 setup。")
