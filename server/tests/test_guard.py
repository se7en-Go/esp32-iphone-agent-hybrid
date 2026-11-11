from datetime import datetime

from hidagent.guard import FLOOR_INTERVAL, Guard, in_quiet, limits_for


def test_builtin_risk_words_trip_and_stay_tripped():
    guard = Guard()
    first = guard.observe("系统提示：操作频繁，请稍后再试", "wechat-group-reply")
    assert first["stop"] and "不要重试" in first["reason"]
    second = guard.observe("普通的一句话", "wechat-group-reply")
    assert second["stop"]
    send = guard.confirm_send(bubble_visible=True, observed_text="", skill="wechat-group-reply")
    assert send["stop"] and send["counted"] is False


def test_send_counts_only_when_the_bubble_is_visible():
    clock = {"now": 1_700_000_000.0}  # 2023-11-14 22:13 UTC，具体本地小时无关，下面关掉静默
    guard = Guard(clock=lambda: clock["now"])
    hidden = guard.confirm_send(bubble_visible=False, observed_text="谢谢", skill="")
    assert hidden["counted"] is False and hidden["allowed"] is False
    # 默认 23:00-07:00 静默。用中午的时间戳避免踩到静默。
    noon = datetime(2023, 11, 14, 12, 0, 0).timestamp()
    clock["now"] = noon
    shown = guard.confirm_send(bubble_visible=True, observed_text="谢谢", skill="")
    assert shown["counted"] is True and shown["allowed"] is True
    too_soon = guard.confirm_send(bubble_visible=True, observed_text="谢谢", skill="")
    assert too_soon["counted"] is False and "秒" in too_soon["reason"]


def test_skill_cannot_loosen_the_floor(tmp_path, monkeypatch):
    skill = tmp_path / "fast"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nmin_interval_seconds: 1\nmax_per_hour: 100\nquiet_hours: off\nextra_stop: 账号异常\n---\n随便写\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HIDAGENT_SKILLS", str(tmp_path))
    limits = limits_for("fast")
    assert limits["min_interval"] == FLOOR_INTERVAL
    assert limits["max_per_hour"] == 30
    assert limits["quiet"] is False
    guard = Guard(clock=lambda: datetime(2023, 11, 14, 15, 0).timestamp())
    tripped = guard.observe("账号异常", "fast")
    assert tripped["stop"]


def test_trip_is_still_there_after_a_new_guard(tmp_path):
    path = tmp_path / "guard.json"
    first = Guard(path=path)
    assert first.observe("系统：操作频繁", "")["stop"]
    second = Guard(path=path)
    assert second.tripped and second.reason == "操作频繁"
    assert second.observe("普通的一句", "")["stop"]
    second.reset()
    third = Guard(path=path)
    assert not third.tripped
    assert third.observe("普通的一句", "")["stop"] is False


def test_prepare_send_enforces_quiet_hours_before_counting():
    night = datetime(2023, 11, 14, 23, 30).timestamp()
    guard = Guard(clock=lambda: night)
    decision = guard.prepare_send("")
    assert decision["allowed"] is False and guard.sends == []


def test_corrupt_guard_file_fails_closed_and_sends_survive_restart(tmp_path):
    path = tmp_path / "guard.json"
    path.write_text("{", encoding="utf-8")
    broken = Guard(path=path)
    assert broken.tripped and broken.prepare_send("")["allowed"] is False
    path.write_text("[]", encoding="utf-8")
    assert Guard(path=path).tripped
    noon = datetime(2023, 11, 14, 12, 0).timestamp()
    fresh = tmp_path / "ok.json"
    first = Guard(clock=lambda: noon, path=fresh)
    assert first.prepare_send("")["allowed"] is True
    assert first.confirm_send(bubble_visible=True, observed_text="", skill="")["counted"] is True
    second = Guard(clock=lambda: noon, path=fresh)
    assert second.sends == [noon]
    assert second.prepare_send("")["allowed"] is False


def test_quiet_hours_wrap_midnight():
    assert in_quiet(datetime(2023, 11, 14, 23, 30), (23, 0), (7, 0))
    assert in_quiet(datetime(2023, 11, 14, 6, 0), (23, 0), (7, 0))
    assert not in_quiet(datetime(2023, 11, 14, 12, 0), (23, 0), (7, 0))
