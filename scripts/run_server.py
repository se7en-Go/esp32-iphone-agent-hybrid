"""启动本机网页和 MCP 后端，只监听 127.0.0.1:8890。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import SERVER, fail, require_venv  # noqa: E402


def main() -> int:
    if sys.platform == "win32":
        if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
            print("用法: python scripts/run_server.py")
            print("Windows 原生前台服务，板载 AirPlay；终端关闭后服务停止。")
            print("请先运行 scripts/setup.ps1，然后运行 scripts/run-server.ps1。")
            print("start / status / restart-worker / stop 后台管理仅用于 Unix。")
            return 0
        if sys.argv[1:]:
            fail(
                "Windows 原生前台模式不支持管理子命令或附加参数："
                + " ".join(sys.argv[1:])
                + "。请不带参数运行 scripts/run-server.ps1，或使用 --help 查看用法。"
            )
    python = require_venv()
    env = os.environ.copy()
    env.setdefault("HOST", "127.0.0.1")
    env.setdefault("PORT", "8890")
    env.setdefault("HIDAGENT_DATA", str(SERVER / ".local-data"))
    if sys.platform != "win32":
        os.chdir(SERVER)
        os.execve(str(python), [str(python), "-m", "hidagent.local_runtime", *sys.argv[1:]], env)
    print("即将打开 http://127.0.0.1:8890")
    print("三个灯 device / screen / calibrated 都绿，才算通。")
    # execv 不带 env，HIDAGENT_DATA 不会进新进程，校准会写到家目录而不是 server/.local-data。
    os.execve(str(python), [str(python), "-m", "hidagent"], env)
    fail("没能启动服务。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
