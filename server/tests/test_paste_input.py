import asyncio
import io
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from hidagent.device import DeviceError
from hidagent.phone import TYPE_LIMIT, Phone
from hidagent.text_input import select_type_mode


class Device:
    connected = False
    abort_epoch = 0

    def __init__(self):
        self.sent = []

    async def send(self, command):
        self.sent.append(command)

    def request_abort(self):
        self.abort_epoch += 1


class Clipboard:
    def __init__(self, error=None):
        self.copied = []
        self.error = error

    async def paste(self, text, *, stopped, send):
        assert not stopped()
        self.copied.append(text)
        if self.error:
            raise self.error
        await send("key cmd+v")


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


@pytest.mark.parametrize("text,mode,configured,expected", [
    ("13267179555", "auto", False, "keys"),
    ("hello", "auto", False, "keys"),
    ("中文", "auto", True, "paste"),
    ("13267179555", "auto", True, "paste"),
    ("hello", "paste", True, "paste"),
    ("123", "keys", True, "keys"),
])
def test_mode_selection(text, mode, configured, expected):
    assert select_type_mode(text, mode, clipboard_available=configured) == expected


@pytest.mark.parametrize("text,mode,configured", [
    ("中文", "auto", False), ("中文", "keys", True), ("123", "paste", False),
])
def test_missing_clipboard_or_ascii_limit_explains_setup(tmp_path, text, mode, configured):
    device = Device()
    phone = Phone(device, tmp_path / "cal.json", clipboard=Clipboard() if configured else None)
    with pytest.raises(DeviceError, match="docs/PASTE_INPUT.md"):
        asyncio.run(phone.type(text, mode=mode))
    assert device.sent == []


@pytest.mark.parametrize("text", ["", "x" * 4001, "\x00", "\x7f", "\x85", "\ud800"])
def test_invalid_text_never_reaches_clipboard_or_hid(tmp_path, text):
    device, clipboard = Device(), Clipboard()
    phone = Phone(device, tmp_path / "cal.json", clipboard=clipboard)
    with pytest.raises(DeviceError):
        asyncio.run(phone.type(text))
    assert not clipboard.copied and not device.sent


@pytest.mark.parametrize("mode", ["paste", "auto"])
def test_paste_preserves_multiline_text_and_tabs_without_pressing_enter(tmp_path, mode):
    device, clipboard = Device(), Clipboard()
    phone = Phone(device, tmp_path / "cal.json", clipboard=clipboard)
    text = " 00123\t中文\r\n第二行\nkey enter\r末行 "
    asyncio.run(phone.type(text, mode=mode))
    assert clipboard.copied == [text]
    assert device.sent == ["key cmd+v"]


@pytest.mark.parametrize("mode,configured", [("keys", False), ("keys", True), ("auto", False)])
@pytest.mark.parametrize("separator", ["\n", "\r", "\t"])
def test_keys_reject_line_breaks_and_tabs_before_any_hid_write(tmp_path, mode, configured, separator):
    device, clipboard = Device(), Clipboard()
    phone = Phone(device, tmp_path / "cal.json", clipboard=clipboard if configured else None)
    with pytest.raises(DeviceError, match="控制字符"):
        asyncio.run(phone.type("first" + separator + "second", mode=mode))
    assert not clipboard.copied and not device.sent


@pytest.mark.parametrize("mode", ["keys", "paste", "auto"])
@pytest.mark.parametrize("control", ["\x00", "\x01", "\x0b", "\x0c", "\x1b", "\x7f", "\x80", "\x85", "\x9f",
                                     "\ud800", "\udfff", "\u2028", "\u2029"])
def test_all_input_modes_reject_other_controls_and_surrogates(mode, control):
    with pytest.raises(ValueError, match="控制字符"):
        select_type_mode("text" + control, mode, clipboard_available=True)


@pytest.mark.parametrize("text,mode", [("你好，123 👩‍💻", "auto"), ("13267179555", "auto"), ("中文", "paste")])
def test_confirmed_clipboard_uses_one_paste_command(tmp_path, text, mode):
    device, clipboard = Device(), Clipboard()
    phone = Phone(device, tmp_path / "cal.json", clipboard=clipboard)
    asyncio.run(phone.type(text, mode=mode))
    assert clipboard.copied == [text]
    assert device.sent == ["key cmd+v"]


@pytest.mark.parametrize("configured", [False, True])
def test_ascii_fallback_and_explicit_keys_preserve_chunks(tmp_path, configured):
    device, clipboard = Device(), Clipboard()
    text = " 00123 " * 190
    phone = Phone(device, tmp_path / "cal.json", clipboard=clipboard if configured else None)
    asyncio.run(phone.type(text, mode="keys" if configured else "auto"))
    assert not clipboard.copied
    assert len(device.sent) > 1
    assert "".join(command.removeprefix("type ") for command in device.sent) == text
    assert all(len(command.encode()) <= TYPE_LIMIT + len("type ") for command in device.sent)


