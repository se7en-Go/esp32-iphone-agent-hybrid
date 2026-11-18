import asyncio
import base64
import io
import math
import time
from types import SimpleNamespace

import httpx
import pytest
from fake_clock import ManualClock
from PIL import Image

from hidagent import feedback, freshness
from hidagent.app import ControlAction
from hidagent.control import Controls
from hidagent.phone import TYPE_LIMIT, Phone


def jpeg(color="white"):
    buffer = io.BytesIO()
    Image.new("RGB", (101, 201), color).save(buffer, "JPEG")
    return buffer.getvalue()


class Screen:
    live = True
    def __init__(self):
        self.updated = time.time()
    frame = jpeg()
    new_frame = True

    async def fresh(self, *args):
        if not self.new_frame:
            return None
        # A delivered frame is a later event, even on a coarse Windows wall clock.
        await asyncio.sleep(1 / 30)
        self.updated = time.time()
        return self.frame
    async def settled(self, **kwargs): pass


@pytest.fixture(autouse=True)
def instant_mirror(monkeypatch, request):
    # These fake screens publish the next frame without mirror lag; latency tests use paced fakes.
    if "delayed_pointer_frame" not in request.node.name:
        clock = ManualClock()
        clock.install(monkeypatch)
        monkeypatch.setattr(feedback, "MIRROR_LAG_SECONDS", 0)
        monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 0)
        return clock


class FakePhone:
    def __init__(self): self.calls = []
    async def point_verified(self, x, y):
        self.calls.append(("point", x, y))
        return x, y
    async def tap_verified(self, x, y, *, before_click=None, target_box=None, target_frame=None):
        if before_click is not None:
            before_click()
        self.calls.append(("tap", x, y))
    async def type(self, text, mode="auto", *, before_action=None):
        if before_action is not None:
            before_action()
        self.calls.append(("type", text))
    async def key(self, text, *, before_action=None):
        if before_action is not None:
            before_action()
        self.calls.append(("key", text))


def test_long_ascii_is_split_under_the_firmware_line_limit(tmp_path):
    sent = []

    async def send(command):
        assert len(command) <= TYPE_LIMIT + len("type ")
        sent.append(command)

    phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration.json", None)
    asyncio.run(phone.type("x" * 1000))
    assert len(sent) == 3
    assert "".join(part.removeprefix("type ") for part in sent) == "x" * 1000


def test_chunk_preserves_space_that_firmware_trim_would_drop(tmp_path):
    text = "x" * 479 + " " + "y" * 21
    sent = []

    async def send(command):
        sent.append(command)

    phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration.json", None)
    asyncio.run(phone.type(text))

    def firmware_typed(command: str) -> str:
        line = f"7 {command}".strip()
        return line.split(" ", 2)[2]

    assert "".join(firmware_typed(command) for command in sent) == text
    assert all(not firmware_typed(command).endswith(" ") for command in sent)


def test_all_space_run_is_typed_exactly(tmp_path):
    text = "x" + " " * 480 + "y"
    sent = []

    async def send(command):
        sent.append(command)

    phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration.json", None)
    asyncio.run(phone.type(text))

    def firmware_typed(command: str) -> str:
        line = f"7 {command}".strip()
        return line.split(" ", 2)[2] if line.count(" ") >= 2 else ""

    assert "".join(firmware_typed(command) for command in sent) == text


def test_pixel_coordinates_return_evidence_and_single_use_token():
    phone, screen = FakePhone(), Screen()
    control = Controls(phone, screen)
    token = control.observation()["screen_id"]
    request = ControlAction(action="tap", screen_id=token, coordinate_space="pixels", x=50, y=100)
    result = asyncio.run(control.perform(request))
    assert phone.calls == [("tap", .5, .5)]
    assert result["command_acknowledged"] and result["fresh"]
    assert result["effect_verified"] is False
    assert base64.b64decode(result["image_base64"]) == screen.frame
    assert result["screen_id"] != token
    with pytest.raises(ValueError, match="consumed"):
        asyncio.run(control.perform(request))
    assert len(phone.calls) == 1


