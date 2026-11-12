import asyncio
import time
from types import SimpleNamespace

import pytest
from test_controls import Screen, jpeg

from hidagent import control, freshness, ocr
from hidagent import phone as phone_module
from hidagent.app import ControlAction
from hidagent.control import Controls
from hidagent.phone import Phone


@pytest.mark.parametrize("action", ["tap", "tap_text"])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("later_evidence", [False, True])
def test_feedback_rejects_pointer_frames_received_before_delayed_tap_result(
        tmp_path, monkeypatch, action, batch, later_evidence):
    clock = SimpleNamespace(now=1000.)
    commands, waits = [], []
    before, result_frame = jpeg(), jpeg("black")

    class DelayedMirror(Screen):
        updated = clock.now
        frame = before

        def __init__(self):
            self.pending = []

        async def fresh(self, since, timeout):
            waits.append(since)
            deadline = clock.now + timeout
            while self.updated <= since:
                if not self.pending or self.pending[0][0] > deadline:
                    clock.now = deadline
                    return None
                clock.now, self.frame = self.pending.pop(0)
                self.updated = clock.now
            return self.frame

    screen = DelayedMirror()

    async def send(command):
        commands.append((command, clock.now))
        # The first arriving frame was captured before the click. Its result is one second late.
        screen.pending = [(clock.now + .3, before), (clock.now + 1., result_frame)]
        if later_evidence:
            screen.pending.append((clock.now + 1.8, result_frame))

    async def point(*_):
        clock.now += 5  # real positioning can take seconds
        screen.updated = clock.now - .1
        phone._pointer_confirmed = True
        return .5, .5

    fake_time = SimpleNamespace(time=lambda: clock.now, monotonic=time.monotonic, perf_counter=time.perf_counter)
    monkeypatch.setattr(control, "time", fake_time)
    monkeypatch.setattr(freshness, "time", fake_time)
    monkeypatch.setattr(phone_module, "time", fake_time)
    monkeypatch.setattr(phone_module, "TAP_GAP", 0)
    monkeypatch.setattr(ocr, "find_matches", lambda *_: [ocr.TextBox("设置", (.4, .4, .6, .6), (.5, .5), .99)])
    phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration", screen)
    monkeypatch.setattr(phone, "point_verified", point)
    controls = Controls(phone, screen)
    token = controls.observation()["screen_id"]
    request = ControlAction(action=action, screen_id=token, text="设置", x=.5, y=.5, ocr_diff=False)
    response = asyncio.run(controls.perform_batch(token, [request], ocr_diff=False) if batch
                           else controls.perform(request))
    assert commands == [("click", 1005.)]
    assert waits[-1] >= 1006.7  # actual click completion plus the known mirror lag bound
    assert response["fresh"] is later_evidence
    assert response["changed"] is (True if later_evidence else None)
    assert response["screen_diff"]["status"] == ("ok" if later_evidence else "stale")


@pytest.mark.parametrize("batch", [False, True])
def test_text_tap_rechecks_context_after_real_phone_positioning(tmp_path, monkeypatch, batch):
    commands = []
    screen = Screen()

    async def send(command):
        commands.append(command)

    async def point(*_):
        screen.frame = jpeg("black")
        phone._pointer_confirmed = True
        return .5, .5

    monkeypatch.setattr(ocr, "find_matches", lambda *_: [ocr.TextBox("设置", (.4, .4, .6, .6), (.5, .5), .99)])
    phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration", screen)
    monkeypatch.setattr(phone, "point_verified", point)
    controls = Controls(phone, screen)
    token = controls.observation()["screen_id"]
    request = ControlAction(action="tap_text", screen_id=token, text="设置", ocr_diff=False)
    result = asyncio.run(controls.perform_batch(token, [request], ocr_diff=False) if batch
                         else controls.perform(request))
    assert commands == []
    assert result["command_acknowledged"] is False
    assert "未发送点击" in result["error"]