def test_started_paste_failure_never_retries_ascii_keys(tmp_path):
    device, clipboard = Device(), Clipboard(DeviceError("剪贴板确认超时"))
    phone = Phone(device, tmp_path / "cal.json", clipboard=clipboard)
    with pytest.raises(DeviceError, match="超时"):
        asyncio.run(phone.type("13267179555"))
    assert clipboard.copied == ["13267179555"]
    assert device.sent == []


def test_cancel_waiting_for_copy_sends_nothing(tmp_path):
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def paste(text, *, stopped, send):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        device = Device()
        phone = Phone(device, tmp_path / "cal.json", clipboard=SimpleNamespace(paste=paste))
        task = asyncio.create_task(phone.type("中文"))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set() and device.sent == []

    asyncio.run(scenario())


def test_stop_after_copy_confirmation_prevents_paste(tmp_path):
    async def scenario():
        device = Device()
        copied = asyncio.Event()

        release = asyncio.Event()

        async def paste(text, *, stopped, send):
            copied.set()
            await release.wait()
            await send("key cmd+v")

        phone = Phone(device, tmp_path / "cal.json", clipboard=SimpleNamespace(paste=paste))
        task = asyncio.create_task(phone.type("中文"))
        await asyncio.wait_for(copied.wait(), 1)
        device.request_abort()
        release.set()
        with pytest.raises(DeviceError, match="紧急停止"):
            await task
        assert device.sent == []

    asyncio.run(scenario())


def test_typing_serializes_until_first_paste_is_completed(tmp_path):
    async def scenario():
        device = Device()
        entered, release = asyncio.Event(), asyncio.Event()
        copied = []

        async def paste(text, *, stopped, send):
            copied.append(text)
            await send("key cmd+v")
            if text == "first":
                entered.set()
                await release.wait()
            else:
                assert device.sent == ["key cmd+v", "key cmd+v"]

        phone = Phone(device, tmp_path / "cal.json", clipboard=SimpleNamespace(paste=paste))
        first = asyncio.create_task(phone.type("first"))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(phone.type("second"))
        await asyncio.sleep(0)
        assert copied == ["first"]
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert copied == ["first", "second"]
        assert device.sent == ["key cmd+v", "key cmd+v"]

    asyncio.run(scenario())


def test_control_batch_stops_before_more_input_and_preserves_separate_pinyin_keys(tmp_path):
    from hidagent.app import ControlAction
    from hidagent.control import Controls

    device, clipboard = Device(), Clipboard()
    phone = Phone(device, tmp_path / "cal.json", clipboard=clipboard)
    control = Controls(phone, Screen())

    async def scenario():
        token = control.observation()["screen_id"]
        requests = [ControlAction(action="type", screen_id=token, text="中文", mode="paste"),
                    ControlAction(action="type", screen_id=token, text="123", mode="keys"),
                    ControlAction(action="compose_chinese", screen_id=token, text="你好", input_mode="pinyin")]
        result = await control.perform_batch(token, requests)
        assert result["completed"] == 1 and "new observation" in result["error"]
        assert device.sent == ["key cmd+v"]
        # The steering deliberately requires another observation for each input step.
        for request in requests[1:]:
            request.screen_id = control.observation()["screen_id"]
            assert (await control.perform(request))["command_acknowledged"]

    asyncio.run(scenario())
    assert clipboard.copied == ["中文"]
    assert device.sent == ["key cmd+v", "type 123", "type nihao"]


@pytest.mark.parametrize("modes", [("paste", "paste"), ("auto", "paste"), ("auto", "auto")])
def test_control_batch_rejects_multiple_pastes_before_any_action(tmp_path, modes):
    from hidagent.app import ControlAction
    from hidagent.control import Controls

    device, clipboard = Device(), Clipboard()
    phone = Phone(device, tmp_path / "cal.json", clipboard=clipboard)
    control = Controls(phone, Screen())

    async def scenario():
        token = control.observation()["screen_id"]
        requests = [ControlAction(action="key", screen_id=token, text="tab")]
        requests += [ControlAction(action="type", screen_id=token, text=text, mode=mode)
                     for text, mode in zip(("first", "second"), modes, strict=True)]
        with pytest.raises(ValueError, match="粘贴"):
            await control.perform_batch(token, requests)

    asyncio.run(scenario())
    assert clipboard.copied == []
    assert device.sent == []


@pytest.fixture
def app_phone(tmp_path, monkeypatch):
    from hidagent import app as module
    from hidagent.control import Controls
    from hidagent.guard import Guard

    device, clipboard, screen = Device(), Clipboard(), Screen()
    phone = Phone(device, tmp_path / "cal.json", screen, clipboard=clipboard)
    monkeypatch.setattr(module, "phone", phone)
    monkeypatch.setattr(module, "device", device)
    monkeypatch.setattr(module, "screen", screen)
    monkeypatch.setattr(module, "controls", Controls(phone, screen))
    monkeypatch.setattr(module, "guard", Guard())
    monkeypatch.setattr(module, "manual_lock", asyncio.Lock())
    monkeypatch.setattr(module, "agent", SimpleNamespace(running=False, stop=lambda: None))
    return module, phone, device, clipboard


