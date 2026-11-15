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
from hidagent.text_target import PointerLandingError, TargetChangedError


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

    async def measured_pointer(near, **kwargs):
        # These tests isolate route selection, ACKs and Home recognition; cursor
        # detection is covered separately with real/synthetic image fixtures.
        return near

    phone._find_pointer = measured_pointer
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


@pytest.mark.parametrize("error", [PointerLandingError, TargetChangedError])
@pytest.mark.parametrize("successful_command", ["homescreen", "key cmd+h"])
def test_confirmed_no_pointer_click_can_use_consumer_home(tmp_path, monkeypatch, error, successful_command):
    screen = Screen()

    def change_screen(command):
        if command == successful_command:
            asyncio.get_running_loop().call_soon(screen.show, app_frame(home=True))

    device = Device(change_screen)
    phone = make_phone(tmp_path, device, screen)

    async def rejected(*args):
        raise error("pointer target could not be verified before sending")

    monkeypatch.setattr(phone.assistive_touch, "_assistive_home", rejected)
    asyncio.run(phone.go_home())
    assert device.sent == (["homescreen"] if successful_command == "homescreen" else ["homescreen", "key cmd+h"])


@pytest.mark.parametrize("error", [PointerLandingError, TargetChangedError])
@pytest.mark.parametrize("route", ["assistive-touch", "swipe-up"])
@pytest.mark.parametrize("incidental_change,later_error", [(False, False), (True, False), (False, True)])
def test_ack_without_home_preserves_prior_pointer_rejection(
        tmp_path, monkeypatch, caplog, error, route, incidental_change, later_error):
    screen = Screen()
    rejection = error("observed Home target: measured pointer remains outside its bounds")

    def change_screen(command):
        screen.show(app_frame(clock=incidental_change, pointer=incidental_change))
        if command == "key cmd+h" and later_error:
            # A later transport failure must not erase the earlier visual reason
            # after the consumer Home command was acknowledged without effect.
            raise DeviceError("keyboard shortcut unavailable")

    device = Device(change_screen)
    phone = make_phone(tmp_path, device, screen)

    async def rejected(*args, **kwargs):
        raise rejection

    if route == "assistive-touch":
        monkeypatch.setattr(phone.assistive_touch, "_assistive_home", rejected)
    else:
        monkeypatch.setattr(phone, "swipe", rejected)

    with pytest.raises(RuntimeError, match="未确认主屏幕") as caught:
        asyncio.run(phone.go_home())

    assert str(rejection) in str(caught.value)
    assert caught.value.__cause__ is rejection
    assert str(rejection) in caplog.text
    assert device.sent == ["homescreen", "key cmd+h"]
    assert not assistive.assistive_vision.is_home_screen(screen.frame)
    assert "verified_route" not in phone.assistive_touch._saved


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


@pytest.mark.parametrize("error", [None, PointerLandingError, TargetChangedError])
def test_known_assistive_failure_uses_visually_verified_fallback(tmp_path, monkeypatch, error):
    screen = Screen()

    def change_screen(command):
        if command == "homescreen":
            asyncio.get_running_loop().call_soon(screen.show, app_frame(home=True))

    device = Device(change_screen)
    phone = make_phone(tmp_path, device, screen, calibrated=False)
    phone.assistive_touch._saved["verified_route"] = "assistive-touch"
    phone.assistive_touch._write_profile()
    attempts = []

    async def unavailable(epoch):
        attempts.append("assistive-touch")
        if error is not None:
            raise error("pointer target could not be verified before sending")
        return False

    monkeypatch.setattr(phone.assistive_touch, "_assistive_home", unavailable)
    asyncio.run(phone.go_home())
    assert attempts == ["assistive-touch"]
    assert device.sent == ["homescreen"]
    assert json.loads(phone.assistive_touch.path.read_text())["verified_route"] == "homescreen"


def test_saved_keyboard_route_runs_before_pointer_routes(tmp_path, monkeypatch):
    screen = Screen()

    def change_screen(command):
        if command == "key cmd+h":
            asyncio.get_running_loop().call_soon(screen.show, app_frame(home=True))

    device = Device(change_screen)
    phone = make_phone(tmp_path, device, screen)
    phone.assistive_touch._saved["verified_route"] = "cmd+h"
    phone.assistive_touch._write_profile()
    phone = make_phone(tmp_path, device, screen)

    async def unnecessary_pointer_route(epoch):
        pytest.fail("saved keyboard route must run before a pointer route")

    monkeypatch.setattr(phone.assistive_touch, "_assistive_home", unnecessary_pointer_route)
    asyncio.run(phone.go_home())
    assert device.sent == ["key cmd+h"]


def test_saved_route_ack_without_home_does_not_count_as_success(tmp_path):
    device = Device()
    phone = make_phone(tmp_path, device, Screen(), calibrated=False)
    phone.assistive_touch._saved["verified_route"] = "homescreen"
    with pytest.raises(RuntimeError, match="未确认主屏幕"):
        asyncio.run(phone.go_home())
    assert device.sent == ["homescreen", "key cmd+h"]


def test_already_home_is_idempotent_without_sending_hid(tmp_path, monkeypatch):
    screen = Screen()
    screen.show(app_frame(home=True))
    device = Device()
    phone = make_phone(tmp_path, device, screen)

    async def unnecessary_pointer_route(epoch):
        pytest.fail("already on Home; should not touch the AssistiveTouch menu")

    monkeypatch.setattr(phone.assistive_touch, "_assistive_home", unnecessary_pointer_route)
    asyncio.run(phone.go_home())
    assert device.sent == []


def test_reset_routes_forgets_saved_preference(tmp_path):
    phone = make_phone(tmp_path, Device(), Screen())
    phone.assistive_touch._set_verified_route("homescreen")
    phone.assistive_touch.reset_routes()
    assert "verified_route" not in json.loads(phone.assistive_touch.path.read_text())


def test_already_open_menu_uses_visible_home_icon_before_saved_key_route(tmp_path, monkeypatch):
    from test_assistive_vision import panel_frame

    screen = Screen()
    screen.show(panel_frame(home_y=.74))
    device = Device()
    phone = make_phone(tmp_path, device, screen)
    phone.assistive_touch._saved["verified_route"] = "cmd+h"
    taps = []

    async def tap(x, y, *, target_box=None, target_frame=None, before_click=None, navigation_context=None):
        before_click()
        navigation_context.validate_target(target_frame, getattr(screen, "generation", 0), (x, y))
        taps.append((x, y, target_box, target_frame))
        assert target_box[0] < x < target_box[2] and target_box[1] < y < target_box[3]
        since = time.time()
        asyncio.get_running_loop().call_soon(screen.show, app_frame(home=True))
        return since

    monkeypatch.setattr(phone, "tap", tap)
    asyncio.run(phone.go_home())
    assert len(taps) == 1 and not device.sent
    assert phone.assistive_touch._saved["verified_route"] == "assistive-touch"
