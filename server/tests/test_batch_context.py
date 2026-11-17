import asyncio
import time

import pytest
from fake_clock import ManualClock
from test_controls import FakePhone, Screen, jpeg

from hidagent import feedback, freshness
from hidagent.app import ControlAction
from hidagent.control import Controls


@pytest.fixture(autouse=True)
def instant_mirror(monkeypatch):
    ManualClock().install(monkeypatch)
    monkeypatch.setattr(feedback, "MIRROR_LAG_SECONDS", 0)
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 0)


@pytest.mark.parametrize("evidence", ["missing", "changed", "unchanged"])
@pytest.mark.parametrize("next_action,text", [("type", "draft"), ("key", "enter"), ("point", "")])
def test_batch_stops_after_unverified_coordinate_tap(evidence, next_action, text):
    screen, phone = Screen(), FakePhone()

    async def tap(x, y, **kwargs):
        phone.calls.append(("tap", x, y))
        completed_at = time.time()
        screen.new_frame = evidence != "missing"
        if evidence == "changed":
            screen.frame = jpeg("black")
        screen.updated = time.time()
        return {"clicked": True, "effect_verified": False,
                "screen_changed": None if evidence == "missing" else evidence == "changed",
                "fresh": evidence != "missing", "completed_at": completed_at,
                "_after_frame": screen.frame if evidence != "missing" else None,
                "_after_snapshot": freshness.snapshot(screen) if evidence != "missing" else None}

    phone.tap_verified = tap
    control = Controls(phone, screen)
    token = control.observation()["screen_id"]
    requests = [ControlAction(action="tap", screen_id=token, x=.5, y=.5),
                ControlAction(action=next_action, text=text, screen_id=token, x=.5, y=.5)]
    result = asyncio.run(control.perform_batch(token, requests))
    assert phone.calls == [("tap", .5, .5)]
    assert result["completed"] == 1 and result["stopped_before_step"] == 2
    assert "observe" in result["error"]
    assert result["effect_verified"] is False
    assert result["fresh"] is (evidence != "missing")
    assert result["changed"] is (None if evidence == "missing" else evidence == "changed")
    if evidence == "missing":
        assert result["screen_id"] is None and control.ticket is None
        with pytest.raises(ValueError, match="consumed"):
            asyncio.run(control.perform(ControlAction(action="type", text="draft", screen_id=token)))
        assert phone.calls == [("tap", .5, .5)]


@pytest.mark.parametrize("transition", ["changed", "generation", "stale", "missing"])
def test_batch_revalidates_context_and_fresh_evidence_between_steps(transition):
    screen, phone = Screen(), FakePhone()
    screen.generation = 0

    async def point(x, y):
        phone.calls.append(("point", x, y))
        return x, y

    async def fresh(*args):
        screen.updated = time.time()
        if transition == "changed":
            screen.frame = jpeg("black")
        elif transition == "generation":
            screen.generation += 1
        elif transition == "stale":
            screen.updated -= 20
        elif transition == "missing":
            return None
        return screen.frame

    phone.point_verified, screen.fresh = point, fresh
    control = Controls(phone, screen)
    token = control.observation()["screen_id"]
    requests = [ControlAction(action="point", screen_id=token, x=x, y=.5) for x in (.3, .7)]
    result = asyncio.run(control.perform_batch(token, requests))
    assert phone.calls == [("point", .3, .5)]
    assert result["completed"] == 1 and result["stopped_before_step"] == 2
    assert "observe" in result["error"]


@pytest.mark.parametrize("action,text", [("type", "draft"), ("compose_chinese", "你好"),
                                         ("key", "enter"), ("key", "shift+enter"), ("key", "backspace")])
def test_focus_dependent_step_requires_new_observation(action, text):
    screen, phone = Screen(), FakePhone()
    control = Controls(phone, screen)
    token = control.observation()["screen_id"]
    requests = [ControlAction(action="point", screen_id=token, x=.3, y=.5),
                ControlAction(action=action, text=text, input_mode="pinyin", screen_id=token)]
    result = asyncio.run(control.perform_batch(token, requests))
    assert phone.calls == [("point", .3, .5)]
    assert result["completed"] == 1 and "new observation" in result["error"]


def test_batch_can_continue_with_fresh_unchanged_context_and_no_focus_dependency():
    screen, phone = Screen(), FakePhone()
    control = Controls(phone, screen)
    token = control.observation()["screen_id"]
    requests = [ControlAction(action="point", screen_id=token, x=x, y=.5) for x in (.3, .7)]
    result = asyncio.run(control.perform_batch(token, requests))
    assert phone.calls == [("point", .3, .5), ("point", .7, .5)]
    assert result["completed"] == 2 and result["error"] is None


def test_batch_evidence_error_keeps_one_record_for_the_acknowledged_step():
    screen, phone = Screen(), FakePhone()
    original_fresh = screen.fresh
    failed = False

    async def fresh(*args):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("evidence unavailable; observe again")
        return await original_fresh(*args)

    screen.fresh = fresh
    control = Controls(phone, screen)
    token = control.observation()["screen_id"]
    requests = [ControlAction(action="point", screen_id=token, x=x, y=.5) for x in (.3, .7)]
    result = asyncio.run(control.perform_batch(token, requests))
    assert phone.calls == [("point", .3, .5)]
    assert len(result["steps"]) == result["completed"] == 1
    assert result["steps"][0]["ok"] is True
    assert result["stopped_before_step"] == 2 and result["error"]
