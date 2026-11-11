"""以 stdio 启动 MCP。给 Cursor / Claude Code / Codex 当命令用。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import fail, require_venv  # noqa: E402


def main() -> int:
    python = require_venv()
    url = os.environ.get("IPHONE_AGENT_URL", "http://127.0.0.1:8890")
    os.execv(str(python), [str(python), "-m", "hidagent.cli", "--url", url, "mcp"])
    fail("没能启动 MCP。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
