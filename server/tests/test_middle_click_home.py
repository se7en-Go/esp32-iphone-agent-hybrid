"""Opt-in BLE middle-button Home navigation tests."""

import asyncio
import json

import pytest
from test_assistive_home import Mirror, frame, make_phone, short_waits  # noqa: F401


class MiddleHomeMirror(Mirror):
    """Test device that supports BLE `click middle` Home transitions."""

    def __init__(self, *, middle_goes_home=True, ble=True, port_name=""):
        super().__init__()
        self.middle_goes_home = middle_goes_home
        self.board_status = {"hid": {"path": "ble" if ble else "usb"}}
        self.port_name = port_name

    async def send(self, command):
        if command == "click middle":
            self.sent.append(command)
            if self.middle_goes_home:
                self.clock.call_later(self.delay, self.show, frame(home=True))
            return
        await super().send(command)


def test_unverified_device_never_sends_middle_click_by_default(tmp_path):
    mirror = MiddleHomeMirror(middle_goes_home=True, ble=True)
    phone = make_phone(tmp_path, mirror)

    asyncio.run(phone.go_home())

    assert "click middle" not in mirror.sent
    assert mirror.sent.count("click") == 2


def test_non_ble_device_ignores_middle_click_flag_and_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HIDAGENT_MIDDLE_CLICK_HOME", "1")
    mirror = MiddleHomeMirror(middle_goes_home=True, ble=False)
    phone = make_phone(tmp_path, mirror)
    phone.assistive_touch.set_middle_click_home_verified(True)

    asyncio.run(phone.go_home())

    assert "click middle" not in mirror.sent
    assert mirror.sent.count("click") == 2


def test_verified_ble_device_uses_middle_click_and_requires_home_frame(tmp_path):
    mirror = MiddleHomeMirror(middle_goes_home=True, ble=True)
    phone = make_phone(tmp_path, mirror)
    phone.assistive_touch.set_middle_click_home_verified(True)

    asyncio.run(phone.go_home())

    assert mirror.sent == ["click middle"]
    saved = json.loads((tmp_path / "calibration.assistive-touch.json").read_text(encoding="utf-8"))
    assert saved["middle_click_home_verified"] is True
    assert saved["verified_route"] == "middle-click"


def test_middle_click_ack_without_home_frame_falls_back_to_assistive_touch(tmp_path, monkeypatch):
    monkeypatch.setenv("HIDAGENT_MIDDLE_CLICK_HOME", "1")
    mirror = MiddleHomeMirror(middle_goes_home=False, ble=True)
    phone = make_phone(tmp_path, mirror)
    phone.assistive_touch.set_middle_click_home_verified(True)

    asyncio.run(phone.go_home())

    assert mirror.sent[0] == "click middle"
    assert mirror.sent.count("click") == 2


def test_middle_click_ack_without_any_visual_home_still_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HIDAGENT_MIDDLE_CLICK_HOME", "1")
    mirror = MiddleHomeMirror(middle_goes_home=False, ble=True)
    mirror.menu_opens = False
    phone = make_phone(tmp_path, mirror)

    with pytest.raises(RuntimeError, match="未确认主屏幕"):
        asyncio.run(phone.go_home())

    assert mirror.sent[0] == "click middle"


def test_already_open_menu_clicks_visible_home_and_skips_middle_click(tmp_path, monkeypatch):
    monkeypatch.setenv("HIDAGENT_MIDDLE_CLICK_HOME", "1")
    mirror = MiddleHomeMirror(middle_goes_home=True, ble=True)
    mirror.frame = frame(menu=True)
    mirror.clicks.append((0, 0))  # align Mirror's second-click target to Home item
    phone = make_phone(tmp_path, mirror)
    phone.assistive_touch.set_middle_click_home_verified(True)

    asyncio.run(phone.go_home())

    assert "click middle" not in mirror.sent
    assert mirror.sent.count("click") == 1


def test_middle_click_verification_is_scoped_per_device(tmp_path):
    mirror_a = MiddleHomeMirror(middle_goes_home=True, ble=True, port_name="/dev/cu.wchusbserialA")
    phone_a = make_phone(tmp_path, mirror_a)
    phone_a.assistive_touch.set_middle_click_home_verified(True)

    asyncio.run(phone_a.go_home())
    assert mirror_a.sent == ["click middle"]

    mirror_b = MiddleHomeMirror(middle_goes_home=True, ble=True, port_name="/dev/cu.wchusbserialB")
    phone_b = make_phone(tmp_path, mirror_b)
    asyncio.run(phone_b.go_home())
    assert "click middle" not in mirror_b.sent
    assert mirror_b.sent.count("click") == 2

    phone_a.assistive_touch.set_middle_click_home_verified(False)
    mirror_a2 = MiddleHomeMirror(middle_goes_home=True, ble=True, port_name="/dev/cu.wchusbserialA")
    phone_a2 = make_phone(tmp_path, mirror_a2)
    asyncio.run(phone_a2.go_home())
    assert "click middle" not in mirror_a2.sent
    assert mirror_a2.sent.count("click") == 2
