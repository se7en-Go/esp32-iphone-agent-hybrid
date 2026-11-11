import json

import pytest

from hidagent.layouts import list_layouts, resolve_point


def test_layout_points_come_from_the_skill_directory(tmp_path, monkeypatch):
    skill = tmp_path / "demo"
    skill.mkdir()
    (skill / "layout.json").write_text(json.dumps({
        "title": "示例",
        "points": {"send": {"x": 0.9, "y": 0.93, "note": "发送"}},
    }), encoding="utf-8")
    monkeypatch.setenv("HIDAGENT_SKILLS", str(tmp_path))
    found = list_layouts()
    assert found[0]["id"] == "demo"
    assert found[0]["points"][0]["name"] == "send"
    assert resolve_point("demo", "send") == (0.9, 0.93)
    with pytest.raises(ValueError, match="unknown layout"):
        resolve_point("../secrets", "send")
