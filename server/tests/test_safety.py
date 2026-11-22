"""熔断挡住 HID，以及网页接口不接受跨站表单。"""

import asyncio
import io
import os
import socket
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

from hidagent import app as module
from hidagent.client import IPhoneClient
from hidagent.device import DeviceError
from hidagent.guard import Guard


def _real_guard_bytes() -> bytes | None:
    path = Path.home() / ".hidagent" / "guard.json"
    return path.read_bytes() if path.is_file() else None


def test_tripped_guard_blocks_hid_and_cross_site_posts(tmp_path):
    before = _real_guard_bytes()
    saved = (module.guard.tripped, module.guard.reason, list(module.guard.sends), module.guard._path)
    module.guard._path = tmp_path / "guard.json"
    module.guard.sends = []
    module.guard.tripped = True
    module.guard.reason = "操作频繁"
    try:
        async def scenario():
            transport = httpx.ASGITransport(app=module.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                blocked = await client.post("/api/key", json={"text": "enter"})
                assert blocked.status_code == 409 and "熔断" in blocked.json()["detail"]
                stopped = await client.post("/api/agent/stop", json={})
                assert stopped.status_code == 200
                bare = await client.post("/api/agent/stop", headers={"origin": "http://127.0.0.1"})
                assert bare.status_code == 200
                evil = await client.post(
                    "/api/key", json={"text": "a"}, headers={"origin": "https://evil.example"})
                assert evil.status_code == 403
                form = await client.post(
                    "/api/key", content=b"text=a",
                    headers={"content-type": "application/x-www-form-urlencoded"})
                assert form.status_code == 415

        asyncio.run(scenario())
    finally:
        module.guard.tripped, module.guard.reason, module.guard.sends, module.guard._path = saved
        assert _real_guard_bytes() == before


def test_enter_modifiers_and_trailing_space_match(tmp_path):
    night = datetime(2023, 11, 14, 23, 30).timestamp()
    previous = module.guard
    module.guard = Guard(clock=lambda: night, path=tmp_path / "guard.json")
    try:
        for command in ("key enter", "key enter ", "key Enter", "key enter+shift", "key shift+enter"):
            with pytest.raises(DeviceError, match="静默"):
                with module._hid_gate(command) as commit_write:
                    commit_write()
        module._hid_gate("key a")
        assert module.guard.sends == []
    finally:
        module.guard = previous


def test_web_reset_clears_trip_but_keeps_sends(tmp_path):
    before = _real_guard_bytes()
    saved = (module.guard.tripped, module.guard.reason, list(module.guard.sends), module.guard._path)
    path = tmp_path / "guard.json"
    module.guard._path = path
    module.guard.sends = [time.time()] * 20
    module.guard.tripped = True
    module.guard.reason = "验证码"
    module.guard._save()
    try:
        async def scenario():
            transport = httpx.ASGITransport(app=module.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                denied = await client.post(
                    "/api/guard/reset", json={"confirm": False},
                    headers={"origin": "http://127.0.0.1"},
                )
                assert denied.status_code == 400
                assert module.guard.tripped
                no_origin = await client.post("/api/guard/reset", json={"confirm": True})
                assert no_origin.status_code == 403
                ok = await client.post(
                    "/api/guard/reset", json={"confirm": True},
                    headers={"origin": "http://127.0.0.1:8890"},
                )
                assert ok.status_code == 200

        asyncio.run(scenario())
        assert module.guard.tripped is False
        assert len(module.guard.sends) == 20
        assert Guard(path=path).sends and not Guard(path=path).tripped
        assert module.guard.prepare_send("")["allowed"] is False
    finally:
        module.guard.tripped, module.guard.reason, module.guard.sends, module.guard._path = saved
        assert _real_guard_bytes() == before


class _OkLink:
    def __init__(self):
        self.written: list[bytes] = []
        self.replies: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)
        req = data.decode().strip().split()[0]
        self.replies.put_nowait(f"{req} ok\n".encode())

    async def readline(self) -> bytes:
        return await self.replies.get()

    def close(self) -> None:
        self.closed = True
        self.replies.put_nowait(b"")

    def is_open(self) -> bool:
        return not self.closed


class _HoldFirstType(_OkLink):
    def __init__(self):
        super().__init__()
        self.first = asyncio.Event()
        self._held = False

    def write(self, data: bytes) -> None:
        self.written.append(data)
        if b" type " in data and not self._held:
            self._held = True
            self.first.set()
            return
        req = data.decode().strip().split()[0]
        self.replies.put_nowait(f"{req} ok\n".encode())


def test_prepare_then_enter_reaches_serial_once(tmp_path):
    noon = datetime(2023, 11, 14, 12, 0).timestamp()
    previous = module.guard
    module.guard = Guard(clock=lambda: noon, path=tmp_path / "guard.json")
    link = _OkLink()
    module.device.attach(link)
    try:
        async def scenario():
            transport = httpx.ASGITransport(app=module.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                prepared = await client.post("/api/skill/prepare-send", json={"skill": ""})
                assert prepared.status_code == 200 and prepared.json()["allowed"] is True
                first = await client.post("/api/key", json={"text": "enter"})
                assert first.status_code == 200, first.text
                second = await client.post("/api/key", json={"text": "enter"})
                assert second.status_code == 409

        asyncio.run(scenario())
        assert len([line for line in link.written if b"key enter" in line]) == 1
        assert len(module.guard.sends) == 1
    finally:
        module.device.close()
        module.guard = previous


@pytest.fixture
def send_routes(tmp_path, monkeypatch):
    from hidagent.control import Controls
    from hidagent.device import Device
    from hidagent.phone import Phone
    from hidagent.screen import Screen as MirrorScreen

    image = io.BytesIO()
    Image.new("RGB", (100, 200), "white").save(image, format="JPEG")

    class Screen(MirrorScreen):
        def __init__(self):
            super().__init__()
            self._on_jpeg(image.getvalue())

        async def fresh(self, *_args):
            return self.frame

        async def settled(self, **_kwargs):
            pass

    clock = [datetime(2023, 11, 14, 12, 0).timestamp()]
    guard = Guard(clock=lambda: clock[0], path=tmp_path / "guard.json")
    device, link, screen = Device(), _OkLink(), Screen()
    device.attach(link)
    device.gate = module._hid_gate
    phone = Phone(device, tmp_path / "calibration.json", screen)
    controls = Controls(phone, screen)

    async def positioned(x, y):
        phone._pointer_confirmed = True
        return x, y

    monkeypatch.setattr(phone, "point_verified", positioned)
    monkeypatch.setattr("hidagent.phone.TAP_GAP", 0)
    monkeypatch.setattr(module, "guard", guard)
    monkeypatch.setattr(module, "device", device)
    monkeypatch.setattr(module, "phone", phone)
    monkeypatch.setattr(module, "screen", screen)
    monkeypatch.setattr(module, "controls", controls)
    monkeypatch.setattr(module, "resolve_point", lambda *_args: (0.9, 0.9))
    yield clock, guard, link, phone
    device.close()


@pytest.mark.parametrize("confirm_first", [False, True])
def test_late_enter_records_send_time_and_blocks_second(send_routes, confirm_first):
    clock, guard, link, _phone = send_routes

    async def scenario():
        transport = httpx.ASGITransport(app=module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            prepared = await client.post("/api/skill/prepare-send", json={})
            assert prepared.json()["allowed"]
            clock[0] += 9
            first = await client.post("/api/key", json={"text": "enter"})
            assert first.status_code == 200
            if confirm_first:
                confirmed = await client.post("/api/skill/confirm-send", json={"bubble_visible": True})
                assert confirmed.json()["counted"]
                again = await client.post("/api/skill/prepare-send", json={})
                assert not again.json()["allowed"]
            second = await client.post("/api/key", json={"text": "enter"})
            assert second.status_code == 409

    asyncio.run(scenario())
    assert guard.sends == [clock[0]]
    assert len([line for line in link.written if b"key enter" in line]) == 1


@pytest.mark.parametrize("prepare_first", [False, True])
@pytest.mark.parametrize("previous_sends", [0, 19])
def test_send_button_consumes_reservation_before_confirm_and_enter(send_routes, prepare_first, previous_sends):
    clock, guard, link, _phone = send_routes
    guard.sends = [clock[0] - 100 - 9 * index for index in reversed(range(previous_sends))]

    async def scenario():
        transport = httpx.ASGITransport(app=module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            if prepare_first:
                prepared = await client.post("/api/skill/prepare-send", json={})
                assert prepared.json()["allowed"]
            observed = await client.get("/api/control/observe")
            sent = await client.post("/api/control/layout-tap", json={
                "layout": "demo", "point": "send", "screen_id": observed.json()["screen_id"],
            })
            assert sent.status_code == 200, sent.text
            assert sent.json()["command_acknowledged"]
            confirmed = await client.post("/api/skill/confirm-send", json={"bubble_visible": True})
            assert confirmed.json()["counted"]
            clock[0] += 1
            second = await client.post("/api/key", json={"text": "enter"})
            assert second.status_code == 409

    asyncio.run(scenario())
    assert len(guard.sends) == previous_sends + 1
    assert len([line for line in link.written if b" click" in line]) == 1
    assert not any(b"key enter" in line for line in link.written)


def test_send_button_records_time_after_pointer_positioning(send_routes, monkeypatch):
    clock, guard, _link, phone = send_routes

    async def delayed_position(x, y):
        clock[0] += 9
        phone._pointer_confirmed = True
        return x, y

    monkeypatch.setattr(phone, "point_verified", delayed_position)

    async def scenario():
        transport = httpx.ASGITransport(app=module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            observed = await client.get("/api/control/observe")
            sent = await client.post("/api/control/layout-tap", json={
                "layout": "demo", "point": "send", "screen_id": observed.json()["screen_id"],
            })
            assert sent.status_code == 200 and sent.json()["command_acknowledged"]

    asyncio.run(scenario())
    assert guard.sends == [clock[0]]


def test_invalid_send_button_observation_does_not_reserve_quota(send_routes):
    _clock, guard, link, _phone = send_routes

    async def scenario():
        transport = httpx.ASGITransport(app=module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            sent = await client.post("/api/control/layout-tap", json={
                "layout": "demo", "point": "send", "screen_id": "invalid",
            })
            assert sent.status_code == 409

    asyncio.run(scenario())
    assert guard.sends == []
    assert link.written == []


def test_send_button_rate_limit_returns_409_without_second_click(send_routes):
    _clock, guard, link, phone = send_routes

    async def scenario():
        transport = httpx.ASGITransport(app=module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            observed = await client.get("/api/control/observe")
            first = await client.post("/api/control/layout-tap", json={
                "layout": "demo", "point": "send", "screen_id": observed.json()["screen_id"],
            })
            assert first.status_code == 200 and first.json()["command_acknowledged"]
            # A stale action result no longer grants a token; observe again to test the send quota.
            assert first.json()["screen_id"] is None
            # This fixture publishes no click feedback. Deliver a new synthetic
            # capture through the production receiver before requesting a new ticket.
            previous_receipt = phone.screen.updated
            capture = Image.new("RGB", (100, 200), "white")
            capture.putpixel((4, 4), (0, 0, 0))
            payload = io.BytesIO()
            capture.save(payload, format="JPEG")
            phone.screen._on_jpeg(payload.getvalue())
            assert phone.screen.updated > previous_receipt
            observed = await client.get("/api/control/observe")
            assert observed.status_code == 200, observed.text
            assert observed.json()["frame_received_at"] == phone.screen.updated
            second = await client.post("/api/control/layout-tap", json={
                "layout": "demo", "point": "send", "screen_id": observed.json()["screen_id"],
            })
            assert second.status_code == 409
            assert "8 秒" in second.json()["detail"]

    asyncio.run(scenario())
    assert len(guard.sends) == 1
    assert len([line for line in link.written if b" click" in line]) == 1


def test_stopped_type_returns_409_not_500(tmp_path, monkeypatch):
    from hidagent.control import Controls
    from hidagent.device import Device
    from hidagent.phone import Phone
    from hidagent.screen import Screen

    screen, device = Screen(), Device()
    image = io.BytesIO()
    Image.new("RGB", (360, 780), "white").save(image, "JPEG")
    screen._on_jpeg(image.getvalue())
    phone = Phone(device, tmp_path / "cal.json", screen)
    monkeypatch.setattr(module, "screen", screen)
    monkeypatch.setattr(module, "device", device)
    monkeypatch.setattr(module, "phone", phone)
    monkeypatch.setattr(module, "controls", Controls(phone, screen))
    link = _HoldFirstType()
    module.device.attach(link)
    try:
        async def scenario():
            transport = httpx.ASGITransport(app=module.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                typing = asyncio.create_task(client.post("/api/type", json={"text": "x" * 1000}))
                await asyncio.wait_for(link.first.wait(), 1)
                stopped = await client.post("/api/agent/stop", json={})
                assert stopped.status_code == 200
                result = await typing
                assert result.status_code == 409
                assert "紧急停止" in result.json()["detail"]

        asyncio.run(scenario())
        assert len([line for line in link.written if b" type " in line]) == 1
    finally:
        module.device.close()


def test_stop_during_calibration_does_not_keep_moving():
    class HoldSettled:
        live = True
        frame = b"frame"
        updated = 0.0

        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def settled(self, **_kwargs):
            self.entered.set()
            await self.release.wait()

        async def fresh(self, *_args, **_kwargs):
            return None

    screen = HoldSettled()
    saved = module.phone.screen
    module.phone.screen = screen
    link = _OkLink()
    module.device.attach(link)
    try:
        async def scenario():
            transport = httpx.ASGITransport(app=module.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                job = asyncio.create_task(client.post("/api/calibrate/start", json={}))
                await asyncio.wait_for(screen.entered.wait(), 1)
                stopped = await client.post("/api/agent/stop", json={})
                assert stopped.status_code == 200
                screen.release.set()
                result = await job
                assert result.status_code == 409

        asyncio.run(scenario())
        text = b"".join(link.written).decode()
        assert "move 12" not in text and "move -12" not in text and "move 200 200" not in text
    finally:
        module.phone.screen = saved
        module.device.close()


def _not_unsupported(call):
    try:
        return call()
    except RuntimeError as exc:
        assert not str(exc).startswith("HTTP 415"), exc
        return exc


@contextmanager
def _running_test_app(*, timeout=10):
    """Keep ownership of the ephemeral listener through startup and shutdown."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(uvicorn.Config(module.app, host="127.0.0.1", port=port, log_level="error"))
        failures = []

        def run():
            try:
                server.run(sockets=[listener])
            except BaseException as exc:
                failures.append(f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            last_probe = "server has not reported startup"
            with httpx.Client(base_url=url, timeout=.3, trust_env=False) as probe:
                deadline = time.monotonic() + timeout
                while True:
                    if not thread.is_alive():
                        raise AssertionError(f"real app thread exited before ready: {failures}; {last_probe}")
                    if server.started:
                        try:
                            response = probe.get("/api/status")
                            last_probe = f"HTTP {response.status_code}"
                            if response.status_code == 200:
                                break
                        except httpx.HTTPError as exc:
                            last_probe = f"{type(exc).__name__}: {exc}"
                    if time.monotonic() >= deadline:
                        if not thread.is_alive():
                            raise AssertionError(f"real app thread exited before ready: {failures}; {last_probe}")
                        raise AssertionError(
                            f"real app not ready: started={server.started}, alive={thread.is_alive()}, "
                            f"last_probe={last_probe}, errors={failures}")
                    time.sleep(.01)
            if not thread.is_alive():
                raise AssertionError(f"real app thread exited before ready: {failures}; {last_probe}")
            yield url
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive(), "real app test thread did not stop"


def test_real_client_stop_calibrate_home_back_not_415(tmp_path, monkeypatch):
    monkeypatch.setenv("HIDAGENT_PORT", str(tmp_path / "missing-port"))
    original_run = uvicorn.Server.run
    owned = []

    def run_with_owned_socket(self, sockets=None):
        # The old bind/close/start sequence allows another process to claim the
        # port here. The listener must stay bound before the server thread runs.
        assert sockets and len(sockets) == 1
        address = sockets[0].getsockname()
        with socket.socket() as competitor, pytest.raises(OSError):
            competitor.bind(address)
        owned.append(address)
        return original_run(self, sockets=sockets)

    monkeypatch.setattr(uvicorn.Server, "run", run_with_owned_socket)
    with _running_test_app() as url:
        with IPhoneClient(url) as client:
            assert client.stop()["ok"] is True
            _not_unsupported(lambda: client.calibrate())
            _not_unsupported(lambda: client.action("home"))
            _not_unsupported(lambda: client.action("back"))
    assert len(owned) == 1


def test_mcp_stop_task_against_the_real_app(tmp_path, monkeypatch):
    monkeypatch.setenv("HIDAGENT_PORT", str(tmp_path / "missing-port"))
    before = _real_guard_bytes()
    saved = (module.guard.tripped, module.guard.reason, list(module.guard.sends), module.guard._path)
    module.guard._path = tmp_path / "guard.json"
    module.guard.tripped = False
    try:
        with _running_test_app() as url:
            async def scenario():
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "hidagent.cli", "--url", url, "mcp"],
                    env={k: v for k, v in os.environ.items()
                         if not k.startswith(("LLM_", "ACTOR_", "PLANNER_"))},
                )
                async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    assert "guard_reset" not in names
                    result = await session.call_tool("stop_task", {})
                    assert not result.isError

            asyncio.run(asyncio.wait_for(scenario(), 25))
    finally:
        module.guard.tripped, module.guard.reason, module.guard.sends, module.guard._path = saved
        assert _real_guard_bytes() == before


def test_real_app_probe_reports_thread_startup_failure(monkeypatch):
    def failed_start(*_args, **_kwargs):
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr(uvicorn.Server, "run", failed_start)
    with pytest.raises(AssertionError, match="thread exited.*synthetic startup failure"):
        with _running_test_app(timeout=2.0):
            pytest.fail("failed startup was accepted")


def test_real_app_probe_does_not_treat_http_503_as_ready(monkeypatch):
    class UnhealthyServer:
        started = True
        should_exit = False

        def __init__(self, *_args):
            pass

        def run(self, **_kwargs):
            while not self.should_exit:
                time.sleep(.001)

    class Probe:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get(self, *_args):
            return httpx.Response(503)

    monkeypatch.setattr(uvicorn, "Server", UnhealthyServer)
    monkeypatch.setattr(httpx, "Client", Probe)
    with pytest.raises(AssertionError, match="started=True, alive=True, last_probe=HTTP 503"):
        with _running_test_app(timeout=.02):
            pytest.fail("unhealthy HTTP response was accepted")
