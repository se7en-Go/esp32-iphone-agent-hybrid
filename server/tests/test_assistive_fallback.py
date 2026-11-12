import asyncio
import io
import json
import time

import pytest
from home_scene import draw_home
from PIL import Image, ImageDraw

from hidagent import assistive
from hidagent.device import DeviceError
from hidagent.phone import Phone


def app_frame(*, home=False, clock=False, pointer=False):
    image = Image.new("RGB", (360, 780), "#a6c6dc" if home else "#e5dbc5")
    draw = ImageDraw.Draw(image)
    if home:
        draw_home(image)
    draw.text((15, 12), "12:01" if clock else "12:00", fill="black")
    if pointer:
        draw.ellipse((172, 460, 188, 476), fill="#969696")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


class Screen:
    live = True

    def __init__(self):
        self.frame = app_frame()
        self.updated = time.time()

    def show(self, frame):
        self.frame, self.updated = frame, time.time()

    async def fresh(self, since, timeout=0.8):
        if self.updated > since:
            return self.frame
        await asyncio.sleep(min(timeout, .005))
        return None

    async def settled(self, **kwargs):
        pass


class Device:
    links = 0

    def __init__(self, on_send=None):
        self.sent = []
        self.abort_epoch = 0
        self.on_send = on_send

    async def send(self, command):
        self.sent.append(command)
        if self.on_send:
            self.on_send(command)


@pytest.fixture(autouse=True)
def short_waits(monkeypatch):
    monkeypatch.setattr("hidagent.freshness.MIRROR_LAG_SECONDS", 0)
    monkeypatch.setattr(assistive, "MENU_WAIT", .04)
    monkeypatch.setattr(assistive, "RESULT_WAIT", .3)
    monkeypatch.setattr(assistive, "STABLE_QUIET", .02)
    monkeypatch.setattr("hidagent.phone.TAP_GAP", 0)


def make_phone(tmp_path, device, screen=None, *, calibrated=True):
    phone = Phone(device, tmp_path / "calibration.json", screen)
    if calibrated:
        phone.span = {"w": 360, "h": 780}
    return phone


def test_without_screen_refuses_all_recovery_commands(tmp_path):
    device = Device()
    phone = make_phone(tmp_path, device)

    with pytest.raises(RuntimeError, match="新鲜|重放"):
        asyncio.run(phone.go_home())

    assert device.sent == []


def test_without_calibration_keyboard_fallback_still_runs(tmp_path):
    device = Device()
    phone = make_phone(tmp_path, device, Screen(), calibrated=False)

    with pytest.raises(RuntimeError, match="未确认主屏幕"):
        asyncio.run(phone.go_home())

    assert device.sent == ["homescreen", "key cmd+h"]


def test_unsupported_consumer_home_continues_to_keyboard(tmp_path):
    def reject_consumer(command):
        if command == "homescreen":
            raise DeviceError("err unknown command")

    device = Device(reject_consumer)
    phone = make_phone(tmp_path, device, Screen())

    with pytest.raises(RuntimeError, match="未确认主屏幕"):
        asyncio.run(phone.go_home())

    assert device.sent[-2:] == ["homescreen", "key cmd+h"]


@pytest.mark.parametrize("successful_command", ["release", "homescreen", "key cmd+h"])
def test_live_fallback_stops_after_home_signature(tmp_path, successful_command):
    screen = Screen()

    def change_screen(command):
        if command == successful_command:
            asyncio.get_running_loop().call_soon(screen.show, app_frame(home=True))

    device = Device(change_screen)
    phone = make_phone(tmp_path, device, screen)

    asyncio.run(phone.go_home())

    assert device.sent[-1] == successful_command
    assert "click" not in device.sent


@pytest.mark.parametrize("incidental_change", [False, True])
def test_live_unchanged_or_clock_pointer_only_is_not_success(tmp_path, incidental_change):
    screen = Screen()

    def change_screen(command):
        if command in {"release", "homescreen", "key cmd+h"}:
            screen.show(app_frame(clock=incidental_change, pointer=incidental_change))

    device = Device(change_screen)
    phone = make_phone(tmp_path, device, screen)

    with pytest.raises(RuntimeError):
        asyncio.run(phone.go_home())

    assert device.sent[-2:] == ["homescreen", "key cmd+h"]


def test_abort_after_swipe_prevents_remaining_fallbacks(tmp_path):
    device = Device()

    def stop_after_swipe(command):
        if command == "release":
            device.abort_epoch += 1

    device.on_send = stop_after_swipe
    phone = make_phone(tmp_path, device, Screen())

    with pytest.raises(DeviceError):
        asyncio.run(phone.go_home())

    assert "homescreen" not in device.sent
    assert "key cmd+h" not in device.sent


def test_abort_error_is_not_swallowed_as_unsupported_consumer(tmp_path):
    device = Device()

    def stop_on_consumer(command):
        if command == "homescreen":
            device.abort_epoch += 1
            raise DeviceError("已紧急停止")

    device.on_send = stop_on_consumer
    phone = make_phone(tmp_path, device, Screen(), calibrated=False)

    with pytest.raises(DeviceError):
        asyncio.run(phone.go_home())

    assert device.sent == ["homescreen"]


def test_cancellation_does_not_trigger_another_fallback(tmp_path):
    def cancel_consumer(command):
        if command == "homescreen":
            raise asyncio.CancelledError

    device = Device(cancel_consumer)
    phone = make_phone(tmp_path, device, Screen(), calibrated=False)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(phone.go_home())

    assert device.sent == ["homescreen"]


def test_known_assistive_route_is_not_followed_by_hid_fallbacks(tmp_path):
    device = Device()
    screen = Screen()
    phone = make_phone(tmp_path, device, screen)
    phone.assistive_touch._saved["verified_route"] = "assistive-touch"
    phone.assistive_touch._write_profile()
    reloaded = make_phone(tmp_path, Device(), Screen())
    assert reloaded.assistive_touch._saved["verified_route"] == "assistive-touch"

    for _ in range(2):
        with pytest.raises(RuntimeError, match="辅助触控"):
            asyncio.run(phone.go_home())
    assert device.sent == []

    phone.assistive_touch.reset_routes()
    assert "verified_route" not in json.loads(phone.assistive_touch.path.read_text())
    with pytest.raises(RuntimeError, match="未确认"):
        asyncio.run(phone.go_home())
    assert "homescreen" in device.sent and "key cmd+h" in device.sent
