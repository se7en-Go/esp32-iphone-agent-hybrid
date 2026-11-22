"""从 skill 目录读取命名坐标。核心不知道微信的发送键在哪。"""

from __future__ import annotations

import json
import logging
import math

from .paths import skill_roots

log = logging.getLogger(__name__)


def _parse_point(value) -> tuple[float, float]:
    if not isinstance(value, dict):
        raise ValueError("layout point must be an object")
    x = float(value["x"])
    y = float(value["y"])
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError("layout point coordinates must be finite numbers")
    return x, y


def list_layouts() -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    for root in skill_roots():
        for path in sorted(root.glob("*/layout.json")):
            name = path.parent.name
            if name in seen:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("layout root must be an object")
                points = data.get("points") or {}
                if not isinstance(points, dict):
                    raise ValueError("layout points must be an object")
                parsed_points = []
                for key, value in points.items():
                    x, y = _parse_point(value)
                    parsed_points.append({
                        "name": str(key), "x": x, "y": y,
                        "note": str(value.get("note", "")) if value.get("note") is not None else "",
                    })
                found.append({
                    "id": name,
                    "title": str(data.get("title") or name),
                    "points": parsed_points,
                })
                seen.add(name)
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                log.warning("ignoring malformed layout %s: %s: %s", path, type(exc).__name__, exc)
    return found


def resolve_point(layout_id: str, point: str) -> tuple[float, float]:
    if "/" in layout_id or "\\" in layout_id or layout_id.startswith("."):
        raise ValueError("unknown layout")
    for root in skill_roots():
        path = root / layout_id / "layout.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"malformed layout {layout_id}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"malformed layout {layout_id}")
        points = data.get("points") or {}
        if not isinstance(points, dict):
            raise ValueError(f"malformed layout {layout_id}")
        item = points.get(point)
        if not item:
            known = ", ".join(points.keys())
            raise ValueError(f"layout {layout_id} has no point {point!r} (known: {known})")
        try:
            return _parse_point(item)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"malformed point {point!r} in layout {layout_id}") from exc
    raise ValueError(f"unknown layout {layout_id}")
