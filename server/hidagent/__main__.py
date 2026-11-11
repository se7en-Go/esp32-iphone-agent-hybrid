import logging
import os
import sys
from pathlib import Path

import uvicorn


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_stdio()
_here = Path(__file__).resolve()
_load_env(_here.parents[2] / ".env")
_load_env(_here.parents[1] / ".env")

debug = os.environ.get("HIDAGENT_DEBUG") == "1"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logging.getLogger("hidagent").setLevel(logging.DEBUG if debug else logging.INFO)

host = os.environ.get("HOST", "127.0.0.1")
port = int(os.environ.get("PORT", "8890"))
print(f"网页控制台：http://{host}:{port}")
print("三个灯 device / screen / calibrated 都绿，才算通。")
print("若系统询问是否允许 Python 访问网络：7201 是连板子的出站连接，选择允许专用网络即可。")
if host not in ("127.0.0.1", "localhost", "::1"):
    print("注意：服务没有只绑在本机。手机画面会经过这个地址，确认这是你自己的电脑。")
uvicorn.run("hidagent.app:app", host=host, port=port)
