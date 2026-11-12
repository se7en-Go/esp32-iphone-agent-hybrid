"""Operation budgets and disconnects, adapted from pr4-repro-catchup-disconnect.py.

The real Uvicorn HTTP protocol runs on an in-memory transport, so no TCP privileges
are needed. Only hardware and vision are replaced.
"""

import asyncio
import io
import time
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
from hidagent.device import Device
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


class HeldButtonLink:
    """Model USB HID independently of UART, as in pr4-r2-cancel-held.py."""

    def __init__(self, pause, hold_release=False):
        self.pause = pause
        self.hold_release = hold_release
        self.closed = False
        self.pressed = False
        self.writes = []
        self.events = []
        self.replies = asyncio.Queue()
        self.waiting = asyncio.Event()
        self.delayed = None
        self.release_waiting = asyncio.Event()
        self.release_reply = None

    def is_open(self):
        return not self.closed

    def close(self):
        self.events.append("close")
        self.closed = True  # Closing UART does not release the iPhone's USB button.

    def write(self, wire):
        if self.closed:
            raise OSError("serial closed")
        ident, command = wire.decode().strip().split(" ", 1)
        self.writes.append(command)
        self.events.append(command)
        if command == "press":
            self.pressed = True
        elif command == "release":
            self.pressed = False
        reply = f"{ident} ok\n".encode()
        if (self.pause == "press" and command == "press") or (
            self.pause == "drag" and self.pressed and command.startswith("move ")
        ):
            self.delayed = reply
            self.waiting.set()
        elif command == "release" and self.hold_release and self.delayed is not None:
            self.hold_release = False
            self.release_reply = reply
            self.release_waiting.set()
        else:
            self.replies.put_nowait(reply)

    async def readline(self):
        return await self.replies.get()


@pytest.mark.parametrize("pause,disconnect,repeat_cancel", [
    ("press", True, False), ("drag", True, False), ("drag", False, False), ("press", True, True),
], ids=["press-disconnect", "drag-disconnect", "drag-deadline", "press-repeated-cancel"])
def test_cancelled_gesture_releases_before_serial_close(tmp_path, monkeypatch, pause, disconnect, repeat_cancel):
    from hidagent import operation

    if not disconnect:
        monkeypatch.setattr(operation, "OPERATION_TIMEOUT", 0.04)

    async def scenario():
        device, link = Device(), HeldButtonLink(pause, hold_release=repeat_cancel)
        device.attach(link)
        phone = Phone(device, tmp_path / "calibration.json")
        phone.span = {"w": 500, "h": 1000}
        monkeypatch.setattr(app_module, "device", device)
        monkeypatch.setattr(app_module, "phone", phone)
        monkeypatch.setattr(app_module, "agent", SimpleNamespace(running=False, stop=lambda: None))
        monkeypatch.setattr(app_module, "manual_lock", asyncio.Lock())
        config = uvicorn.Config(app_module.app, lifespan="off", log_level="critical", http="h11")
        state = ServerState()
        protocol = H11Protocol(config, state, {})
        transport = MemoryTransport()
        protocol.connection_made(transport)
        request = httpx.Request("POST", "http://127.0.0.1:8890/api/swipe",
                                json={"x1": .5, "y1": .5, "x2": .7, "y2": .7})
        wire = (b"POST /api/swipe HTTP/1.1\r\n"
                + b"".join(k + b": " + v + b"\r\n" for k, v in request.headers.raw)
                + b"\r\n" + request.read())
        protocol.data_received(wire)
        try:
            await asyncio.wait_for(link.waiting.wait(), 1)
            assert link.pressed, "cancellation must interrupt an actually held button"
            before_cancel = len(link.writes)
            if disconnect:
                protocol.connection_lost(None)
            if repeat_cancel:
                await asyncio.wait_for(link.release_waiting.wait(), 1)
                assert app_module.manual_lock.locked()
                device.request_abort()
                for task in list(state.tasks):
                    task.cancel()
                await asyncio.sleep(0.01)
                assert app_module.manual_lock.locked(), "cleanup released the route lock before its ACK"
                assert state.tasks and all(not task.done() for task in state.tasks)
                assert not link.closed
                link.replies.put_nowait(link.release_reply)
            await asyncio.sleep(0.08)
            # A late command ACK must not resume the gesture after cancellation.
            link.replies.put_nowait(link.delayed)
            if state.tasks:
                await asyncio.wait_for(asyncio.gather(*state.tasks), 2)
            assert not app_module.manual_lock.locked()
            assert not link.pressed, link.events
            assert link.writes[before_cancel:], "cancelled gesture omitted release"
            assert set(link.writes[before_cancel:]) == {"release"}, "new HID after cancellation"
            if "close" in link.events:
                press = link.events.index("press")
                release = link.events.index("release", press)
                assert release < link.events.index("close"), link.events
            assert not link.closed, "cancelling an ACK must not abandon the serial link"
        finally:
            if not disconnect:
                protocol.connection_lost(None)

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


def test_loop_timer_is_a_deadline_even_when_monotonic_lags():
    """Windows can deliver the loop timer while time.monotonic() is still one tick behind."""
    from hidagent import operation

    async def scenario():
        loop = asyncio.get_running_loop()
        origin = time.monotonic()

        def fast():
            # The scheduler runs ahead of the monotonic read, as a coarse tick can.
            return origin + (time.monotonic() - origin) * 20

        loop.time = fast
        with pytest.raises(operation.OperationDeadline):
            async with operation.operation_scope(0.05):
                await asyncio.sleep(1)

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
