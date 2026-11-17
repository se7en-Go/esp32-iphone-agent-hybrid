"""Backward-compatible BLE middle-button Home navigation and enable/revoke flow."""

import asyncio
import json

import httpx
import pytest
from test_assistive_home import Mirror, frame, make_phone, short_waits  # noqa: F401

from hidagent import app as app_module
from hidagent.control import Controls
from hidagent.device import DeviceError
from hidagent.guard import Guard
from hidagent.messages import explain_reply


class MiddleHomeMirror(Mirror):
    """Test device with explicit firmware capability, port identity, and legacy behavior switches."""

    generation = 1
    connected = True
    input_sequence = 0
    last_input_at = 0.0
    session_started_at = 0.0
    recent_frames = ()

    def __init__(
        self,
        *,
        middle_goes_home=True,
        ble=True,
        firmware_middle_click=True,
        legacy_firmware_left_clicks_on_arg=False,
        port_name="/dev/cu.usbserial-TESTA",
    ):
        super().__init__()
        self.middle_goes_home = middle_goes_home
        self.legacy_firmware_left_clicks_on_arg = legacy_firmware_left_clicks_on_arg
        self.unpositioned_left_clicks = 0
        self.port_name = port_name
        hid = {"path": "ble" if ble else "usb", "pace": 4}
        if firmware_middle_click:
            hid["middle_click"] = 1
        self.board_status = {"hid": hid}

    async def refresh_hid_status(self):
        return None

    async def send(self, command):
        self.input_sequence += 1
        self.last_input_at = self.clock.wall_time if hasattr(self.clock, "wall_time") else 0.0
        if self.legacy_firmware_left_clicks_on_arg:
            if command.startswith("click "):
                self.sent.append(command)
                self.unpositioned_left_clicks += 1
                return
            if command in {"middleclick", "middlepress"}:
                self.sent.append(command)
                raise DeviceError(explain_reply(command, "err unknown command"))
        if command == "middleclick":
            self.sent.append(command)
            if self.middle_goes_home:
                self.clock.call_later(self.delay, self.show, frame(home=True))
            return
        await super().send(command)


def test_old_firmware_never_emits_left_click_or_allows_enable(tmp_path):
    """Old firmware lacking `middle_click=1` is refused upfront and falls back cleanly."""
    mirror = MiddleHomeMirror(
        middle_goes_home=True,
        ble=True,
        firmware_middle_click=False,
        legacy_firmware_left_clicks_on_arg=True,
        port_name="/dev/cu.wchusbserialOLD",
    )
    # Even if an earlier profile persisted this port, lacking firmware capability disables it.
    (tmp_path / "calibration.assistive-touch.json").write_text(
        json.dumps({"middle_click_home_devices": [mirror.port_name], "verified_route": "middle-click"}),
        encoding="utf-8",
    )
    phone = make_phone(tmp_path, mirror)

    status = phone.assistive_touch.middle_click_home_status()
    assert status["supported_by_firmware"] is False
    assert status["enabled"] is False

    asyncio.run(phone.go_home())

    assert "middleclick" not in mirror.sent
    assert "click middle" not in mirror.sent
    assert mirror.unpositioned_left_clicks == 0
    assert mirror.sent.count("click") == 2

    with pytest.raises(ValueError, match="middle_click=1"):
        asyncio.run(phone.assistive_touch.verify_and_enable_middle_click_home())
    assert mirror.unpositioned_left_clicks == 0


def test_old_firmware_protocol_rejects_middleclick_with_zero_left_clicks_even_if_status_stale(tmp_path):
    """If cached status falsely claimed capability, old 049ab01 firmware rejects `middleclick` without clicking."""
    mirror = MiddleHomeMirror(
        middle_goes_home=False,
        ble=True,
        firmware_middle_click=True,
        legacy_firmware_left_clicks_on_arg=True,
        port_name="/dev/cu.wchusbserialSTALE",
    )
    (tmp_path / "calibration.assistive-touch.json").write_text(
        json.dumps({"middle_click_home_devices": [mirror.port_name], "verified_route": "middle-click"}),
        encoding="utf-8",
    )
    phone = make_phone(tmp_path, mirror)

    asyncio.run(phone.go_home())

    assert mirror.sent[0] == "middleclick"
    assert "click middle" not in mirror.sent
    assert mirror.unpositioned_left_clicks == 0
    assert mirror.sent.count("click") == 2


