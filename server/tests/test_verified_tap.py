"""Verified tap regressions using a real iOS 26 keypad and a delayed mirror."""

import asyncio
import io
import json
import math
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from hidagent import feedback
from hidagent import phone as phone_module
from hidagent.app import ControlAction
from hidagent.control import Controls
from hidagent.device import DeviceError
from hidagent.phone import Phone

FIXTURES = Path(__file__).parent / "fixtures" / "ios26"
KEYPAD = (FIXTURES / "keypad_no_disc.jpg").read_bytes()


def keypad_edit(*, digit=False, clock=False, pointer=False, highlight=False, pointer_at=(.5, .44)):
    image = Image.open(io.BytesIO(KEYPAD)).convert("RGB")
    draw = ImageDraw.Draw(image)
    if digit:
        draw.rectangle((304, 88, 310, 107), fill="black")
    if clock:
        draw.rectangle((20, 16, 70, 28), fill="white")
        draw.text((22, 17), "15:23", fill="black")
    if pointer:
        x, y = pointer_at[0] * image.width, pointer_at[1] * image.height
        draw.ellipse((x - 8, y - 8, x + 8, y + 8), fill=(150, 150, 150))
    if highlight:
        draw.rounded_rectangle((144, 309, 216, 377), radius=25, outline=(190, 190, 190), width=3)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


class LaggingMirror:
    """Only publish command effects after the simulated AirPlay delay."""

    live = True
    links = 0
    abort_epoch = 0

    def __init__(self, lag=0.0, effect=None, *, paced=False):
        self.lag, self.effect = lag, effect
        self.paced, self.firmware_time = paced, 0.0
        self.frame, self.updated = KEYPAD, time.time()
        if paced:
            self.frame_ts = (self.updated - lag) * 1000
        self.commands = []
        self.pending = []
        self.settled_calls = 0

    async def send(self, command):
        self.commands.append(command)
        if self.paced:
            parts = command.split()
            duration = 0.0
            if parts[0] == "move":
                speed = int(parts[3]) if len(parts) > 3 else 10
                duration = math.ceil(math.hypot(int(parts[1]), int(parts[2])) / speed) * .008
            elif command == "click":
                duration = .060
            elif command == "home":
                duration = 2 * 25 * .002
            self.firmware_time += duration
            await asyncio.sleep(duration)
        if command == "click" and self.effect:
            self.pending.append((time.monotonic() + self.lag, self.effect))

    async def fresh(self, since, timeout):
        deadline = time.monotonic() + timeout
        while True:
            while self.pending and self.pending[0][0] <= time.monotonic():
                _, self.frame = self.pending.pop(0)
                self.updated = time.time()
                if self.paced:
                    self.frame_ts = (self.updated - self.lag) * 1000
            if self.updated > since:
                return self.frame
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(min(0.005, max(0.0, deadline - time.monotonic())))

    async def settled(self, **kwargs):
        self.settled_calls += 1
        await asyncio.sleep(kwargs.get("least", 0))


def calibrated_phone(tmp_path, mirror):
    saved = tmp_path / "calibration.json"
    saved.write_text(json.dumps({"w": 678, "h": 1466, "x0": .1273, "y0": -.0006}))
    phone = Phone(mirror, saved, mirror)
    phone._pos = (0, 0)
    return phone


def test_missing_pointer_on_real_keypad_still_clicks_once(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)
    expected_move = f"move {phone._dx(.5)} {phone._dy(.44)}"

    async def no_disc(*args, **kwargs):
        return None

    phone._find_pointer = no_disc
    result = asyncio.run(phone.tap_verified(.5, .44))
    assert mirror.commands == [expected_move, "click"]
    assert result["clicked"] is True
    assert result["pointer_seen"] is False
    assert result["screen_changed"] is False
    assert result["warning"]
    assert phone.last_tap == result


@pytest.mark.parametrize("found", [(.95, .90), (-.1, .44), (float("nan"), .44), (float("inf"), .44)])
def test_implausible_pointer_never_moves_away_from_calibrated_target(tmp_path, found):
    mirror = LaggingMirror(effect=keypad_edit(digit=True))
    phone = calibrated_phone(tmp_path, mirror)
    expected_move = f"move {phone._dx(.5)} {phone._dy(.44)}"

    async def locate(*args, **kwargs):
        return found

    phone._find_pointer = locate
    result = asyncio.run(phone.tap_verified(.5, .44))
    assert mirror.commands == [expected_move, "click"]
    assert result["pointer_seen"] is False
    assert not phone._seen["x"] and not phone._seen["y"]


