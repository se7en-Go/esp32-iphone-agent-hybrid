"""脚本共用的路径和报错。入口脚本都用仓库里的虚拟环境，不依赖 WSL。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
VENV = SERVER / ".venv"


def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def require_venv() -> Path:
    python = venv_python()
    if python.is_file():
        return python
    if sys.platform == "win32":
        fail("还没有 Python 环境。请先在仓库根目录运行：powershell -File scripts\\setup.ps1")
    fail("还没有 Python 环境。请先在仓库根目录运行：./scripts/setup.sh")
    return python
