"""Observed keyboard semantics distinguish editing/navigation Enter from sends."""

import asyncio
import io
import threading
import time
from datetime import datetime

import httpx
import pytest
from PIL import Image

from hidagent import ocr
from hidagent.control import Controls
from hidagent.device import Device, DeviceError
from hidagent.guard import Guard
from hidagent.ocr import TextBox


def keyboard_boxes(label):
    def box(text, x):
        return TextBox(text, (x - .04, .83, x + .04, .88), (x, .855), .99)
    return [box("123", .1), box("空格", .5), box(label, .9)]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HIDAGENT_DATA", str(tmp_path))
    from hidagent import app as module

    image = io.BytesIO()
    Image.new("RGB", (200, 400), "white").save(image, format="PNG")

    class Screen:
        live, frame, updated, generation = True, image.getvalue(), time.time(), 0
        _frame_lock = threading.RLock()

        async def fresh(self, *_args):
            return self.frame

        async def settled(self, **_kwargs):
            pass

    class Link:
        written = []
        reply = b""

        def write(self, data):
            self.written.append(data)
            self.reply = data.split(b" ", 1)[0] + b" ok\n"

        async def readline(self):
            return self.reply

        def is_open(self):
            return True

        def close(self):
            pass

    class Phone:
        async def key(self, combo, *, before_action=None):
            if before_action is not None:
                before_action()
            await device.send("key " + combo)

    now = datetime(2023, 11, 14, 12).timestamp()
    guard = Guard(clock=lambda: now)
    guard.sends = [now - 100] * 20
    device, link, screen, phone = Device(), Link(), Screen(), Phone()
    device.attach(link)
    device.gate = module._hid_gate
    controls = Controls(phone, screen)
    for name, value in (("guard", guard), ("device", device), ("screen", screen),
                        ("phone", phone), ("controls", controls), ("manual_lock", asyncio.Lock())):
        monkeypatch.setattr(module, name, value)
    boxes = keyboard_boxes("搜索")

    async def read(frame):
        assert frame == screen.frame
        return boxes

    monkeypatch.setattr(ocr, "read_text_async", read)
    return module, guard, link, screen, boxes


def test_proven_navigation_enter_does_not_consume_send_budget(setup):
    module, guard, link, _screen, _boxes = setup
    before = list(guard.sends)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
            observed = module.controls.observation()
            response = await client.post("/api/key", json={
                "text": "enter", "screen_id": observed["screen_id"], "enter_intent": "navigation"})
            assert response.status_code == 200, response.text
    asyncio.run(scenario())
    assert guard.sends == before and guard._pending == 0
    assert len(link.written) == 1


@pytest.mark.parametrize("route", ["/api/key", "/api/control/action"])
@pytest.mark.parametrize("intent,label", [("navigation", "搜索"), ("editing", "换行")])
def test_observed_enter_routes_preserve_quota_and_send_reservation(setup, route, intent, label):
    module, guard, link, _screen, boxes = setup
    boxes[:] = keyboard_boxes(label)
    guard.sends.clear()
    assert guard.prepare_send()["allowed"]
    previous = list(guard.sends), guard._reservation

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
            body = {"text": "enter", "screen_id": module.controls.observation()["screen_id"],
                    "enter_intent": intent}
            if route.endswith("action"):
                body.update(action="key", include_diff=False)
            response = await client.post(route, json=body)
            assert response.status_code == 200, response.text
            if route.endswith("action"):
                assert response.json()["command_acknowledged"]
    asyncio.run(scenario())
    assert (guard.sends, guard._reservation) == previous
    assert guard._pending == 0 and len(link.written) == 1


@pytest.mark.parametrize("bad", ["send", "missing_space", "misplaced", "low_confidence"])
def test_intent_flag_requires_complete_observed_keyboard_evidence(setup, bad):
    module, guard, link, _screen, boxes = setup
    if bad == "send":
        boxes[:] = keyboard_boxes("发送")
    elif bad == "missing_space":
        boxes.pop(1)
    elif bad == "misplaced":
        item = boxes[-1]
        boxes[-1] = TextBox(item.text, (.85, .2, .95, .25), (.9, .225), .99)
    else:
        item = boxes[-1]
        boxes[-1] = TextBox(item.text, item.box, item.center, .4)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
            response = await client.post("/api/key", json={
                "text": "enter", "screen_id": module.controls.observation()["screen_id"],
                "enter_intent": "navigation"})
            assert response.status_code == 409
    asyncio.run(scenario())
    assert len(guard.sends) == 20 and link.written == []


@pytest.mark.parametrize("extra", [{}, {"navigation": True}, {"enter_intent": "navigation"}])
def test_unknown_or_unobserved_enter_keeps_send_limits(setup, extra):
    module, guard, link, _screen, _boxes = setup

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
            response = await client.post("/api/key", json={"text": "enter", **extra})
            assert response.status_code == 409
    asyncio.run(scenario())
    assert len(guard.sends) == 20 and link.written == []


