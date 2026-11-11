"""入口脚本能打印帮助，并且文档没有叫人去装 Linux 子系统。"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BANNED = ("请先装 WSL", "请先安装 WSL", "install WSL", "先装 WSL2", "Windows 用户请先装 WSL")


def test_help_exits_cleanly():
    for script in ("scripts/setup.py", "scripts/flash.py", "scripts/wifi_config.py", "scripts/mcp_config.py"):
        result = subprocess.run([sys.executable, str(ROOT / script), "--help"], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_docs_do_not_tell_windows_users_to_install_a_subsystem():
    roots = [
        ROOT / "docs", ROOT / "scripts", ROOT / "skills", ROOT / "prompts",
        ROOT / "README.md", ROOT / "README.en.md",
    ]
    for root in roots:
        paths = [root] if root.is_file() else list(root.rglob("*"))
        for path in paths:
            if not path.is_file() or path.suffix.lower() not in {".md", ".py", ".ps1", ".sh", ""}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for phrase in BANNED:
                assert phrase not in text, f"{path} contains {phrase}"
