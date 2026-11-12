"""Enter retains its observation and accounts only at an eligible HID write."""

import asyncio
import io
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image

from hidagent import ocr
from hidagent.control import Controls
from hidagent.device import Device, DeviceError
from hidagent.guard import Guard
from hidagent.phone import Phone
from hidagent.screen import Screen


@pytest.fixture
def entering(tmp_path, monkeypatch):
    monkeypatch.setenv("HIDAGENT_DATA", str(tmp_path))
    from hidagent import app as module

    image = io.BytesIO()
    Image.new("RGB", (200, 400), "white").save(image, format="PNG")
    screen = Screen()
    screen._on_jpeg(image.getvalue())
    clock = [datetime(2023, 11, 14, 12).timestamp()]
    guard = Guard(clock=lambda: clock[0])

    class Link:
        def __init__(self):
            self.writes, self.accounted, self.reply = [], [], b""

        def write(self, data):
            self.writes.append(data)
            self.accounted.append((list(guard.sends), guard._pending))
            self.reply = data.split(b" ", 1)[0] + b" ok\n"

        async def readline(self):
            return self.reply

        def is_open(self):
            return True

        def close(self):
            pass

    device, link = Device(), Link()
    device.attach(link)
    device.gate = module._hid_gate
    phone = Phone(device, tmp_path / "calibration.json", screen)
    controls = Controls(phone, screen)
    for name, value in (("guard", guard), ("device", device), ("screen", screen),
                        ("phone", phone), ("controls", controls), ("manual_lock", asyncio.Lock())):
        monkeypatch.setattr(module, name, value)

    async def read(_frame):
        return [ocr.TextBox(label, (x - .04, .83, x + .04, .88), (x, .855), .99)
                for label, x in (("123", .1), ("空格", .5), ("搜索", .9))]

    monkeypatch.setattr(ocr, "read_text_async", read)
    return SimpleNamespace(module=module, screen=screen, guard=guard, device=device,
                           link=link, phone=phone, controls=controls, clock=clock)


@pytest.mark.parametrize("replace_observation", [False, True])
def test_direct_enter_rejects_identical_reconnect_after_ticket_consumption(
        entering, monkeypatch, replace_observation):
    s = entering
    consume = s.controls.consume

    def reconnect(screen_id):
        observed = consume(screen_id)
        frame = s.screen.frame
        s.screen._on_ready()
        s.screen._on_jpeg(frame)
        if replace_observation:
            s.controls.observation()
        return observed

    monkeypatch.setattr(s.controls, "consume", reconnect)

    async def scenario():
        request = s.module.KeyText(text="enter", enter_intent="navigation",
                                   screen_id=s.controls.observation()["screen_id"])
        with pytest.raises(HTTPException) as caught:
            await s.module.key(request)
        assert caught.value.status_code == 409

    asyncio.run(scenario())
    assert s.screen.generation == 1
    assert s.link.writes == [] and s.guard.sends == [] and s.guard._pending == 0


@pytest.mark.parametrize("lock_name", ["phone", "device"])
def test_direct_enter_keeps_original_generation_through_lock_wait(entering, monkeypatch, lock_name):
    s = entering
    verified = asyncio.Event()
    verify = s.module.verify_enter_context

    async def capture(*args, **kwargs):
        validator = await verify(*args, **kwargs)
        verified.set()
        return validator

    monkeypatch.setattr(s.module, "verify_enter_context", capture)

    async def scenario():
        lock = s.phone._interaction_lock if lock_name == "phone" else s.device._lock
        await lock.acquire()
        request = s.module.KeyText(text="enter", enter_intent="navigation",
                                   screen_id=s.controls.observation()["screen_id"])
        task = asyncio.create_task(s.module.key(request))
        try:
            await asyncio.wait_for(verified.wait(), 1)
            assert not task.done()
            frame = s.screen.frame
            s.screen._on_ready()
            s.screen._on_jpeg(frame)
        finally:
            lock.release()
        with pytest.raises(HTTPException) as caught:
            await task
        assert caught.value.status_code == 409

    asyncio.run(scenario())
    assert s.link.writes == [] and s.guard.sends == [] and s.guard._pending == 0


@pytest.mark.parametrize("command", ["key enter", "key shift+enter"])
@pytest.mark.parametrize("reserved", [False, True])
def test_rejected_final_enter_validation_preserves_quota(entering, command, reserved):
    s = entering
    if reserved:
        assert s.guard.prepare_send()["allowed"]
        s.clock[0] += 9
    original = list(s.guard.sends), s.guard._reservation

    def stale():
        raise RuntimeError("original observation no longer eligible")

    with pytest.raises(RuntimeError, match="no longer eligible"):
        asyncio.run(s.device.send_guarded(command, stale, write_lock=s.screen._frame_lock))
    assert s.link.writes == []
    assert (s.guard.sends, s.guard._reservation) == original
    assert s.guard._pending == 0


@pytest.mark.parametrize("command", ["key enter", "key shift+enter"])
def test_enter_consumes_once_at_write_and_enforces_pacing(entering, command):
    s = entering
    checks = []

    def validate():
        checks.append((list(s.guard.sends), s.guard._pending))

    async def scenario():
        await s.device.send_guarded(command, validate, write_lock=s.screen._frame_lock)
        assert checks == [([], 0)]
        assert s.link.accounted == [([s.clock[0]], 1)]
        with pytest.raises(DeviceError, match="8 秒"):
            await s.device.send(command)
        assert len(s.link.writes) == 1

    asyncio.run(scenario())