def test_api_modes_and_invalid_mode(app_phone):
    module, _phone, device, clipboard = app_phone

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
            assert (await client.post("/api/type", json={"text": "中文", "mode": "paste"})).status_code == 200
            assert (await client.post("/api/type", json={"text": "123", "mode": "keys"})).status_code == 200
            assert (await client.post("/api/type", json={"text": "中文", "mode": "keys"})).status_code == 422
            assert (await client.post("/api/type", json={"text": "123", "mode": "unknown"})).status_code == 422

    asyncio.run(scenario())
    assert clipboard.copied == ["中文"]
    assert device.sent == ["key cmd+v", "type 123"]


def test_companion_api_fetch_does_not_paste_until_ack(app_phone):
    from hidagent.clipboard import CompanionClipboard

    module, phone, device, _clipboard = app_phone
    secret = "a" * 40
    phone.clipboard = CompanionClipboard("http://test", secret)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
            typing = asyncio.create_task(client.post("/api/type", json={"text": "你好 00123", "mode": "paste"}))
            for _ in range(100):
                pending = await client.get("/api/clipboard/pending", headers={"Authorization": f"Bearer {secret}"})
                payload = pending.json()
                if payload.get("pending"):
                    break
                await asyncio.sleep(.01)
            else:
                raise AssertionError("no pending paste")
            assert not device.sent and not typing.done()
            fetched = await client.get(payload["text_url"])
            assert fetched.text == "你好 00123"
            assert fetched.headers["cache-control"] == "no-store"
            assert (await client.get(payload["text_url"])).status_code >= 400
            assert not device.sent and not typing.done()
            assert (await client.post(payload["ack_url"],
                                      json={"copied": True, "focused": True})).status_code == 200
            for _ in range(100):
                status = await client.get(payload["status_url"])
                if status.json().get("paste_sent"):
                    break
                await asyncio.sleep(.01)
            else:
                raise AssertionError("paste was not sent")
            assert not typing.done()
            assert (await client.post(payload["complete_url"], json={"pasted": True})).status_code == 200
            result = await asyncio.wait_for(typing, 2)
            assert result.status_code == 200
            assert device.sent == ["key cmd+v"]
            assert (await client.post(payload["ack_url"],
                                      json={"copied": True, "focused": True})).status_code >= 400

    asyncio.run(scenario())


def test_stop_waiting_for_companion_clears_pending_without_paste(app_phone):
    from hidagent.clipboard import CompanionClipboard

    module, phone, device, _clipboard = app_phone
    secret = "b" * 40
    phone.clipboard = CompanionClipboard("http://test", secret)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
            typing = asyncio.create_task(client.post("/api/type", json={"text": "中文"}))
            for _ in range(100):
                pending = await client.get("/api/clipboard/pending", headers={"Authorization": f"Bearer {secret}"})
                if pending.json().get("pending"):
                    break
                await asyncio.sleep(.01)
            else:
                raise AssertionError("no pending paste")
            assert (await client.post("/api/agent/stop", json={})).status_code == 200
            result = await asyncio.wait_for(typing, 1)
            assert result.status_code == 409 and "紧急停止" in result.json()["detail"]
            idle = await client.get("/api/clipboard/pending", headers={"Authorization": f"Bearer {secret}"})
            assert idle.json() == {"pending": False}
            assert device.sent == []

    asyncio.run(scenario())


def test_cli_type_mode_reaches_http(monkeypatch, capsys):
    from hidagent import cli
    from hidagent.client import IPhoneClient

    requests = []

    def respond(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(cli, "IPhoneClient", lambda url: IPhoneClient(url, transport=httpx.MockTransport(respond)))
    assert cli.main(["--url", "http://test", "type", "你好", "--mode", "paste"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    assert requests == [("/api/type", {"text": "你好", "mode": "paste"})]


def test_mcp_type_mode_reaches_control_http(monkeypatch):
    pytest.importorskip("mcp")
    from hidagent import mcp
    from hidagent.client import IPhoneClient

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"command_acknowledged": True})

    monkeypatch.setattr(mcp, "IPhoneClient", lambda url: IPhoneClient(url, transport=httpx.MockTransport(respond)))

    async def scenario():
        server = mcp.create_server("http://test")
        await server.call_tool("iphone_type", {"text": "中文", "screen_id": "observed", "mode": "paste"})

    asyncio.run(scenario())
    assert requests == [{"action": "type", "screen_id": "observed", "text": "中文", "mode": "paste"}]