@pytest.mark.parametrize("quantum", [.015625, .001])
def test_post_action_evidence_with_coarse_wall_clock(monkeypatch, instant_mirror, quantum):
    clock = instant_mirror
    monkeypatch.setattr(time, "time", lambda: math.floor(clock.time() / quantum) * quantum)
    phone, screen = FakePhone(), Screen()
    control = Controls(phone, screen)
    token = control.observation()["screen_id"]
    completed_at = time.time()
    result = asyncio.run(control.perform(ControlAction(action="tap", screen_id=token, x=.5, y=.5)))
    assert phone.calls == [("tap", .5, .5)]
    assert screen.updated > completed_at
    assert result["fresh"] is True and result["changed"] is False
    assert result["screen_diff"]["changed_fraction"] == 0
    assert result["screen_id"] and result["screen_id"] != token


@pytest.mark.parametrize("reason", ["expired", "changed", "normalized", "pixels"])
def test_bad_observations_and_coordinates_never_reach_hid(reason):
    phone, screen = FakePhone(), Screen()
    control = Controls(phone, screen)
    token = control.observation()["screen_id"]
    space, x = "normalized", .5
    if reason == "expired":
        t = control.ticket
        control.ticket = (t[0], time.monotonic() - 31, *t[2:])
    elif reason == "changed": screen.frame = jpeg("black")
    elif reason == "normalized": x = 500
    elif reason == "pixels": space, x = "pixels", 101
    with pytest.raises(ValueError):
        asyncio.run(control.perform(ControlAction(action="tap", screen_id=token,
                                                 coordinate_space=space, x=x, y=.5)))
    assert not phone.calls


def test_chinese_is_composition_only_and_requires_mode_confirmation():
    phone, screen = FakePhone(), Screen()
    control = Controls(phone, screen)
    token = control.observation()["screen_id"]
    with pytest.raises(ValueError, match="Pinyin"):
        asyncio.run(control.perform(ControlAction(action="compose_chinese", screen_id=token, text="谢谢分享")))
    token = control.observation()["screen_id"]
    result = asyncio.run(control.perform(ControlAction(action="compose_chinese", screen_id=token,
                                                       text="谢谢分享", input_mode="pinyin")))
    assert phone.calls == [("type", "xiexiefenxiang")]
    assert result["expected_text"] == "谢谢分享"
    assert result["requires_candidate_selection"] and result["submitted"] is False


@pytest.mark.parametrize("action,text", [("type", "你好"), ("type", "hello\n"),
                                         ("compose_chinese", "你好!"), ("key", "enter\nhome")])
def test_invalid_keyboard_input_cannot_send_protocol_lines(action, text):
    phone, screen = FakePhone(), Screen()
    control = Controls(phone, screen)
    with pytest.raises(ValueError):
        asyncio.run(control.perform(ControlAction(action=action, screen_id=control.observation()["screen_id"],
                                                 text=text, input_mode="pinyin")))
    assert not phone.calls


def test_uncertain_input_is_not_retried_and_unchanged_frame_is_not_verified():
    phone, screen = FakePhone(), Screen()
    screen.new_frame = False

    async def failing(text, **kwargs):
        phone.calls.append(("key", text))
        raise RuntimeError("connection lost after write")

    phone.key = failing
    control = Controls(phone, screen)
    result = asyncio.run(control.perform(ControlAction(action="key", text="enter",
                                                       screen_id=control.observation()["screen_id"])))
    assert phone.calls == [("key", "enter")]
    assert not result["command_acknowledged"] and not result["fresh"]
    assert result["changed"] is None and not result["effect_verified"]
    assert "connection lost" in result["error"] and result["action_may_have_executed"]


def test_repeated_edit_keys_return_one_observation_and_phase_timings():
    phone = FakePhone()
    control = Controls(phone, Screen())
    result = asyncio.run(control.perform(ControlAction(action="key", text="backspace", repeat=4,
                                                       screen_id=control.observation()["screen_id"])))
    assert phone.calls == [("key", "backspace")] * 4
    assert result["repeat_completed"] == result["repeat_requested"] == 4
    assert result["screen_id"] and result["image_base64"]
    assert set(result["timing_ms"]) == {"validation", "device_and_pointer", "wait_fresh",
                                       "wait_settled", "capture", "total"}
    assert all(value >= 0 for value in result["timing_ms"].values())


