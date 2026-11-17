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
        peer_id=1,
        pair_seq=1,
        bonded=True,
        pairing=False,
    ):
        super().__init__()
        self.middle_goes_home = middle_goes_home
        self.legacy_firmware_left_clicks_on_arg = legacy_firmware_left_clicks_on_arg
        self.unpositioned_left_clicks = 0
        self.port_name = port_name
        self._firmware_middle_click = firmware_middle_click
        self._live_hid = self._make_hid_status(
            ble=ble,
            firmware_middle_click=firmware_middle_click,
            peer_id=peer_id,
            pair_seq=pair_seq,
            bonded=bonded,
            pairing=pairing,
        )
        self.board_status = {"hid": dict(self._live_hid, ble=dict(self._live_hid.get("ble", {})))}

    @staticmethod
    def _make_hid_status(
        *,
        ble: bool,
        firmware_middle_click: bool,
        peer_id: int,
        pair_seq: int,
        bonded: bool,
        pairing: bool,
    ) -> dict:
        hid: dict = {"path": "ble" if ble else "usb", "pace": 4}
        if firmware_middle_click:
            hid["middle_click"] = 1
            hid["ble"] = {
                "adv": 0,
                "conn": 1 if ble else 0,
                "bond": 1 if (ble or bonded) else 0,
                "pair": 1 if pairing else 0,
                "peer": peer_id if ble else 0,
                "pair_seq": pair_seq,
            }
        return hid

    def set_live_ble_state(
        self,
        *,
        ble=True,
        peer_id=1,
        pair_seq=1,
        bonded=True,
        pairing=False,
        middle_goes_home=None,
        refresh_immediately=False,
    ) -> None:
        if middle_goes_home is not None:
            self.middle_goes_home = middle_goes_home
        self._live_hid = self._make_hid_status(
            ble=ble,
            firmware_middle_click=self._firmware_middle_click,
            peer_id=peer_id,
            pair_seq=pair_seq,
            bonded=bonded,
            pairing=pairing,
        )
        if refresh_immediately:
            self.board_status = {"hid": dict(self._live_hid, ble=dict(self._live_hid.get("ble", {})))}

    async def refresh_hid_status(self):
        self.board_status = {"hid": dict(self._live_hid, ble=dict(self._live_hid.get("ble", {})))}
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
    # Even if an earlier profile persisted this port, lacking firmware capability disables and clears it.
    (tmp_path / "calibration.assistive-touch.json").write_text(
        json.dumps(
            {
                "middle_click_home_devices": [mirror.port_name],
                "middle_click_home_bindings": {mirror.port_name: {"peer": 1, "pair_seq": 1}},
                "verified_route": "middle-click",
            }
        ),
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
        json.dumps(
            {
                "middle_click_home_bindings": {mirror.port_name: {"peer": 1, "pair_seq": 1}},
                "verified_route": "middle-click",
            }
        ),
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
    assert mirror.port_name not in (profile.get("middle_click_home_bindings") or {})

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


def test_same_esp32_same_port_repaired_to_another_iphone_invalidates_middle_click(tmp_path):
    """Re-pairing the same ESP32 on the same serial port invalidates old verification and skips middleclick."""
    port = "/dev/cu.usbserial-SAME-ESP32"
    mirror = MiddleHomeMirror(
        middle_goes_home=True,
        ble=True,
        firmware_middle_click=True,
        port_name=port,
        peer_id=1,
        pair_seq=1,
    )
    phone = make_phone(tmp_path, mirror)
    asyncio.run(phone.assistive_touch.verify_and_enable_middle_click_home())
    st1 = phone.assistive_touch.middle_click_home_status()
    assert st1["enabled"] is True
    assert st1["ble_peer"] == 1
    assert st1["ble_pair_seq"] == 1

    # Confirm iPhone A uses middleclick normally.
    mirror.show(frame())
    mirror.sent.clear()
    asyncio.run(phone.go_home())
    assert mirror.sent == ["middleclick"]

    # Case 1 (running worker / stale cached board_status):
    # Same ESP32 on same port unpairs iPhone A and pairs iPhone B (middle button unbound).
    # Even if board_status still holds iPhone A until go_home's refresh_hid_status(), go_home must
    # invalidate the binding and emit zero middleclick commands.
    mirror.set_live_ble_state(
        ble=True,
        peer_id=2,
        pair_seq=3,
        bonded=True,
        pairing=False,
        middle_goes_home=False,
        refresh_immediately=False,
    )
    mirror.show(frame())
    mirror.sent.clear()
    mirror.clicks.clear()
    asyncio.run(phone.go_home())
    assert "middleclick" not in mirror.sent
    assert mirror.sent.count("click") == 2

    st2 = phone.assistive_touch.middle_click_home_status()
    assert st2["verified_for_device"] is False
    assert st2["enabled"] is False
    assert st2["preferred_route"] == "assistive-touch"
    saved = json.loads((tmp_path / "calibration.assistive-touch.json").read_text(encoding="utf-8"))
    assert port not in (saved.get("middle_click_home_bindings") or {})

    # Re-verify iPhone B after binding its Button 3 -> Home, then test Case 2 (offline worker restart):
    mirror.middle_goes_home = True
    mirror.show(frame())
    asyncio.run(phone.assistive_touch.verify_and_enable_middle_click_home())
    assert phone.assistive_touch.middle_click_home_status()["enabled"] is True

    # Simulate worker restarting after same-port unpair+pair to iPhone C (or additional bond without unpair).
    mirror_c = MiddleHomeMirror(
        middle_goes_home=False,
        ble=True,
        firmware_middle_click=True,
        port_name=port,
        peer_id=3,
        pair_seq=4,
    )
    phone_c = make_phone(tmp_path, mirror_c)
    st3 = phone_c.assistive_touch.middle_click_home_status()
    assert st3["verified_for_device"] is False
    assert st3["enabled"] is False

    mirror_c.sent.clear()
    asyncio.run(phone_c.go_home())
    assert "middleclick" not in mirror_c.sent
    assert mirror_c.sent.count("click") == 2
    saved_after = json.loads((tmp_path / "calibration.assistive-touch.json").read_text(encoding="utf-8"))
    assert port not in (saved_after.get("middle_click_home_bindings") or {})
    assert saved_after.get("verified_route") != "middle-click"


def test_same_iphone_temporary_ble_disconnect_preserves_binding_until_reconnect(tmp_path):
    """Temporary BLE link loss pauses middle-click while disconnected; unpair or pair window erases it."""
    port = "/dev/cu.usbserial-RECONN"
    mirror = MiddleHomeMirror(
        middle_goes_home=True,
        ble=True,
        firmware_middle_click=True,
        port_name=port,
        peer_id=5,
        pair_seq=7,
    )
    phone = make_phone(tmp_path, mirror)
    asyncio.run(phone.assistive_touch.verify_and_enable_middle_click_home())
    assert phone.assistive_touch.middle_click_home_status()["enabled"] is True

    # Temporary disconnect of same bonded iPhone (conn=0, bond=1, pair=0, pair_seq=7).
    mirror.set_live_ble_state(
        ble=False, peer_id=0, pair_seq=7, bonded=True, pairing=False, refresh_immediately=True
    )
    disc = phone.assistive_touch.middle_click_home_status()
    assert disc["ble_active"] is False
    assert disc["verified_for_device"] is True
    assert disc["enabled"] is False

    # Same bonded iPhone reconnects cleanly.
    mirror.set_live_ble_state(
        ble=True, peer_id=5, pair_seq=7, bonded=True, pairing=False, refresh_immediately=True
    )
    reconn = phone.assistive_touch.middle_click_home_status()
    assert reconn["enabled"] is True

    # Opening pairing window (`pair=1`) invalidates existing verified middle-click binding immediately.
    mirror.set_live_ble_state(
        ble=True, peer_id=5, pair_seq=7, bonded=True, pairing=True, refresh_immediately=True
    )
    pairing_status = phone.assistive_touch.middle_click_home_status()
    assert pairing_status["verified_for_device"] is False
    assert pairing_status["enabled"] is False


def test_enable_confirms_late_settled_home_frame(tmp_path):
    """If Home finishes settling slightly past RESULT_WAIT (3.0s), enable confirms within LATE_HOME_WAIT."""
    mirror = MiddleHomeMirror(
        middle_goes_home=True,
        ble=True,
        firmware_middle_click=True,
        port_name="/dev/cu.usbserial-LATE",
        peer_id=2,
        pair_seq=2,
    )
    mirror.delay = 3.2
    phone = make_phone(tmp_path, mirror)

    asyncio.run(phone.assistive_touch.verify_and_enable_middle_click_home())

    status = phone.assistive_touch.middle_click_home_status()
    assert status["enabled"] is True
    assert mirror.sent == ["middleclick"]