@pytest.mark.parametrize("change", ["frame", "generation", "stale", "expiry"])
def test_changed_or_expired_evidence_refuses_at_actual_write(setup, monkeypatch, change):
    from hidagent import enter_context

    module, guard, link, screen, _boxes = setup
    if change == "expiry":
        monkeypatch.setattr(enter_context, "ENTER_EVIDENCE_TTL", .01)

    async def scenario():
        validate = await enter_context.verify_enter_context(screen, screen.frame, "navigation")
        with guard.verified_enter("navigation", validate):
            fence = module._hid_gate("key enter")
            if change == "frame":
                screen.frame += b"changed"
            elif change == "generation":
                screen.generation += 1
            elif change == "stale":
                screen.updated -= 10
            else:
                await asyncio.sleep(.02)
            with pytest.raises(DeviceError):
                with fence:
                    link.write(b"1 key enter\n")
    asyncio.run(scenario())
    assert len(guard.sends) == 20 and link.written == []


def test_keyboard_publication_cannot_interleave_final_validation_and_hid_write(setup, monkeypatch):
    from hidagent.enter_context import verify_enter_context
    from hidagent.screen import Screen

    module, guard, link, initial, _boxes = setup
    screen = Screen()
    screen._on_jpeg(initial.frame)
    monkeypatch.setattr(module, "screen", screen)
    attempted, published = threading.Event(), threading.Event()
    image = io.BytesIO()
    Image.new("RGB", (200, 400), "black").save(image, format="PNG")
    changed = image.getvalue()
    observed_at_write = []
    original_write = link.write

    def write(data):
        observed_at_write.append(screen.snapshot().frame)
        original_write(data)

    def publish():
        # Publish immediately if final validation released the publication lock.
        # Otherwise report contention before waiting, keeping the test deterministic.
        if screen._frame_lock.acquire(blocking=False):
            try:
                screen._on_jpeg(changed)
            finally:
                screen._frame_lock.release()
            attempted.set()
        else:
            attempted.set()
            screen._on_jpeg(changed)
        published.set()

    publisher = threading.Thread(target=publish)
    original_check = guard.check_enter_evidence

    def check(evidence, *, consume=False):
        original_check(evidence, consume=consume)
        if consume:
            publisher.start()
            assert attempted.wait(1), "publication thread did not reach the boundary"

    monkeypatch.setattr(link, "write", write)
    monkeypatch.setattr(guard, "check_enter_evidence", check)

    async def scenario():
        validate = await verify_enter_context(screen, initial.frame, "navigation")
        with guard.verified_enter("navigation", validate):
            await module.device.send("key enter")

    try:
        asyncio.run(scenario())
    finally:
        if publisher.ident is not None:
            publisher.join(1)
    assert published.is_set(), "publication remained blocked after the HID write"
    assert observed_at_write == [initial.frame]
    assert screen.frame == changed
    assert len(link.written) == 1 and len(guard.sends) == 20


def test_quota_exemption_requires_a_screen_publication_lock(setup):
    module, guard, link, screen, _boxes = setup
    screen._frame_lock = None

    with guard.verified_enter("navigation", lambda: None):
        with pytest.raises(DeviceError, match="画面.*锁"):
            with module._hid_gate("key enter"):
                link.write(b"1 key enter\n")
    assert link.written == [] and len(guard.sends) == 20


def test_proof_is_one_use_and_does_not_exempt_modified_enter(setup):
    module, guard, link, _screen, _boxes = setup
    with guard.verified_enter("navigation", lambda: None):
        with pytest.raises(DeviceError):
            asyncio.run(module.device.send("key shift+enter"))
        asyncio.run(module.device.send("key enter"))
        with pytest.raises(DeviceError, match="已使用"):
            asyncio.run(module.device.send("key enter"))
    with pytest.raises(DeviceError):
        asyncio.run(module.device.send("key enter"))
    assert len(link.written) == 1 and len(guard.sends) == 20


def test_navigation_proof_does_not_bypass_trip_or_real_send_confirmation(setup):
    module, guard, _link, _screen, _boxes = setup
    guard.sends.clear()
    asyncio.run(module.device.send("key enter"))
    assert guard._pending == 1 and len(guard.sends) == 1
    with guard.verified_enter("editing", lambda: None):
        asyncio.run(module.device.send("key enter"))
    assert guard._pending == 1 and len(guard.sends) == 1
    assert guard.confirm_send(bubble_visible=True, observed_text="")["counted"]
    assert guard._pending == 0 and len(guard.sends) == 1
    with guard.verified_enter("navigation", lambda: None):
        guard.observe("操作频繁")
        with pytest.raises(DeviceError, match="熔断"):
            module._hid_gate("key enter")


def test_batch_non_send_intent_is_rejected_before_any_command(setup):
    module, _guard, link, _screen, _boxes = setup

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
            response = await client.post("/api/control/batch", json={
                "screen_id": module.controls.observation()["screen_id"], "steps": [
                    {"action": "key", "text": "a"},
                    {"action": "key", "text": "enter", "enter_intent": "navigation"}]})
            assert response.status_code == 409
    asyncio.run(scenario())
    assert link.written == []
