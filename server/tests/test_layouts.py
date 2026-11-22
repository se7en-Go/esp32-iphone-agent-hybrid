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


def test_malformed_layout_is_isolated_and_cwd_skills_is_not_implicitly_trusted(tmp_path, monkeypatch):
    from hidagent.paths import skill_roots

    good = tmp_path / "good"
    good.mkdir()
    (good / "layout.json").write_text(json.dumps({
        "title": "好布局", "points": {"ok": {"x": 0.5, "y": 0.5}},
    }), encoding="utf-8")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "layout.json").write_text("{not-json", encoding="utf-8")
    bad_point = tmp_path / "bad_point"
    bad_point.mkdir()
    (bad_point / "layout.json").write_text(json.dumps({
        "points": {"nan_pt": {"x": "abc", "y": 0.5}},
    }), encoding="utf-8")
    monkeypatch.setenv("HIDAGENT_SKILLS", str(tmp_path))

    found = list_layouts()
    ids = [item["id"] for item in found]
    assert "good" in ids and "bad" not in ids and "bad_point" not in ids
    with pytest.raises(ValueError, match="malformed"):
        resolve_point("bad", "send")
    with pytest.raises(ValueError, match="malformed"):
        resolve_point("bad_point", "nan_pt")

    rogue_cwd = tmp_path / "untrusted"
    (rogue_cwd / "skills").mkdir(parents=True)
    monkeypatch.chdir(rogue_cwd)
    assert (rogue_cwd / "skills") not in skill_roots()
