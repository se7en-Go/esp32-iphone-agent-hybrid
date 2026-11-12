"""蓝牙 HID 的状态、提示、校准节奏，以及串口熔断仍然挡住写手机。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from hidagent import app as module
from hidagent.device import Device, DeviceError
from hidagent.messages import explain_reply
from hidagent.phone import USB_PACE, Phone


def test_explain_reply_names_the_missing_link():
    both = explain_reply("move", "err phone not connected over usb/ble")
    assert "设置" in both and "蓝牙" in both and "ESP32 Agent" in both and "辅助触控" in both
    ble = explain_reply("move", "err phone not connected over ble")
    assert "设置" in ble and "ESP32 Agent" in ble and "既没有" not in ble
    usb = explain_reply("move", "err phone not connected over usb")
    assert "USB" in usb and "辅助触控" in usb
    asleep = explain_reply("move", "err phone usb asleep (unlock the phone)")
    assert "休眠" in asleep


def test_status_reports_hid_and_the_pairing_hint():
    saved_board = module.device.board_status
    saved_pace = module.device.pace_version
    module.device.pace_version = None
    try:
        module.device.board_status = {
            "hid": {"mode": "auto", "path": "none", "pace": 3,
                    "ble": {"adv": 1, "conn": 0, "bond": 0, "pair": 0}},
        }
        body = module.status()
        assert body["hid"]["path"] == "none"
        assert "ESP32 Agent" in body["hints"]["hid"]["zh"]
        assert "Settings" in body["hints"]["hid"]["en"]

        module.device.board_status = {"hid": "ble"}
        assert module.status()["hid"] == {"path": "ble"}
        assert module.status()["hints"]["hid"]["zh"] == ""

        module.device.board_status = {"hid": {"mode": "usb", "path": "none", "pace": 3, "ble": {}}}
        assert "USB" in module.status()["hints"]["hid"]["zh"]
    finally:
        module.device.board_status = saved_board
        module.device.pace_version = saved_pace


def test_legacy_usb_span_survives_pace_3(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps({"w": 1000, "h": 2000, "seen": {"x": [], "y": []}}))
    phone = Phone(SimpleNamespace(), path)
    phone.adopt_pace(USB_PACE)
    assert phone.span == {"w": 1000, "h": 2000}
    saved = json.loads(path.read_text())
    assert saved["pace"] == 3 and saved["w"] == 1000


def test_ble_pace_clears_the_span_and_the_same_pace_keeps_it(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps({"w": 1000, "h": 2000, "pace": 3, "seen": {"x": [[1, 2]], "y": []}}))
    phone = Phone(SimpleNamespace(), path)
    phone.adopt_pace(True)  # bool is an int subclass; it is not a pace number
    assert phone.span == {"w": 1000, "h": 2000}
    phone.adopt_pace(None)
    assert phone.calibrated
    phone.adopt_pace(4)
    assert phone.span is None and not phone.calibrated
    assert phone.origin == {"x": 0.0, "y": 0.0}
    saved = json.loads(path.read_text())
    assert "w" not in saved and saved["pace"] == 4
    assert saved["x0"] == 0 and saved["y0"] == 0
    phone.span = {"w": 800, "h": 1600}
    phone._save()
    phone.adopt_pace(4)
    assert phone.span == {"w": 800, "h": 1600}


def test_status_query_sets_the_pace_number():
    class Fake:
        board_status: dict = {}
        pace_version = None

        async def query(self, command):
            assert command == "status"
            return json.dumps({"hid": {"mode": "ble", "path": "ble", "pace": 4}})

    fake = Fake()
    asyncio.run(Device.refresh_hid_status(fake))
    assert fake.pace_version == 4
    assert fake.board_status["hid"]["path"] == "ble"


class _ScriptLink:
    def __init__(self):
        self.written: list[bytes] = []
        self.replies: list[bytes] = []
        self._open = True

    def is_open(self) -> bool:
        return self._open

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def close(self) -> None:
        self._open = False

    async def readline(self) -> bytes:
        return self.replies.pop(0)


def test_tripped_guard_blocks_exchange_before_the_serial_write(tmp_path):
    saved = (module.guard.tripped, module.guard.reason, list(module.guard.sends), module.guard._path)
    module.guard._path = tmp_path / "guard.json"
    module.guard.sends = []
    device = Device()
    link = _ScriptLink()
    device.attach(link)
    device.gate = module._hid_gate
    try:
        module.guard.tripped = True
        module.guard.reason = "hid test"

        async def blocked():
            with pytest.raises(DeviceError):
                await device.exchange("move 1 0")
            assert link.written == []
            link.replies.append(b"1 ok\n")
            assert await device.exchange("release") == "ok"
            link.replies.append(b"2 ok\n")
            assert await device.exchange("home") == "ok"

        asyncio.run(blocked())
        assert [w.split()[1] for w in link.written] == [b"release", b"home"]
    finally:
        device.close()
        module.guard.tripped, module.guard.reason, module.guard.sends, module.guard._path = saved
