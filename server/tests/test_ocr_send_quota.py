"""Semantic OCR sends reach the real HID quota gate at the click write."""

import asyncio
import io
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image

from hidagent import ocr
from hidagent.agent import Agent
from hidagent.control import Controls
from hidagent.debug import Trace
from hidagent.device import Device
from hidagent.freshness import snapshot
from hidagent.guard import Guard
from hidagent.phone import Phone


@pytest.fixture
def sending(tmp_path, monkeypatch):
    from hidagent import app as module

    encoded = io.BytesIO()
    Image.new("RGB", (200, 400), "white").save(encoded, format="PNG")

    class Screen:
        live, generation = True, 0
        frame, updated = encoded.getvalue(), time.time()
        _frame_lock = threading.RLock()

        async def fresh(self, *_args):
            return None

        async def settled(self, **_kwargs):
            pass

    class Link:
        def __init__(self):
            self.commands, self.quota_at_write, self.reply = [], [], b""

        def write(self, line):
            self.commands.append(line.decode().strip().split(" ", 1)[1])
            self.quota_at_write.append((list(guard.sends), guard._pending))
            self.reply = line.split(b" ", 1)[0] + b" ok\n"

        async def readline(self):
            return self.reply

        def is_open(self):
            return True

        def close(self):
            pass

    clock = [datetime(2023, 11, 14, 12).timestamp()]
    guard = Guard(clock=lambda: clock[0])
    device, link, screen = Device(), Link(), Screen()
    device.attach(link)
    device.gate = module._hid_gate
    phone = Phone(device, tmp_path / "calibration.json", screen)
    controls = Controls(phone, screen)
    agent = Agent(phone, screen, None, None, Trace(None))
    boxes = [ocr.TextBox("发送", (.7, .7, .9, .8), (.8, .75), .99)]
    reads = []

    def read(frame):
        reads.append(frame)
        return boxes

    ocr.set_backend(SimpleNamespace(read_text=read))
    monkeypatch.setattr(module, "guard", guard)
    monkeypatch.setattr(module, "screen", screen)

    async def positioned(x, y):
        await device.send("move 1 0")
        phone._pointer_confirmed = True
        return x, y

    async def effect(*_args):
        phone._effect_frame = phone._effect_snapshot = None
        return False, False

    async def no_feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(phone, "point_verified", positioned)
    monkeypatch.setattr(phone, "_tap_effect", effect)
    monkeypatch.setattr("hidagent.control.feedback.wait_after_action", no_feedback)

    async def invoke(path, query="发送"):
        if path == "agent":
            return await agent._do({"action": "tap_text", "text": query, "region": [0, .6, 1, .9],
                                    "_shot": screen.frame, "_observation": snapshot(screen)})
        request = module.ControlAction(action="tap_text", text=query, region=[0, .6, 1, .9],
                                       screen_id=controls.observation()["screen_id"], include_diff=False)
        if path == "batch":
            return await controls.perform_batch(request.screen_id, [request], include_diff=False)
        return await controls.perform(request)

    yield SimpleNamespace(invoke=invoke, clock=clock, guard=guard, device=device, link=link,
                          screen=screen, phone=phone, controls=controls, boxes=boxes, reads=reads)
    ocr.set_backend(None)
    device.close()


@pytest.mark.parametrize("path", ["controls", "batch", "agent"])
@pytest.mark.parametrize("label,query", [("发送", "发送"), ("發送", "發送"), ("Send", "send"), ("发送", "发")])
def test_exhausted_hourly_quota_blocks_semantic_ocr_send(sending, path, label, query):
    s = sending
    s.boxes[0] = ocr.TextBox(label, (.7, .7, .9, .8), (.8, .75), .99)
    s.guard.sends = [s.clock[0] - 100] * 20
    with pytest.raises(HTTPException, match="20"):
        asyncio.run(s.invoke(path, query))
    assert s.link.commands == ["move 1 0"]
    assert len(s.guard.sends) == 20 and s.guard._pending == 0
    assert len(s.reads) == 1


@pytest.mark.parametrize("path", ["controls", "batch", "agent"])
@pytest.mark.parametrize("reserved", [False, True])
def test_ocr_send_consumes_once_at_click_and_enforces_pacing(sending, path, reserved):
    s = sending
    if reserved:
        assert s.guard.prepare_send()["allowed"]
        s.clock[0] += 9
    original = list(s.guard.sends)

    async def scenario():
        await s.invoke(path)
        assert s.link.commands == ["move 1 0", "click"]
        assert s.link.quota_at_write == [(original, 0), ([s.clock[0]], 1)]
        assert s.guard.sends == [s.clock[0]] and s.guard._reservation is None
        s.clock[0] += 1
        with pytest.raises(HTTPException, match="8 秒"):
            await s.invoke(path)
        assert s.link.commands.count("click") == 1
        assert len(s.guard.sends) == 1 and s.guard._pending == 1
        s.clock[0] += 7
        await s.invoke(path)
        assert s.link.commands.count("click") == 2
        assert s.guard.sends == [s.clock[0] - 8, s.clock[0]] and s.guard._pending == 2

    asyncio.run(scenario())
    assert len(s.reads) == 1


@pytest.mark.parametrize("path", ["controls", "agent"])
def test_ocr_send_rejected_at_final_validation_does_not_consume_quota(sending, monkeypatch, path):
    s = sending
    before = s.device.send_guarded

    async def changed_before_click(command, validator, **kwargs):
        if command == "click":
            s.screen.generation += 1
        return await before(command, validator, **kwargs)

    monkeypatch.setattr(s.device, "send_guarded", changed_before_click)
    if path == "agent":
        with pytest.raises(RuntimeError, match="变化"):
            asyncio.run(s.invoke(path))
    else:
        result = asyncio.run(s.invoke(path))
        assert not result["command_acknowledged"] and "变化" in result["error"]
    assert s.link.commands == ["move 1 0"]
    assert s.guard.sends == [] and s.guard._pending == 0


@pytest.mark.parametrize("path", ["controls", "agent"])
def test_ordinary_ocr_tap_does_not_consume_send_quota(sending, path):
    s = sending
    s.guard.sends = [s.clock[0] - 100] * 20
    s.boxes[0] = ocr.TextBox("设置", (.7, .7, .9, .8), (.8, .75), .99)
    asyncio.run(s.invoke(path, "设置"))
    assert s.link.commands == ["move 1 0", "click"]
    assert len(s.guard.sends) == 20 and s.guard._pending == 0


def test_traditional_send_also_requires_an_observed_region():
    box = ocr.TextBox("發送", (.7, .7, .9, .8), (.8, .75), .99)
    with pytest.raises(ValueError, match="region"):
        ocr.select_match([box], "發送")
