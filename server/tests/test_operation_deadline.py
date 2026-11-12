"""Operation budgets and disconnects, adapted from pr4-repro-catchup-disconnect.py.

The real Uvicorn HTTP protocol runs on an in-memory transport, so no TCP privileges
are needed. Only hardware and vision are replaced.
"""

import asyncio
import io
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from PIL import Image
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.server import ServerState

from hidagent import app as app_module
from hidagent import phone as phone_module
from hidagent.control import Controls
from hidagent.phone import Phone


class MemoryTransport(asyncio.Transport):
    def __init__(self):
        self.output = bytearray()
        self.closed = False

    def get_extra_info(self, name, default=None):
        return {"sockname": ("127.0.0.1", 8890), "peername": ("127.0.0.1", 12345)}.get(name, default)

    def write(self, data):
        self.output.extend(data)

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed

    def pause_reading(self):
        pass

    def resume_reading(self):
        pass


class Screen:
    live = True
    updated = 0

    def __init__(self):
        buffer = io.BytesIO()
        Image.new("RGB", (20, 40), "white").save(buffer, "JPEG")
        self.frame = buffer.getvalue()

    async def settled(self, **kwargs):
        pass

    async def fresh(self, *args):
        return self.frame


def make_phone(tmp_path):
    sent = []

    async def send(command):
        sent.append(command)

    phone = Phone(SimpleNamespace(send=send, links=0, abort_epoch=0), tmp_path / "calibration.json", Screen())
    phone.span = {"w": 500, "h": 1000}
    return phone, sent


def test_tap_budget_covers_all_verification_rounds(tmp_path):
    phone, sent = make_phone(tmp_path)
    phone.operation_timeout = 0.10
    attempts = []

    async def find(*args, **kwargs):
        attempts.append(len(attempts))
        await asyncio.sleep(0.07)
        return (0.53, 0.53) if len(attempts) == 1 else (0.5, 0.5)

    phone._find_pointer = find

    async def scenario():
        with pytest.raises(RuntimeError, match="deadline"):
            await phone.tap_verified(0.5, 0.5)
        commands = list(sent)
        await asyncio.sleep(0.15)
        assert sent == commands
        assert "click" not in sent
        assert len(attempts) == 2  # the second round shares the first round's budget

    asyncio.run(scenario())