@pytest.mark.parametrize("key", ["enter", "space", "cmd+backspace"])
def test_repeat_cannot_batch_sends_or_other_actions(key):
    phone = FakePhone()
    control = Controls(phone, Screen())
    with pytest.raises(ValueError, match="never repeat Enter"):
        asyncio.run(control.perform(ControlAction(action="key", text=key, repeat=2,
                                                 screen_id=control.observation()["screen_id"])))
    assert not phone.calls


def test_partial_repeat_failure_stops_without_replaying():
    phone = FakePhone()

    async def fail_second(key, **kwargs):
        phone.calls.append(("key", key))
        if len(phone.calls) == 2:
            raise RuntimeError("lost ACK; key may have executed")

    phone.key = fail_second
    control = Controls(phone, Screen())
    result = asyncio.run(control.perform(ControlAction(action="key", text="backspace", repeat=5,
                                                       screen_id=control.observation()["screen_id"])))
    assert len(phone.calls) == 2
    assert result["repeat_completed"] == 1 and not result["command_acknowledged"]
    assert result["error"] and result["action_may_have_executed"]


def test_verified_tap_refuses_unmeasured_drift_or_unseen_pointer(tmp_path):
    commands = []

    async def send(command): commands.append(command)

    async def scenario(found):
        phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration", Screen())
        phone.span, phone._pos = {"w": 400, "h": 800}, (200, 400)
        async def point(x, y): pass
        phone.point = point
        positions = iter(found)
        async def locate(near, **kwargs): return next(positions)
        phone._find_pointer = locate
        return await phone.tap_verified(.5, .5)

    with pytest.raises(RuntimeError, match="未发送点击"):
        asyncio.run(scenario([(.55, .45), (.5, .5)]))
    assert commands == ["move -20 40"]
    commands.clear()
    with pytest.raises(RuntimeError, match="未发送点击"):
        asyncio.run(scenario([None]))
    assert commands == []


@pytest.mark.parametrize("landing", [None, (.505, .497)], ids=["unconfirmed-correction", "confirmed-correction"])
def test_point_reports_final_landing_not_an_earlier_pointer_sighting(tmp_path, monkeypatch, landing):
    commands = []

    async def send(command):
        commands.append(command)

    screen = Screen()
    device = SimpleNamespace(send=send, links=0, abort_epoch=0, pace_version=4)
    phone = Phone(device, tmp_path / "calibration.json", screen)
    phone.span, phone._pos = {"w": 400, "h": 800}, (0, 0)
    phone._pace, phone._link = 4, 0

    async def first_sighting(*_args, **_kwargs):
        phone._pointer_pair_verified = True
        return .54, .5  # measured, but outside the requested final landing

    async def after_correction(*_args, **_kwargs):
        return landing

    monkeypatch.setattr(phone, "_find_pointer", first_sighting)
    monkeypatch.setattr(phone, "_observe_landing", after_correction)
    monkeypatch.setattr(phone, "_localize_pointer", after_correction)
    control = Controls(phone, screen)
    result = asyncio.run(control.perform(ControlAction(
        action="point", x=.5, y=.5, screen_id=control.observation()["screen_id"])))

    assert commands == ["move 200 400", "move -16 0"]  # motion only; never click/press
    assert phone._pointer_seen  # an earlier sighting does not confirm the correction
    assert phone._pointer_confirmed is (landing is not None)
    assert result["command_acknowledged"] and result["error"] is None
    assert result["pointer_seen"] is (landing is not None)
    expected = landing or (.5, .5)
    assert result["pointer"] == {"x": expected[0], "y": expected[1]}
    assert result["measured_pointer"] == ({"x": landing[0], "y": landing[1]} if landing is not None else None)
    assert (phone._pos is None) is (landing is None)


