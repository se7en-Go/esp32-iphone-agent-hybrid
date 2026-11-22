from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest

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


def test_prepare_send_reserves_quota_before_the_next_enter():
    noon = datetime(2023, 11, 14, 12, 0).timestamp()
    guard = Guard(clock=lambda: noon)
    first = guard.prepare_send("")
    assert first["allowed"] is True and guard.sends == [noon]
    assert guard.prepare_send("")["allowed"] is False
    assert guard.prepare_send("")["allowed"] is False
    assert guard.sends == [noon]


def test_guard_json_null_tripped_and_nan_fail_closed(tmp_path):
    path = tmp_path / "guard.json"
    path.write_text('{"tripped": null, "sends": []}', encoding="utf-8")
    assert Guard(path=path).tripped
    path.write_text('{"tripped": false, "sends": [NaN]}', encoding="utf-8")
    assert Guard(path=path).tripped
    path.write_text('{"tripped": false, "sends": [1e309]}', encoding="utf-8")
    assert Guard(path=path).tripped
    path.write_text('{"tripped": false, "sends": [1000.0]}', encoding="utf-8")
    healthy = Guard(path=path)
    assert healthy.tripped is False and healthy.sends == [1000.0]


def test_reset_clears_the_trip_and_keeps_hourly_sends(tmp_path):
    noon = datetime(2023, 11, 14, 12, 0).timestamp()
    path = tmp_path / "guard.json"
    guard = Guard(clock=lambda: noon, path=path)
    assert guard.prepare_send("")["allowed"] is True
    guard.tripped = True
    guard.reason = "验证码"
    guard._save()
    guard.reset()
    assert guard.tripped is False and guard.sends == [noon]
    again = Guard(clock=lambda: noon, path=path)
    assert again.tripped is False and again.sends == [noon]
    assert again.prepare_send("")["allowed"] is False


def test_concurrent_consumers_share_only_one_reservation():
    now = [datetime(2023, 11, 14, 12, 0).timestamp()]
    guard = Guard(clock=lambda: now[0])
    assert guard.prepare_send()["allowed"]
    now[0] += 9
    ready = Barrier(2)

    def consume():
        ready.wait(timeout=5)
        return guard.consume_reservation()["allowed"]

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: consume(), range(2)))
    assert sum(results) == 1
    assert guard.sends == [now[0]]


def test_failed_reset_keeps_trip_and_quota(tmp_path, monkeypatch):
    path = tmp_path / "guard.json"
    noon = datetime(2023, 11, 14, 12).timestamp()
    guard = Guard(clock=lambda: noon, path=path)
    assert guard.consume_reservation()["allowed"]
    assert guard.observe("验证码")["stop"]

    def failed_replace(*_args):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr("hidagent.guard.os.replace", failed_replace)
        with pytest.raises(OSError, match="disk unavailable"):
            guard.reset()
    assert guard.tripped and guard.reason == "验证码"
    assert guard.sends == [noon]
    assert Guard(path=path).tripped and Guard(path=path).sends == [noon]
    guard._save()
    assert Guard(path=path).tripped
    guard.reset()
    assert not Guard(path=path).tripped
    assert Guard(path=path).sends == [noon]


def test_reload_cannot_clear_an_unpersisted_trip(tmp_path, monkeypatch):
    path = tmp_path / "guard.json"
    guard = Guard(path=path)
    guard._save()

    def failed_replace(*_args):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr("hidagent.guard.os.replace", failed_replace)
        with pytest.raises(OSError, match="disk unavailable"):
            guard.observe("风控")
    assert guard.tripped
    guard._load()
    assert guard.tripped and guard.reason == "风控"
    guard._save()
    assert Guard(path=path).tripped


def test_malformed_skill_frontmatter_falls_back_to_safe_defaults(tmp_path, monkeypatch):
    from hidagent.guard import limits_for

    skill = tmp_path / "broken"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nmin_interval_seconds: abc\nmax_per_hour: nope\nquiet_hours: 25:99-07:00\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HIDAGENT_SKILLS", str(tmp_path))
    limits = limits_for("broken")
    assert limits["min_interval"] == 8.0
    assert limits["max_per_hour"] == 20
    assert limits["quiet"] is True
    assert limits["quiet_start"] == (23, 0) and limits["quiet_end"] == (7, 0)


def test_trace_start_creates_unique_dirs_within_same_second(tmp_path, monkeypatch):
    from hidagent.debug import Trace

    monkeypatch.setattr("hidagent.debug.time.strftime", lambda _fmt: "20261122-120000")
    t1 = Trace(tmp_path)
    t1.start("task-1")
    t2 = Trace(tmp_path)
    t2.start("task-2")
    assert t1.dir != t2.dir
    assert (t1.dir / "steps.jsonl").exists() and (t2.dir / "steps.jsonl").exists()


def test_agent_parse_command_rejects_out_of_range_plain_text_coords():
    from hidagent.agent import parse_command

    assert parse_command("tap 500 500") == {"action": "tap", "x": 500, "y": 500}
    with pytest.raises(ValueError, match="0..1000"):
        parse_command("tap 1200 500")
    with pytest.raises(ValueError, match="0..1000"):
        parse_command("tap -10 500")
    with pytest.raises(ValueError, match="0..1000"):
        parse_command("path 100 100 1500 200")
    with pytest.raises(ValueError, match="positive"):
        parse_command("circle 500 500 0")


def test_env_numeric_parsers_fall_back_safely(monkeypatch):
    from hidagent.client import IPhoneClient
    from hidagent.screen import Screen

    monkeypatch.setenv("VIDEO_FPS", "not-a-number")
    assert Screen().fps == 8
    monkeypatch.setenv("VIDEO_FPS", "999")
    assert Screen().fps == 60
    monkeypatch.setenv("IPHONE_AGENT_TIMEOUT", "abc")
    with IPhoneClient("http://127.0.0.1:8890") as client:
        assert client.http.timeout.read == 60.0


def test_screen_can_restart_after_stop(monkeypatch):
    from hidagent.screen import Screen

    screen = Screen()
    screen.start()
    first_thread = screen._thread
    assert first_thread is not None and screen._stopping is False
    screen.stop()
    assert screen._thread is None and screen._stopping is True
    screen.start()
    assert screen._thread is not None and screen._thread is not first_thread and screen._stopping is False
    screen.stop()
