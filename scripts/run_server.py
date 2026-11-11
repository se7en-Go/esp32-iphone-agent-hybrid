"""启动本机网页和 MCP 后端，只监听 127.0.0.1:8890。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import SERVER, fail, require_venv  # noqa: E402


def main() -> int:
    python = require_venv()
    env = os.environ.copy()
    env.setdefault("HOST", "127.0.0.1")
    env.setdefault("PORT", "8890")
    env.setdefault("HIDAGENT_DATA", str(SERVER / ".local-data"))
    print("即将打开 http://127.0.0.1:8890")
    print("若系统询问 Python 是否可以联网：这是连板子 7201 的出站连接，允许专用网络即可。")
    print("三个灯 device / screen / calibrated 都绿，才算通。")
    # execv 不带 env，HIDAGENT_DATA 不会进新进程，校准会写到家目录而不是 server/.local-data。
    os.execve(str(python), [str(python), "-m", "hidagent"], env)
    fail("没能启动服务。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