def test_device_swap_does_not_inherit_previous_board_verification(tmp_path):
    """Swapping to another board (different port_name) keeps middle-click disabled until verified."""
    mirror_a = MiddleHomeMirror(
        middle_goes_home=True, ble=True, firmware_middle_click=True, port_name="/dev/cu.wchusbserialA"
    )
    phone_a = make_phone(tmp_path, mirror_a)
    asyncio.run(phone_a.assistive_touch.verify_and_enable_middle_click_home())
    assert phone_a.assistive_touch.middle_click_home_status()["enabled"] is True

    # Verify board A uses middleclick.
    mirror_a.show(frame())
    mirror_a.sent.clear()
    asyncio.run(phone_a.go_home())
    assert mirror_a.sent == ["middleclick"]

    # Swap to board B: new firmware, different port_name, never verified.
    mirror_b = MiddleHomeMirror(
        middle_goes_home=True, ble=True, firmware_middle_click=True, port_name="/dev/cu.wchusbserialB"
    )
    phone_b = make_phone(tmp_path, mirror_b)
    status_b = phone_b.assistive_touch.middle_click_home_status()
    assert status_b["supported_by_firmware"] is True
    assert status_b["verified_for_device"] is False
    assert status_b["enabled"] is False

    asyncio.run(phone_b.go_home())
    assert "middleclick" not in mirror_b.sent
    assert mirror_b.sent.count("click") == 2

    # Unidentified device (empty port_name) can never enable middle-click globally.
    mirror_anon = MiddleHomeMirror(
        middle_goes_home=True, ble=True, firmware_middle_click=True, port_name=""
    )
    phone_anon = make_phone(tmp_path, mirror_anon)
    with pytest.raises(ValueError, match="port_name"):
        asyncio.run(phone_anon.assistive_touch.verify_and_enable_middle_click_home())
    assert phone_anon.assistive_touch.middle_click_home_status()["enabled"] is False


def test_unverified_mapping_refuses_enable_and_never_uses_middle_click(tmp_path):
    """New firmware without verified iOS mapping neither persists enable nor runs middleclick in go_home."""
    mirror = MiddleHomeMirror(
        middle_goes_home=False, ble=True, firmware_middle_click=True, port_name="/dev/cu.wchusbserialUNMAPPED"
    )
    phone = make_phone(tmp_path, mirror)

    # Default unverified go_home skips middleclick completely.
    asyncio.run(phone.go_home())
    assert "middleclick" not in mirror.sent
    assert mirror.sent.count("click") == 2

    # Reset to Notes and try explicit enable when iPhone has not bound Button 3 -> Home.
    mirror.show(frame())
    mirror.sent.clear()
    mirror.clicks.clear()
    with pytest.raises(RuntimeError, match="未确认返回主屏幕"):
        asyncio.run(phone.assistive_touch.verify_and_enable_middle_click_home())

    assert mirror.sent == ["middleclick"]
    assert phone.assistive_touch.middle_click_home_status()["enabled"] is False
    profile = json.loads((tmp_path / "calibration.assistive-touch.json").read_text(encoding="utf-8"))
    assert mirror.port_name not in (profile.get("middle_click_home_devices") or [])

    # Subsequent go_home still falls back safely to AssistiveTouch.
    mirror.show(frame())
    mirror.sent.clear()
    mirror.clicks.clear()
    asyncio.run(phone.go_home())
    assert "middleclick" not in mirror.sent
    assert mirror.sent.count("click") == 2


def test_enable_from_home_or_open_menu_is_rejected_before_sending(tmp_path):
    mirror = MiddleHomeMirror(
        middle_goes_home=True, ble=True, firmware_middle_click=True, port_name="/dev/cu.wchusbserialPRE"
    )
    phone = make_phone(tmp_path, mirror)

    mirror.show(frame(home=True))
    with pytest.raises(ValueError, match="主屏幕"):
        asyncio.run(phone.assistive_touch.verify_and_enable_middle_click_home())
    assert mirror.sent == []

    mirror.show(frame(menu=True))
    with pytest.raises(ValueError, match="菜单"):
        asyncio.run(phone.assistive_touch.verify_and_enable_middle_click_home())
    assert mirror.sent == []