def test_cancelled_nudge_does_not_send_a_return_move(tmp_path):
    phone, sent = make_phone(tmp_path)

    async def scenario():
        entered = asyncio.Event()

        async def catch_up(epoch):
            return None

        async def watch(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        phone._catch_up, phone._watch = catch_up, watch
        task = asyncio.create_task(phone.tap_verified(0.5, 0.5))
        await entered.wait()
        before_cancel = list(sent)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sent == before_cancel, "cancelled nudge must not move back or click"

    asyncio.run(scenario())


def test_http_disconnect_cancels_catch_up_before_any_later_hid(tmp_path, monkeypatch):
    phone, sent = make_phone(tmp_path)
    monkeypatch.setattr(app_module, "phone", phone)
    monkeypatch.setattr(app_module, "controls", Controls(phone, phone.screen))
    monkeypatch.setattr(phone_module, "TAP_GAP", 0)

    async def scenario():
        monkeypatch.setattr(app_module, "manual_lock", asyncio.Lock())
        waiting = asyncio.Event()
        resume = asyncio.Event()

        async def catch_up(epoch):
            waiting.set()
            await resume.wait()
            return None

        async def watch(*args, **kwargs):
            return (0.5, 0.5)

        phone._catch_up, phone._watch = catch_up, watch
        observation = app_module.controls.observation()
        config = uvicorn.Config(app_module.app, lifespan="off", log_level="error", http="h11")
        state = ServerState()
        protocol = H11Protocol(config, state, {})
        transport = MemoryTransport()
        protocol.connection_made(transport)
        request = httpx.Request("POST", "http://127.0.0.1:8890/api/control/action",
                                json={"action": "tap", "screen_id": observation["screen_id"], "x": .5, "y": .5})
        wire = (b"POST /api/control/action HTTP/1.1\r\n"
                + b"".join(k + b": " + v + b"\r\n" for k, v in request.headers.raw)
                + b"\r\n" + request.read())
        protocol.data_received(wire)
        await asyncio.wait_for(waiting.wait(), 1)
        assert not transport.output
        commands = list(sent)
        protocol.connection_lost(None)
        await asyncio.sleep(0.05)
        resume.set()
        if state.tasks:
            await asyncio.wait_for(asyncio.gather(*state.tasks), 1)
        assert sent == commands, "disconnected HTTP request sent later HID"
        assert "click" not in sent
        assert app_module.controls.job is None
        assert not app_module.manual_lock.locked()

    asyncio.run(scenario())


def test_deadline_is_checked_after_synchronous_pointer_recognition(tmp_path):
    import time

    phone, sent = make_phone(tmp_path)
    phone.operation_timeout = 0.03

    async def find(*args, **kwargs):
        # A CPU-bound image search can delay the asyncio timer callback.
        time.sleep(0.05)
        return (0.5, 0.5)

    phone._find_pointer = find

    async def scenario():
        with pytest.raises(RuntimeError, match="deadline"):
            await phone.tap_verified(0.5, 0.5)
        assert "click" not in sent

    asyncio.run(scenario())


def test_http_deadline_includes_the_final_observation(tmp_path, monkeypatch):
    from hidagent import operation

    phone, sent = make_phone(tmp_path)
    monkeypatch.setattr(app_module, "phone", phone)
    monkeypatch.setattr(app_module, "controls", Controls(phone, phone.screen))
    monkeypatch.setattr(operation, "OPERATION_TIMEOUT", 0.1)
    monkeypatch.setattr(phone_module, "TAP_GAP", 0)

    async def find(*args, **kwargs):
        return (0.5, 0.5)

    async def fresh(*args):
        await asyncio.sleep(0.5)
        return phone.screen.frame

    phone._find_pointer, phone.screen.fresh = find, fresh

    async def scenario():
        monkeypatch.setattr(app_module, "manual_lock", asyncio.Lock())
        observation = app_module.controls.observation()
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.post("/api/control/action", json={
                "action": "tap", "screen_id": observation["screen_id"], "x": .5, "y": .5,
            })
        assert response.status_code == 504
        assert "deadline" in response.json()["detail"]
        assert sent.count("click") == 1
        assert app_module.controls.job is None
        assert not app_module.manual_lock.locked()

    asyncio.run(scenario())


def test_device_rechecks_deadline_after_waiting_for_serial_lock():
    import time

    from hidagent.device import Device
    from hidagent.operation import operation_scope

    class Link:
        writes = []

        def is_open(self):
            return True

        def write(self, data):
            self.writes.append(data)

        async def readline(self):
            return b"1 ok\n"

        def close(self):
            pass

    async def scenario():
        device, link = Device(), Link()
        device.attach(link)
        await device._lock.acquire()
        async with operation_scope(0.03):
            queued = asyncio.create_task(device.send("click"))
            await asyncio.sleep(0)  # exchange queues behind the held serial lock
            await asyncio.sleep(0)
            time.sleep(0.05)  # expire before the event loop can run its timeout callback
            device._lock.release()
            with pytest.raises((RuntimeError, asyncio.CancelledError)):
                await queued

    with pytest.raises(RuntimeError, match="deadline"):
        asyncio.run(scenario())
    assert Link.writes == []


def test_exchange_timing_excludes_serial_lock_queueing():
    import time

    from hidagent.device import Device, exchange_timing

    class Link:
        def is_open(self):
            return True

        def write(self, data):
            self.wrote_at = time.monotonic()

        async def readline(self):
            return b"0 {\"up\":100000}\n"

        def close(self):
            pass

    async def scenario():
        device, link = Device(), Link()
        device.attach(link)
        await device._lock.acquire()
        with exchange_timing() as timing:
            queued = asyncio.create_task(device.query("status"))
            await asyncio.sleep(0.03)
            assert timing.sent_at is None
            unlocked = time.monotonic()
            device._lock.release()
            assert await queued == '{"up":100000}'
        assert unlocked <= timing.sent_at <= link.wrote_at
        assert link.wrote_at - timing.sent_at < 0.01

    asyncio.run(scenario())