def test_plausible_pointer_gets_at_most_one_correction_and_is_learned(tmp_path):
    mirror = LaggingMirror(effect=keypad_edit(digit=True))
    phone = calibrated_phone(tmp_path, mirror)
    expected_move = f"move {phone._dx(.5)} {phone._dy(.44)}"
    attempts = []

    async def locate(*args, **kwargs):
        attempts.append(1)
        return .54, .42

    phone._find_pointer = locate
    result = asyncio.run(phone.tap_verified(.5, .44))
    assert len(attempts) == 1
    assert mirror.commands == [expected_move, "move -27 29", "click"]
    assert result["pointer_seen"] is True
    assert phone._seen["x"][-1] == (.5, .54)
    assert phone._seen["y"][-1] == (.44, .42)


@pytest.mark.parametrize("lag", [.2, 1.7])
@pytest.mark.parametrize("from_origin", [True, False])
def test_control_tap_budget_includes_lag_and_does_not_wait_twice(tmp_path, lag, from_origin):
    mirror = LaggingMirror(lag, keypad_edit(digit=True), paced=True)
    phone = calibrated_phone(tmp_path, mirror)
    phone._clock, phone._clock_link = 0.0, 0
    if not from_origin:
        phone._pos = (phone._dx(.5), phone._dy(.55))
    control = Controls(phone, mirror)
    request = ControlAction(action="tap", screen_id=control.observation()["screen_id"], x=.5, y=.44)
    started = time.monotonic()
    result = asyncio.run(control.perform(request))
    elapsed = time.monotonic() - started
    assert mirror.commands.count("click") == 1
    assert "move 2 0" in mirror.commands and "move -2 0" in mirror.commands
    assert mirror.settled_calls == 0
    assert result["clicked"] is True
    assert result["pointer_seen"] is False
    assert result["screen_changed"] is True
    expected_firmware = .636 if from_origin else .212
    assert mirror.firmware_time == pytest.approx(expected_firmware)
    assert lag + mirror.firmware_time <= result["latency"] <= elapsed + .001
    assert elapsed <= lag + mirror.firmware_time + .15
    assert result["timing_ms"]["total"] <= (2500 if from_origin else 2100)


def test_clock_and_pointer_only_updates_report_no_effect_without_retry(tmp_path):
    mirror = LaggingMirror(.2, keypad_edit(clock=True, pointer=True))
    phone = calibrated_phone(tmp_path, mirror)
    result = asyncio.run(phone.tap_verified(.5, .44))
    assert mirror.commands.count("click") == 1
    assert result["clicked"] is True
    assert result["screen_changed"] is False
    # no board clock here: a no-effect tap waits out the mirror-lag bound plus the effect window
    assert result["latency"] <= phone_module.TAP_EFFECT_WAIT + feedback.MIRROR_LAG_SECONDS + .1


def test_target_highlight_without_content_change_is_not_a_tap_effect(tmp_path):
    mirror = LaggingMirror(.2, keypad_edit(highlight=True))
    phone = calibrated_phone(tmp_path, mirror)
    result = asyncio.run(phone.tap_verified(.5, .44))
    assert mirror.commands.count("click") == 1
    assert result["pointer_seen"] is False
    assert result["screen_changed"] is False


def test_unknown_old_pointer_disappearing_is_not_a_tap_effect(tmp_path):
    mirror = LaggingMirror(.2, KEYPAD)
    mirror.frame = keypad_edit(pointer=True, pointer_at=(.5, .28))
    phone = calibrated_phone(tmp_path, mirror)
    phone._pos = None
    result = asyncio.run(phone.tap_verified(.5, .44))
    assert mirror.commands[0] == "home"
    assert mirror.commands.count("click") == 1
    assert result["screen_changed"] is False


def test_previous_button_highlight_fading_is_not_the_next_taps_effect(tmp_path):
    mirror = LaggingMirror(.2, KEYPAD)
    mirror.frame = keypad_edit(highlight=True)
    phone = calibrated_phone(tmp_path, mirror)
    phone._pointer_spots = ((.5, .44),)
    result = asyncio.run(phone.tap_verified(.5, .7))
    assert mirror.commands.count("click") == 1
    assert result["screen_changed"] is False


def test_cold_controller_old_highlight_disappearing_is_inconclusive(tmp_path):
    mirror = LaggingMirror(.2, KEYPAD)
    mirror.frame = keypad_edit(highlight=True)
    phone = calibrated_phone(tmp_path, mirror)
    phone._pos = None
    assert not phone._pointer_spots
    result = asyncio.run(phone.tap_verified(.5, .7))
    assert mirror.commands[0] == "home"
    assert mirror.commands.count("click") == 1
    assert result["screen_changed"] is False
    assert result["warning"]