def test_unconfirmed_point_rehomes_next_move_and_explicit_rehome_forces_origin(tmp_path, monkeypatch):
    commands = []

    async def send(command):
        commands.append(command)

    screen = Screen()
    device = SimpleNamespace(send=send, links=0, abort_epoch=0, pace_version=4)
    phone = Phone(device, tmp_path / "calibration.json", screen)
    phone.span, phone._pos = {"w": 400, "h": 800}, (100, 100)
    phone._pace, phone._link = 4, 0

    async def miss(*_args, **_kwargs):
        return None

    async def hit(near, *_args, **_kwargs):
        phone._pointer_pair_verified = True
        return near

    monkeypatch.setattr(phone, "_find_pointer", miss)
    control = Controls(phone, screen)
    first = asyncio.run(control.perform(ControlAction(
        action="point", x=.45, y=.96, screen_id=control.observation()["screen_id"])))
    assert first["measured_pointer"] is None
    assert phone._pos is None

    monkeypatch.setattr(phone, "_find_pointer", hit)
    second = asyncio.run(control.perform(ControlAction(
        action="point", x=.25, y=.92, target_box=(.13, .90, .76, .95),
        screen_id=control.observation()["screen_id"])))
    assert second["pointer_seen"] is True and second["measured_pointer"] == {"x": .25, "y": .92}
    assert commands[1:3] == ["home", "move 100 736"]
    assert phone._pos == (100, 736)

    commands.clear()
    third = asyncio.run(control.perform(ControlAction(
        action="point", x=.5, y=.5, rehome=True, target_box=(.4, .4, .6, .6),
        screen_id=control.observation()["screen_id"])))
    assert third["pointer_seen"] is True
    assert commands == ["home", "move 200 400"]


def test_verified_tap_checks_once_before_using_calibrated_position(tmp_path):
    async def scenario():
        commands, attempts = [], []
        async def send(command): commands.append(command)
        phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration", Screen())
        phone._pos = (200, 400)
        async def point(x, y): pass
        async def locate(near, quick=False, **kwargs):
            attempts.append(quick)
            return None
        phone.point, phone._find_pointer = point, locate
        with pytest.raises(RuntimeError, match="未发送点击"):
            await phone.tap_verified(.5, .5)
        assert attempts == [True]
        assert commands == []
    asyncio.run(scenario())


def test_cold_pointer_initialization_uses_bounded_pointer_check(tmp_path):
    async def scenario():
        attempts = []
        async def send(command): pass
        phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration", Screen())
        phone.span = {"w": 400, "h": 800}
        async def point(x, y): phone._pos = (200, 400)
        async def locate(near, quick=False, **kwargs):
            attempts.append(quick)
            return (.5, .5)
        phone.point, phone._find_pointer = point, locate
        await phone.tap_verified(.5, .5)
        assert attempts == [True]
    asyncio.run(scenario())


def test_pointer_nudge_waits_for_its_visual_effect_not_an_older_pipeline_frame(tmp_path):
    from test_vision import H, W
    from test_vision import screen as pointer_frame

    async def scenario():
        commands, frames = [], []
        before = pointer_frame((250, 500))
        after = pointer_frame((262, 500))

        class DelayedScreen:
            live = True
            frame = before
            updated = time.time()
            async def settled(self, **kwargs): pass
            async def fresh(self, since, timeout):
                self.updated = time.time()
                frames.append(self.updated)
                self.frame = before if len(frames) == 1 else after
                return self.frame

        async def send(command): commands.append((command, len(frames)))
        phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration", DelayedScreen())
        phone.span = {"w": W, "h": H}
        found = await phone._find_pointer((250 / W, 500 / H))
        assert found and found[0] * W == pytest.approx(250, abs=3)
        assert commands == [("move 12 0", 0), ("move -12 0", 2)]
    asyncio.run(scenario())


def test_emergency_stop_cancels_new_action_without_ticket_or_waiting_for_lock(monkeypatch):
    from hidagent import app as module

    async def scenario():
        phone, screen = FakePhone(), Screen()
        control = Controls(phone, screen)
        entered = asyncio.Event()
        async def stuck(x, y, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        phone.tap_verified = stuck
        monkeypatch.setattr(module, "controls", control)
        monkeypatch.setattr(module, "manual_lock", asyncio.Lock())
        monkeypatch.setattr(module, "agent", SimpleNamespace(running=False, stop=lambda: None))
        monkeypatch.setattr(module, "device", SimpleNamespace(connected=False))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://local") as client:
            observation = (await client.get("/api/control/observe")).json()
            pending = asyncio.create_task(client.post("/api/control/action", json={
                "action": "tap", "x": .5, "y": .5, "screen_id": observation["screen_id"]}))
            await entered.wait()
            assert (await client.get("/api/control/observe")).status_code == 409
            result = await asyncio.wait_for(client.post("/api/agent/stop", json={}), 1)
            assert result.status_code == 200
            assert (await pending).status_code == 409
            assert control.ticket is None
    asyncio.run(scenario())