def test_non_ble_device_cannot_enable_or_use_middle_click(tmp_path, monkeypatch):
    monkeypatch.setenv("HIDAGENT_MIDDLE_CLICK_HOME", "1")  # ignored env var must not bypass verification
    mirror = MiddleHomeMirror(
        middle_goes_home=True, ble=False, firmware_middle_click=True, port_name="/dev/cu.wchusbserialUSB"
    )
    phone = make_phone(tmp_path, mirror)
    assert phone.assistive_touch.middle_click_home_status()["enabled"] is False

    with pytest.raises(ValueError, match="蓝牙"):
        asyncio.run(phone.assistive_touch.verify_and_enable_middle_click_home())

    asyncio.run(phone.go_home())
    assert "middleclick" not in mirror.sent
    assert mirror.sent.count("click") == 2


def test_verified_enable_home_and_revoke_via_http_api(tmp_path, monkeypatch):
    """End-to-end API flow: status -> enable -> persistent middleclick go_home -> revoke."""
    mirror = MiddleHomeMirror(
        middle_goes_home=True, ble=True, firmware_middle_click=True, port_name="/dev/cu.wchusbserialAPI"
    )
    phone = make_phone(tmp_path, mirror)
    controls = Controls(phone, mirror)
    guard = Guard(path=tmp_path / "guard.json")
    for name, value in {
        "device": mirror,
        "phone": phone,
        "screen": mirror,
        "controls": controls,
        "guard": guard,
        "agent": type("A", (), {"running": False})(),
        "manual_lock": asyncio.Lock(),
    }.items():
        monkeypatch.setattr(app_module, name, value)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module.app), base_url="http://testserver"
        ) as client:
            initial = (await client.get("/api/control/middle-click-home")).json()
            assert initial["supported_by_firmware"] is True
            assert initial["verified_for_device"] is False
            assert initial["enabled"] is False

            caps = (await client.get("/api/control/capabilities")).json()
            assert caps["features"]["middle_click_home"]["enabled"] is False

            # Observe Notes (menu closed) and enable via HTTP API.
            obs = controls.observation()
            res = await client.post(
                "/api/control/middle-click-home/enable",
                json={"screen_id": obs["screen_id"]},
            )
            assert res.status_code == 200
            body = res.json()
            assert body["effect_verified"] is True
            assert body["middle_click_home"]["enabled"] is True
            assert body["middle_click_home"]["verified_for_device"] is True
            assert mirror.sent == ["middleclick"]

            # Return to Notes and run normal guarded `home`: uses middleclick directly.
            mirror.show(frame())
            mirror.sent.clear()
            obs2 = controls.observation()
            home_res = await client.post(
                "/api/control/action",
                json={"action": "home", "screen_id": obs2["screen_id"]},
            )
            assert home_res.status_code == 200
            assert home_res.json()["command_acknowledged"] is True
            assert mirror.sent == ["middleclick"]

            # Revoke via supported API and confirm fallback returns to AssistiveTouch.
            rev = await client.post("/api/control/middle-click-home/revoke", json={})
            assert rev.status_code == 200
            assert rev.json()["enabled"] is False
            assert rev.json()["verified_for_device"] is False

            mirror.show(frame())
            mirror.sent.clear()
            obs3 = controls.observation()
            home_res2 = await client.post(
                "/api/control/action",
                json={"action": "home", "screen_id": obs3["screen_id"]},
            )
            assert home_res2.status_code == 200
            assert home_res2.json()["command_acknowledged"] is True
            assert "middleclick" not in mirror.sent
            assert mirror.sent.count("click") == 2

    asyncio.run(exercise())


def test_already_open_menu_clicks_visible_home_and_skips_middle_click(tmp_path):
    mirror = MiddleHomeMirror(
        middle_goes_home=True, ble=True, firmware_middle_click=True, port_name="/dev/cu.wchusbserialMENU"
    )
    phone = make_phone(tmp_path, mirror)
    phone.assistive_touch.set_middle_click_home_verified(True)
    mirror.show(frame(menu=True))
    mirror.clicks.append((0, 0))  # align Mirror's second-click target to Home item

    asyncio.run(phone.go_home())

    assert "middleclick" not in mirror.sent
    assert mirror.sent.count("click") == 1