def disc_frame(x):
    image = Image.new("RGB", (498, 1080), (40, 80, 120))
    ImageDraw.Draw(image).ellipse((x - 12, 488, x + 12, 512), fill=(150, 150, 150))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


@pytest.mark.parametrize("timestamps", ["old", "mixed", "unknown", "after_return"])
def test_queued_disc_pair_never_corrects_or_teaches_a_current_landing(tmp_path, timestamps):
    class QueuedMirror(LaggingMirror):
        def __init__(self):
            super().__init__(effect=disc_frame(249))
            self.frame = disc_frame(270)
            self.old_ts = (time.time() - .05) * 1000
            self.frame_ts = None if timestamps == "unknown" else self.old_ts

        async def send(self, command):
            await super().send(command)
            if command == "move -2 0":
                self.pending = [(time.monotonic() + .01, disc_frame(272))]
                if timestamps == "after_return":
                    self.pending.insert(0, (time.monotonic(), disc_frame(270)))

        async def fresh(self, since, timeout):
            frame = await super().fresh(since, timeout)
            if frame:
                if timestamps == "old":
                    self.frame_ts = self.old_ts + 10
                elif timestamps in {"mixed", "after_return"}:
                    self.frame_ts = self.updated * 1000
            return frame

    mirror = QueuedMirror()
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone.origin = {"w": 498, "h": 1080}, {"x": 0, "y": 0}
    phone._clock, phone._clock_link = 0.0, 0
    original_calibration = phone._file.read_bytes()
    result = asyncio.run(phone.tap_verified(.5, 500 / 1080))
    assert mirror.commands == ["move 249 500", "move 2 0", "move -2 0", "click"]
    assert result["clicked"] and not result["pointer_seen"]
    assert not phone._seen["x"] and not phone._seen["y"]
    assert phone._file.read_bytes() == original_calibration


def test_queued_real_correction_frame_is_not_a_tap_effect(tmp_path):
    mirror = LaggingMirror(.2, (FIXTURES / "live_tap_corrected.jpg").read_bytes())
    mirror.frame = (FIXTURES / "live_tap_landed.jpg").read_bytes()
    phone = calibrated_phone(tmp_path, mirror)

    async def landed(*args, **kwargs):
        return 570.1 / 720, 663.7 / 1560

    phone._find_pointer = landed
    result = asyncio.run(phone.tap_verified(.75, .40))
    assert mirror.commands.count("click") == 1
    assert result["pointer_seen"] is True
    assert result["screen_changed"] is False


def test_quick_finder_detects_real_disc_motion_after_tiny_wake(tmp_path):
    class TinyNudgeMirror(LaggingMirror):
        frame_ts = 0

        async def send(self, command):
            self.commands.append(command)
            await asyncio.sleep(.004)
            if command == "move 2 0":
                self.pending.append((time.monotonic() + .01, disc_frame(252), time.time() * 1000))
            elif command == "move -2 0":
                self.pending.append((time.monotonic() + .06, disc_frame(250), time.time() * 1000))

        async def fresh(self, since, timeout):
            await asyncio.sleep(.01)
            if self.pending and self.pending[0][0] <= time.monotonic():
                _, self.frame, self.frame_ts = self.pending.pop(0)
                self.updated = time.time()
                return self.frame
            return None

    mirror = TinyNudgeMirror()
    mirror.frame, mirror.frame_ts = disc_frame(250), time.time() * 1000
    phone = calibrated_phone(tmp_path, mirror)
    phone.span = {"w": 498, "h": 1080}
    phone._clock, phone._clock_link = 0.0, 0
    result = asyncio.run(phone._find_pointer((250 / 498, 500 / 1080), quick=True))
    assert result == pytest.approx((250 / 498, 500 / 1080), abs=.002)
    assert mirror.commands == ["move 2 0", "move -2 0"]
    assert mirror.settled_calls == 0


def test_stop_during_pointer_check_prevents_click(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)

    async def stopped(*args, **kwargs):
        mirror.abort_epoch += 1
        return None

    phone._find_pointer = stopped
    with pytest.raises(DeviceError, match="紧急停止"):
        asyncio.run(phone.tap_verified(.5, .44))
    assert "click" not in mirror.commands


