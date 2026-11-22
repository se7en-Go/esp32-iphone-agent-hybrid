"""打印可粘贴的 MCP 配置。不包含 Wi-Fi 密码或 API key。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import require_venv  # noqa: E402


def main() -> int:
    argparse.ArgumentParser(description="打印 MCP 配置片段。不包含密码或 API key。").parse_args()
    python = str(require_venv())
    url = os.environ.get("IPHONE_AGENT_URL", "").strip() or "http://127.0.0.1:8890"
    cursor = {
        "mcpServers": {
            "iphone": {
                "command": python,
                "args": ["-m", "hidagent.cli", "--url", url, "mcp"],
                "env": {
                    "IPHONE_AGENT_URL": url,
                    "IPHONE_MCP_ENABLE_DELEGATE": "0",
                },
            }
        }
    }
    print("先运行服务（run-local.sh 或 run-server.ps1），再把下面的片段交给 Agent。")
    print("\n# Cursor / Claude Code 的 mcp.json\n")
    print(json.dumps(cursor, ensure_ascii=False, indent=2))
    print("\n# Codex\n")
    print(
        f"codex mcp add iphone --env IPHONE_AGENT_URL={url} "
        "--env IPHONE_MCP_ENABLE_DELEGATE=0 -- "
        f"\"{python}\" -m hidagent.cli mcp"
    )
    print("\n最小任务：让 Agent 把指针移到屏幕中央，核对位置，然后停住。不要只看板子灯或一次 ok。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
