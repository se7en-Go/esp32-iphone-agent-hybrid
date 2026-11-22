"""skill 目录的位置。核心不把某个 App 的坐标写死，只从这些目录读。"""

import os
from pathlib import Path


def skill_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("HIDAGENT_SKILLS", "").strip()
    if env:
        roots.append(Path(env))
    # server/hidagent/paths.py → 仓库根目录 / skills
    roots.append(Path(__file__).resolve().parents[2] / "skills")
    found: list[Path] = []
    for root in roots:
        if root.is_dir() and root not in found:
            found.append(root)
    return found
