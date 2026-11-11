"""熔断挡住 HID，以及网页接口不接受跨站表单。"""

import asyncio
import os
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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
                module._hid_gate(command)
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


def test_stopped_type_returns_409_not_500():
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


def test_real_client_stop_calibrate_home_back_not_415(tmp_path, monkeypatch):
    monkeypatch.setenv("HIDAGENT_PORT", str(tmp_path / "missing-port"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(module.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=0.3)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise AssertionError("real app did not start")
        with IPhoneClient(f"http://127.0.0.1:{port}") as client:
            assert client.stop()["ok"] is True
            _not_unsupported(lambda: client.calibrate())
            _not_unsupported(lambda: client.action("home"))
            _not_unsupported(lambda: client.action("back"))
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_mcp_stop_task_against_the_real_app(tmp_path, monkeypatch):
    monkeypatch.setenv("HIDAGENT_PORT", str(tmp_path / "missing-port"))
    before = _real_guard_bytes()
    saved = (module.guard.tripped, module.guard.reason, list(module.guard.sends), module.guard._path)
    module.guard._path = tmp_path / "guard.json"
    module.guard.tripped = False
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(module.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=0.3)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise AssertionError("real app did not start")

        async def scenario():
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "hidagent.cli", "--url", f"http://127.0.0.1:{port}", "mcp"],
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
        server.should_exit = True
        thread.join(timeout=5)
        module.guard.tripped, module.guard.reason, module.guard.sends, module.guard._path = saved
        assert _real_guard_bytes() == before
