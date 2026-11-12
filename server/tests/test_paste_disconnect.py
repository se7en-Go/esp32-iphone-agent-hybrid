import asyncio
import io
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from hidagent.clipboard import CompanionClipboard
from hidagent.control import Controls
from hidagent.guard import Guard
from hidagent.phone import Phone


class Device:
    connected = False
    abort_epoch = 0

    def __init__(self):
        self.sent = []

    async def send(self, command):
        self.sent.append(command)


class Screen:
    live = True
    def __init__(self):
        self.updated = time.time()
        data = io.BytesIO()
        Image.new("RGB", (101, 201), "white").save(data, "JPEG")
        self.frame = data.getvalue()

    async def fresh(self, *_args):
        return self.frame

    async def settled(self, **_kwargs):
        pass


@pytest.mark.parametrize("path", ["/api/type", "/api/control/action", "/api/control/batch"])
@pytest.mark.parametrize("phase", ["published", "fetched", "ack_race", "paste_sent"])
def test_http_disconnect_invalidates_paste_token(tmp_path, monkeypatch, path, phase):
    from hidagent import app as module

    device, screen = Device(), Screen()
    provider = CompanionClipboard("http://test", "x" * 40)
    phone = Phone(device, tmp_path / "cal.json", screen, clipboard=provider)
    controls = Controls(phone, screen)
    monkeypatch.setattr(module, "phone", phone)
    monkeypatch.setattr(module, "device", device)
    monkeypatch.setattr(module, "screen", screen)
    monkeypatch.setattr(module, "controls", controls)
    monkeypatch.setattr(module, "guard", Guard())
    monkeypatch.setattr(module, "manual_lock", asyncio.Lock())
    monkeypatch.setattr(module, "agent", SimpleNamespace(running=False))

    async def scenario():
        body = {"text": "断连后不可粘贴", "mode": "paste"}
        if path.startswith("/api/control/"):
            token = controls.observation()["screen_id"]
            body = ({"screen_id": token, "steps": [{"action": "type", **body}]}
                    if path.endswith("batch") else {"screen_id": token, "action": "type", **body})
        encoded = json.dumps(body).encode()
        incoming = asyncio.Queue()
        incoming.put_nowait({"type": "http.request", "body": encoded, "more_body": False})
        disconnected = False

        async def receive():
            nonlocal disconnected
            if disconnected:
                return {"type": "http.disconnect"}
            message = await incoming.get()
            disconnected = message["type"] == "http.disconnect"
            return message

        async def send(_message):
            pass

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
                 "http_version": "1.1", "method": "POST", "scheme": "http", "path": path,
                 "raw_path": path.encode(), "root_path": "", "query_string": b"",
                 "headers": [(b"content-type", b"application/json"),
                             (b"content-length", str(len(encoded)).encode())],
                 "server": ("test", 80), "client": ("test", 1234)}
        request_task = asyncio.create_task(module.app(scope, receive, send))
        try:
            for _ in range(100):
                item = provider.pending()
                if item:
                    break
                await asyncio.sleep(.01)
            else:
                raise AssertionError("paste did not publish a pending item")
            original = provider._pending
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                         base_url="http://test") as client:
                if phase != "published":
                    assert (await client.get(item["text_url"])).status_code == 200
                if phase == "paste_sent":
                    ready = await client.post(item["ack_url"], json={"copied": True, "focused": True})
                    assert ready.status_code == 200
                    for _ in range(100):
                        if "key cmd+v" in device.sent:
                            break
                        await asyncio.sleep(.01)
                    else:
                        raise AssertionError("paste key was not sent")
                incoming.put_nowait({"type": "http.disconnect"})
                if phase == "ack_race":
                    raced = await client.post(item["ack_url"], json={"copied": True, "focused": True})
                    assert raced.status_code in (200, 404)
                await asyncio.wait_for(request_task, 1)
                assert provider._pending is None
                assert original.text is None
                assert not module.manual_lock.locked()
                assert (await client.get(item["text_url"])).status_code == 404
                assert (await client.get(item["status_url"])).status_code == 404
                assert (await client.post(item["ack_url"], json={"copied": True, "focused": True})).status_code == 404
                if phase == "paste_sent":
                    completion = await client.post(item["complete_url"], json={"pasted": True})
                    assert completion.status_code == 404
                    current = await client.get("/api/clipboard/current",
                                               headers={"Authorization": "Bearer " + "x" * 40})
                    assert current.json() == {"active": False, "needs_resolution": True}
                    retry = await asyncio.wait_for(
                        client.post("/api/type", json={"text": "不得覆盖", "mode": "paste"}), 1)
                    assert retry.status_code == 409
                    assert provider._pending is None
                    assert device.sent.count("key cmd+v") == 1
                else:
                    assert "key cmd+v" not in device.sent
        finally:
            if not request_task.done():
                request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)

    asyncio.run(scenario())
