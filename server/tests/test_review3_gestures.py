"""Gesture evidence must survive positioning and waiting for the actual HID write."""

import asyncio
import io
import time

import pytest
from PIL import Image, ImageDraw
from test_assistive_fallback import Device as FakeDevice
from test_assistive_fallback import Screen as FakeScreen
from test_assistive_fallback import app_frame, make_phone
from test_verified_tap import LaggingMirror, calibrated_phone, disc_frame

from hidagent import assistive, freshness, screen_compare
from hidagent import phone as phone_module
from hidagent.device import Device
from hidagent.phone import Phone
from hidagent.screen import Screen


@pytest.mark.parametrize("lost", ["disconnect", "generation", "replay"])
def test_home_lost_during_positioning_never_presses_or_drags(tmp_path, monkeypatch, lost):
    monkeypatch.setattr(assistive, "RESULT_WAIT", .01)
    screen = FakeScreen()

    def change(command):
        if command.startswith("move "):
            if lost == "disconnect":
                screen.live = False
            elif lost == "generation":
                screen.generation = 1
            else:
                screen.frame_ts = (screen.updated - 10) * 1000

    device = FakeDevice(change)
    phone = make_phone(tmp_path, device, screen)
    with pytest.raises(RuntimeError, match="新鲜|连接"):
        asyncio.run(phone.go_home())
    assert "press" not in device.sent
    assert sum(command.startswith("move ") for command in device.sent) == 1
    assert "homescreen" not in device.sent and "key cmd+h" not in device.sent


class Link:
    def __init__(self):
        self.written = []
        self.replies = asyncio.Queue()

    def write(self, data):
        self.written.append(data.decode().split(" ", 1)[1].strip())
        self.replies.put_nowait(data.split(b" ", 1)[0] + b" ok\n")

    async def readline(self):
        return await self.replies.get()

    def is_open(self):
        return True

    def close(self):
        pass


@pytest.mark.parametrize("gesture", ["swipe", "drag", "scroll", "back"])
@pytest.mark.parametrize("transition", ["content", "generation", "disconnect"])
def test_gesture_revalidates_at_write_after_device_lock_wait(tmp_path, gesture, transition):
    async def run():
        screen, device, link = Screen(), Device(), Link()
        frame = app_frame()
        screen._on_jpeg(frame)
        device.attach(link)
        phone = Phone(device, tmp_path / "calibration.json", screen)
        phone.span = {"w": 360, "h": 780}
        phone._link, phone._pos = device.links, (0, 0)
        original_point = phone.point

        async def point_then_block(*args, **kwargs):
            await original_point(*args, **kwargs)
            await device._lock.acquire()

        phone.point = point_then_block
        operation = {"swipe": lambda: phone.swipe(.5, .9, .5, .6, hold=False),
                     "drag": lambda: phone.drag([(.5, .9), (.5, .6)], hold=False),
                     "scroll": lambda: phone.scroll(.5, .9, -3),
                     "back": phone.go_back}[gesture]
        task = asyncio.create_task(operation())
        try:
            async def waiting():
                while not device._lock._waiters:
                    await asyncio.sleep(0)
            await asyncio.wait_for(waiting(), 1)
            if transition == "content":
                screen._on_jpeg(app_frame(home=True))
            elif transition == "generation":
                screen._on_ready()
                screen._on_jpeg(frame)
            else:
                screen._started, screen._session_ok = True, False
            device._lock.release()
            with pytest.raises(RuntimeError, match="新鲜|连接|屏幕"):
                await task
            assert "press" not in link.written
            assert not any(command.startswith("scroll ") for command in link.written)
            assert sum(command.startswith("move ") for command in link.written) == 1
        finally:
            if device._lock.locked():
                device._lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()
    asyncio.run(run())


def test_successful_subdiameter_correction_confirms_pointer_and_clicks(tmp_path, monkeypatch):
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", .01)
    monkeypatch.setattr(phone_module, "TAP_EFFECT_WAIT", 0)

    class CorrectionMirror(LaggingMirror):
        async def send(self, command):
            await super().send(command)
            if command == "move 25 0":
                self.pending.append((time.monotonic() + .02, disc_frame(285)))
            elif command == "move -10 0":
                self.pending.append((time.monotonic() + .02, disc_frame(275)))

    mirror = CorrectionMirror()
    mirror.frame = disc_frame(250)
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone.origin, phone._pos = {"w": 498, "h": 1080}, {"x": 0, "y": 0}, (250, 500)
    result = asyncio.run(phone.tap_verified(275 / 498, 500 / 1080))
    assert result["clicked"] and result["pointer_seen"]
    assert phone._pointer_confirmed
    assert mirror.commands == ["move 25 0", "move -10 0", "click"]
    assert len(phone._seen["x"]) == 1  # no blind-landing coverage authorized this tap


def test_departing_source_cannot_turn_an_existing_circle_into_landing_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", .01)
    image = Image.open(io.BytesIO(disc_frame(255))).convert("RGB")
    ImageDraw.Draw(image).ellipse((303, 488, 327, 512), fill=(150, 150, 150))
    data = io.BytesIO()
    image.save(data, "PNG")
    before, after = data.getvalue(), disc_frame(255)
    assert not screen_compare.observation_changed(before, after)
    mirror = LaggingMirror()
    mirror.frame = after
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone.origin = {"w": 498, "h": 1080}, {"x": 0, "y": 0}
    phone._position_before, phone._position_from = before, (315 / 498, 500 / 1080)
    phone._position_completed = time.time() - .02
    assert asyncio.run(phone._observe_landing((275 / 498, 500 / 1080), 0)) is None
    assert not mirror.commands and not phone._seen["x"]


@pytest.mark.parametrize("gesture", ["swipe", "scroll", "back"])
def test_gesture_validation_and_actual_write_hold_publication_lock(tmp_path, gesture):
    async def run():
        screen, device = Screen(), Device()
        screen._on_jpeg(app_frame())
        link = Link()
        original_write = link.write
        writes_under_lock = []

        def write(data):
            command = data.decode().split(" ", 1)[1].strip()
            if command == "press" or command.startswith("scroll "):
                writes_under_lock.append(screen._frame_lock._is_owned())
            original_write(data)

        link.write = write
        device.attach(link)
        phone = Phone(device, tmp_path / "calibration.json", screen)
        phone.span = {"w": 360, "h": 780}
        phone._link, phone._pos = device.links, (0, 0)
        validated_under_lock = []

        def validate():
            validated_under_lock.append(screen._frame_lock._is_owned())

        if gesture == "swipe":
            await phone.swipe(.5, .9, .5, .6, hold=False, before_action=validate)
        elif gesture == "back":
            await phone.go_back(before_action=validate)
        else:
            await phone.scroll(.5, .9, -3, before_action=validate)
        assert validated_under_lock == [False, True]
        assert writes_under_lock == [True]
        device.close()
    asyncio.run(run())


def test_press_error_keeps_release_cleanup_when_evidence_is_lost(tmp_path):
    screen = FakeScreen()

    def fail_press(command):
        if command == "press":
            screen.live = False
            raise RuntimeError("press ACK unavailable")

    device = FakeDevice(fail_press)
    phone = make_phone(tmp_path, device, screen)
    with pytest.raises(RuntimeError, match="ACK unavailable"):
        asyncio.run(phone.swipe(.5, .9, .5, .6, hold=False))
    assert device.sent[-2:] == ["press", "release"]
    assert sum(command.startswith("move ") for command in device.sent) == 1
