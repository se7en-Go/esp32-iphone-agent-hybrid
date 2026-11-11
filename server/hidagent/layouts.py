"""从 skill 目录读取命名坐标。核心不知道微信的发送键在哪。"""

from __future__ import annotations

import json

from .paths import skill_roots


def list_layouts() -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    for root in skill_roots():
        for path in sorted(root.glob("*/layout.json")):
            name = path.parent.name
            if name in seen:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            points = data.get("points") or {}
            found.append({
                "id": name,
                "title": data.get("title") or name,
                "points": [
                    {"name": key, "x": float(value["x"]), "y": float(value["y"]),
                     "note": value.get("note", "")}
                    for key, value in points.items()
                ],
            })
            seen.add(name)
    return found


def resolve_point(layout_id: str, point: str) -> tuple[float, float]:
    if "/" in layout_id or "\\" in layout_id or layout_id.startswith("."):
        raise ValueError("unknown layout")
    for root in skill_roots():
        path = root / layout_id / "layout.json"
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        item = (data.get("points") or {}).get(point)
        if not item:
            known = ", ".join((data.get("points") or {}).keys())
            raise ValueError(f"layout {layout_id} has no point {point!r} (known: {known})")
        return float(item["x"]), float(item["y"])
    raise ValueError(f"unknown layout {layout_id}")
