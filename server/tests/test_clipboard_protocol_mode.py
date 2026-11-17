"""A staged-only Shortcut must not receive an unsupported synchronous request."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from test_paste_input import Device, Screen

from hidagent.clipboard import CompanionClipboard
from hidagent.control import Controls
from hidagent.device import DeviceError
from hidagent.phone import Phone
from hidagent.text_input import clipboard_mode_options, select_type_mode

BASE = "http://clipboard.test:8891"
KEY = "test_pairing_key_not_a_secret_123456789"


def staged():
    return CompanionClipboard(BASE, KEY, protocol="staged-v1")


def test_default_protocol_preserves_synchronous_legacy_mode():
    provider = CompanionClipboard(BASE, KEY)
    assert provider.protocol == "legacy-v1" and provider.legacy_paste_available
    assert select_type_mode("ASCII", **clipboard_mode_options(provider)) == "paste"
    assert select_type_mode("中文", **clipboard_mode_options(provider)) == "paste"


@pytest.mark.parametrize("text", ["ASCII", " 00123 ", " spaces remain "])
def test_staged_only_auto_ascii_uses_hid_keys_without_pending_copy(tmp_path, text):
    provider, device = staged(), Device()
    phone = Phone(device, tmp_path / "cal.json", clipboard=provider)
    asyncio.run(phone.type(text))
    assert device.sent == ["type " + text]
    assert provider.pending() is None and not provider.current()["active"]


@pytest.mark.parametrize("text,mode", [("中文", "auto"), ("你好\n下一行", "auto"),
                                     ("first\nsecond", "auto"), ("ASCII", "paste"), ("中文", "paste")])
def test_staged_only_sync_requests_fail_fast_without_hid_or_pending(tmp_path, text, mode):
    provider, device = staged(), Device()
    provider.trigger_key = "ctrl+alt+p"
    phone = Phone(device, tmp_path / "cal.json", clipboard=provider)
    with pytest.raises(DeviceError, match="分阶段.*prepare"):
        asyncio.run(asyncio.wait_for(phone.type(text, mode=mode), .2))
    assert not device.sent and provider.pending() is None
    assert not provider.current()["active"] and not provider._operation_active


def test_provider_itself_rejects_sync_paste_before_reserving_or_triggering():
    provider = staged()
    commands = []
    async def send(command):
        commands.append(command)
    with pytest.raises(DeviceError, match="分阶段"):
        asyncio.run(asyncio.wait_for(provider.paste("你好", stopped=lambda: False, send=send), .2))
    assert not commands and provider.pending() is None and not provider._operation_active


def test_staged_only_provider_still_accepts_actual_staged_transaction():
    async def run():
        provider = staged()
        nonce = provider.stage_begin("你好\n00123")
        pending = provider.pending()
        assert pending["protocol"] == "staged-v1"
        token = pending["text_url"].rsplit("/", 1)[-1]
        assert provider.fetch_payload(token) == {"copy_allowed": True, "text": "你好\n00123"}
        provider.acknowledge(token, copied=True, focused=False)
        assert provider.stage_status(nonce)["phase"] == "awaiting_paste"
        provider.stage_finish(nonce)
    asyncio.run(run())


@pytest.mark.parametrize("protocol", ["staged-v1", "legacy-v1"])
def test_environment_protocol_selection(monkeypatch, protocol):
    monkeypatch.setenv("HIDAGENT_CLIPBOARD_BASE_URL", BASE)
    monkeypatch.setenv("HIDAGENT_CLIPBOARD_KEY", KEY)
    monkeypatch.setenv("HIDAGENT_CLIPBOARD_PROTOCOL", protocol)
    provider = CompanionClipboard.from_env()
    assert provider.protocol == protocol
    assert provider.legacy_paste_available is (protocol == "legacy-v1")


def test_invalid_protocol_is_rejected_before_managed_services_start():
    pytest.importorskip("fcntl")  # The managed supervisor is Unix-only; all other protocol tests are portable.
    from hidagent.local_runtime import clipboard_proxy_config

    with pytest.raises(ValueError, match="HIDAGENT_CLIPBOARD_PROTOCOL"):
        clipboard_proxy_config({"HIDAGENT_CLIPBOARD_BASE_URL": BASE, "HIDAGENT_CLIPBOARD_KEY": KEY,
                                "HIDAGENT_CLIPBOARD_PROTOCOL": "staged-typo"})


def test_controls_preflight_distinguishes_keys_and_staged_paste():
    from hidagent.app import ActionStep

    provider, device = staged(), Device()
    controls = Controls(SimpleNamespace(clipboard=provider), Screen())
    assert controls._plan(ActionStep(action="type", text="00123"), 101, 201)["selected_mode"] == "keys"
    for step in (ActionStep(action="type", text="中文"), ActionStep(action="type", text="ASCII", mode="paste")):
        with pytest.raises(ValueError, match="分阶段"):
            controls._plan(step, 101, 201)
    assert not device.sent and provider.pending() is None


def test_http_sync_paste_rejects_before_observation_and_reports_real_capability(monkeypatch):
    from hidagent import app as api

    provider = staged()
    monkeypatch.setattr(api, "phone", SimpleNamespace(clipboard=provider, app_search_available=False))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://local") as client:
            for body in ({"text": "ASCII", "mode": "paste"}, {"text": "中文", "mode": "auto"}):
                result = await client.post("/api/type", json=body)
                assert result.status_code == 422 and "分阶段" in result.json()["detail"]
            features = (await client.get("/api/control/capabilities")).json()["features"]["clipboard"]
            assert features["configured"] and features["staged_available"]
            assert features["protocol"] == "staged-v1"
            assert features["legacy_paste_available"] is False and features["trigger_configured"] is False
        assert provider.pending() is None
    asyncio.run(run())