@pytest.mark.parametrize("failed_command", ["move 2 0", "move -2 0"])
def test_failed_wake_forgets_position_and_never_clicks(tmp_path, failed_command):
    class FailedWake(LaggingMirror):
        async def send(self, command):
            self.commands.append(command)
            if command == failed_command:
                raise DeviceError("lost wake ACK")

    mirror = FailedWake()
    phone = calibrated_phone(tmp_path, mirror)
    with pytest.raises(DeviceError, match="lost wake ACK"):
        asyncio.run(phone.tap_verified(.5, .44))
    assert phone._pos is None
    assert "click" not in mirror.commands


def test_failed_correction_forgets_position_and_never_clicks(tmp_path):
    class FailedCorrection(LaggingMirror):
        async def send(self, command):
            self.commands.append(command)
            if command == "move -27 29":
                raise DeviceError("lost correction ACK")

    mirror = FailedCorrection()
    phone = calibrated_phone(tmp_path, mirror)

    async def locate(*args, **kwargs):
        return .54, .42

    phone._find_pointer = locate
    with pytest.raises(DeviceError, match="lost correction ACK"):
        asyncio.run(phone.tap_verified(.5, .44))
    assert phone._pos is None
    assert "click" not in mirror.commands


def test_board_clock_cache_is_cleared_when_link_changes(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)
    phone._clock, phone._clock_link = 123.0, 0
    mirror.links = 1
    asyncio.run(phone._find_pointer((.5, .44), quick=True))
    assert phone._clock is None


@pytest.mark.parametrize("after_click", ["none", "same", "new"])
def test_late_pre_click_capture_does_not_count_as_click_effect(tmp_path, after_click):
    old_update = keypad_edit(digit=True)
    image = Image.open(io.BytesIO(old_update)).convert("RGB")
    ImageDraw.Draw(image).rectangle((320, 88, 326, 107), fill="black")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    new_update = buffer.getvalue()

    class TimestampMirror(LaggingMirror):
        def __init__(self):
            super().__init__()
            self.frame_ts = time.time() * 1000

        async def query(self, command):
            raise AssertionError("the board clock is already cached")

        async def send(self, command):
            self.commands.append(command)
            if command == "click":
                at, board_ms = time.monotonic(), time.time() * 1000
                self.pending.append((at + .01, old_update, board_ms - 100))
                if after_click != "none":
                    frame = new_update if after_click == "new" else old_update
                    self.pending.append((at + .03, frame, board_ms + 20))

        async def fresh(self, since, timeout):
            deadline = time.monotonic() + timeout
            while True:
                while self.pending and self.pending[0][0] <= time.monotonic():
                    _, self.frame, self.frame_ts = self.pending.pop(0)
                    self.updated = time.time()
                if self.updated > since:
                    return self.frame
                if time.monotonic() >= deadline:
                    return None
                await asyncio.sleep(.005)

    mirror = TimestampMirror()
    phone = calibrated_phone(tmp_path, mirror)
    phone._clock, phone._clock_link = 0.0, 0
    result = asyncio.run(phone.tap_verified(.5, .44))
    assert mirror.commands.count("click") == 1
    assert result["fresh"] is True
    assert result["screen_changed"] is (after_click == "new")


def test_replaying_mirror_refuses_verified_click(tmp_path):
    mirror = LaggingMirror()
    mirror.frame_ts = (time.time() - 10) * 1000
    phone = calibrated_phone(tmp_path, mirror)
    phone._clock, phone._clock_link = 0.0, 0
    with pytest.raises(DeviceError, match="重放"):
        asyncio.run(phone.tap_verified(.5, .44))
    assert "click" not in mirror.commands


def test_observed_keypad_token_survives_clock_and_pointer_changes(tmp_path):
    mirror = LaggingMirror()
    control = Controls(calibrated_phone(tmp_path, mirror), mirror)
    token = control.observation()["screen_id"]
    mirror.frame = keypad_edit(clock=True, pointer=True)
    _, width, height = control.consume(token)
    assert (width, height) == (360, 780)
    with pytest.raises(ValueError, match="consumed"):
        control.consume(token)


def test_observed_keypad_token_refuses_a_different_real_screen(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)
    control = Controls(phone, mirror)
    token = control.observation()["screen_id"]
    image = Image.open(FIXTURES / "01_m150_2000ms.jpg").resize((360, 780))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    mirror.frame = buffer.getvalue()
    with pytest.raises(ValueError, match="screen changed"):
        asyncio.run(control.perform(ControlAction(action="tap", screen_id=token, x=.5, y=.44)))
    assert not mirror.commands
