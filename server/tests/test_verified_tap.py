"""Verified tap regressions using a real iOS 26 keypad and a delayed mirror."""

import asyncio
import io
import json
import math
import time
from pathlib import Path

import pytest
from fake_clock import ManualClock
from PIL import Image, ImageDraw

from hidagent import feedback
from hidagent import phone as phone_module
from hidagent.app import ControlAction
from hidagent.control import Controls
from hidagent.device import DeviceError
from hidagent.phone import Phone

FIXTURES = Path(__file__).parent / "fixtures" / "ios26"
KEYPAD = (FIXTURES / "keypad_no_disc.jpg").read_bytes()


@pytest.fixture(autouse=True)
def paced_clock(monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)
    return clock


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
            self.frame_ts = self.updated * 1000
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
                received_at, self.frame = self.pending.pop(0)
                self.updated = time.time()
                if self.paced:
                    # Board receipt, even if the host polls this queue later.
                    self.frame_ts = (self.updated - (time.monotonic() - received_at)) * 1000
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


async def observed_tap(phone, x, y, **kwargs):
    # Steering: an unseen pointer now requires measured landings and an observed target's bounds.
    for axis in ("x", "y"):
        if not phone._seen[axis]:
            phone._seen[axis].extend([(.1, .1), (.5, .5), (.9, .9)])
    return await phone.tap_verified(x, y, target_box=(x - .12, y - .12, x + .12, y + .12),
                                    target_frame=phone.screen.frame, **kwargs)


def test_missing_pointer_on_real_keypad_still_clicks_once(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)
    expected_move = f"move {phone._dx(.5)} {phone._dy(.44)}"

    async def no_disc(*args, **kwargs):
        return None

    phone._find_pointer = no_disc
    result = asyncio.run(observed_tap(phone, .5, .44))
    assert mirror.commands == [expected_move, "click"]
    assert result["clicked"] is True
    assert result["pointer_seen"] is False
    assert result["screen_changed"] is False
    assert result["warning"]
    assert phone.last_tap == result


def test_box_from_model_never_authorizes_an_estimated_click(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)

    async def no_disc(*args, **kwargs):
        return None

    phone._find_pointer = no_disc
    with pytest.raises(RuntimeError, match="没有观察到"):
        asyncio.run(observed_tap(phone, .5, .44, allow_estimated_landing=False))
    assert "click" not in mirror.commands


@pytest.mark.parametrize("found", [(.95, .90), (-.1, .44), (float("nan"), .44), (float("inf"), .44)])
def test_implausible_pointer_never_moves_away_from_calibrated_target(tmp_path, found):
    mirror = LaggingMirror(effect=keypad_edit(digit=True))
    phone = calibrated_phone(tmp_path, mirror)
    expected_move = f"move {phone._dx(.5)} {phone._dy(.44)}"

    async def locate(*args, **kwargs):
        return found

    phone._find_pointer = locate
    result = asyncio.run(observed_tap(phone, .5, .44))
    assert mirror.commands == [expected_move, "click"]
    assert result["pointer_seen"] is False
    assert list(phone._seen["x"]) == list(phone._seen["y"]) == [(.1, .1), (.5, .5), (.9, .9)]


def test_plausible_pointer_gets_at_most_one_correction_and_is_learned(tmp_path):
    mirror = LaggingMirror(effect=keypad_edit(digit=True))
    phone = calibrated_phone(tmp_path, mirror)
    expected_move = f"move {phone._dx(.5)} {phone._dy(.44)}"
    attempts = []

    async def locate(*args, **kwargs):
        attempts.append(1)
        return .54, .42

    phone._find_pointer = locate
    result = asyncio.run(observed_tap(phone, .5, .44))
    assert len(attempts) == 1
    assert mirror.commands == [expected_move, "move -27 29", "click"]
    assert result["pointer_seen"] is True
    assert phone._seen["x"][-1] == (.5, .54)
    assert phone._seen["y"][-1] == (.44, .42)


@pytest.mark.parametrize("verified_after_correction", [False, True])
def test_paired_ble_pointer_miss_is_corrected_before_click_without_learning(
        tmp_path, verified_after_correction):
    mirror = LaggingMirror(effect=keypad_edit(digit=True))
    mirror.pace_version = 4
    phone = calibrated_phone(tmp_path, mirror)
    phone._pace = 4
    expected_move = f"move {phone._dx(.5)} {phone._dy(.44)}"

    async def locate(*args, **kwargs):
        phone._pointer_pair_verified = True
        return .52, .513

    async def observe(*args):
        return (.5, .44) if verified_after_correction else None

    async def no_pair(*args, **kwargs):
        return None

    phone._find_pointer = locate
    phone._observe_landing = observe
    phone._localize_pointer = no_pair
    if verified_after_correction:
        result = asyncio.run(phone.tap_verified(.5, .44))
        assert result["clicked"] and result["pointer_seen"]
        assert mirror.commands == [expected_move, "move -14 -107", "click"]
    else:
        with pytest.raises(RuntimeError, match="落点"):
            asyncio.run(phone.tap_verified(.5, .44))
        assert mirror.commands == [expected_move, "move -14 -107"]
    assert not phone._seen["x"] and not phone._seen["y"]


def test_paired_ble_verifies_two_corrections_before_click(tmp_path):
    mirror = LaggingMirror(effect=keypad_edit(digit=True))
    mirror.pace_version = 4
    phone = calibrated_phone(tmp_path, mirror)
    phone._pace = 4

    async def locate(*args, **kwargs):
        phone._pointer_pair_verified = True
        return .52, .55

    landings = iter([(.501, .465), (.5, .44)])

    async def observe(*args):
        return next(landings)

    phone._find_pointer = locate
    phone._observe_landing = observe
    result = asyncio.run(phone.tap_verified(.5, .44))

    assert result["clicked"] and result["pointer_seen"]
    assert mirror.commands[-3:] == ["move -14 -161", "move -1 -37", "click"]


def test_ble_relocalizes_after_passive_landing_loses_the_corrected_pointer(tmp_path):
    mirror = LaggingMirror(effect=keypad_edit(digit=True))
    mirror.pace_version = 4
    phone = calibrated_phone(tmp_path, mirror)
    phone._pace = 4

    async def initial_landing(*args, **kwargs):
        return .55, .46

    arrivals = iter([None, (.5, .44)])

    async def observe(*args):
        return next(arrivals)

    async def localize(*args, **kwargs):
        phone._pointer_pair_verified = True
        return .516, .449

    phone._find_pointer = initial_landing
    phone._observe_landing = observe
    phone._localize_pointer = localize
    result = asyncio.run(phone.tap_verified(.5, .44))

    assert result["clicked"] and result["pointer_seen"]
    assert mirror.commands[-3:] == ["move -34 -29", "move -11 -13", "click"]


@pytest.mark.parametrize("lag", [.2, 1.72])
@pytest.mark.parametrize("from_origin", [True, False])
def test_tap_budget_includes_firmware_lag_and_phase_measurements(tmp_path, lag, from_origin):
    mirror = LaggingMirror(lag, keypad_edit(digit=True), paced=True)
    phone = calibrated_phone(tmp_path, mirror)
    phone._clock, phone._clock_link = 0.0, 0
    if not from_origin:
        phone._pos = (phone._dx(.5), phone._dy(.55))
    started = time.monotonic()
    result = asyncio.run(observed_tap(phone, .5, .44))
    elapsed = time.monotonic() - started
    assert mirror.commands.count("click") == 1
    assert "move 2 0" not in mirror.commands and "move -2 0" not in mirror.commands
    assert mirror.settled_calls == 0
    assert result["clicked"] is True
    assert result["pointer_seen"] is False
    eligible = lag >= phone_module.freshness.MIRROR_LAG_SECONDS
    assert result["screen_changed"] is eligible
    assert result["fresh"] is eligible
    assert result["effect_observed"] is (True if eligible else None)
    expected_firmware = .620 if from_origin else .196
    assert mirror.firmware_time == pytest.approx(expected_firmware)
    assert lag + mirror.firmware_time <= result["latency"] <= elapsed + .001
    frame_wait = lag if eligible else phone_module.TAP_EFFECT_WAIT + phone_module.freshness.MIRROR_LAG_SECONDS
    assert elapsed <= frame_wait + mirror.firmware_time + .4
    timing = result["timing_ms"]
    assert timing["firmware"] >= expected_firmware * 1000
    assert timing["wait_for_frames"] >= frame_wait * 1000 - 10
    assert timing["positioning"] >= 0 and timing["diff"] >= 0
    assert abs(sum(timing[key] for key in ("positioning", "firmware", "wait_for_frames", "diff"))
               - timing["total"]) < 10
    print(f"tap from_origin={from_origin} lag={lag}: {timing}")


def test_clock_and_pointer_only_updates_report_no_effect_without_retry(tmp_path):
    mirror = LaggingMirror(.2, keypad_edit(clock=True, pointer=True))
    phone = calibrated_phone(tmp_path, mirror)
    result = asyncio.run(observed_tap(phone, .5, .44))
    assert mirror.commands.count("click") == 1
    assert result["clicked"] is True
    assert result["screen_changed"] is False
    # Bound the evidence wait separately from the measured positioning/guard work.
    budget = phone_module.TAP_EFFECT_WAIT + feedback.MIRROR_LAG_SECONDS
    timing = result["timing_ms"]
    assert timing["wait_for_frames"] <= (budget + .1) * 1000
    host_and_firmware = sum(timing[key] for key in ("positioning", "firmware", "diff")) / 1000
    assert result["latency"] <= budget + host_and_firmware + .1


def test_target_highlight_without_content_change_is_not_a_tap_effect(tmp_path):
    mirror = LaggingMirror(.2, keypad_edit(highlight=True))
    phone = calibrated_phone(tmp_path, mirror)
    result = asyncio.run(observed_tap(phone, .5, .44))
    assert mirror.commands.count("click") == 1
    assert result["pointer_seen"] is False
    assert result["screen_changed"] is False


def test_unknown_old_pointer_disappearing_is_not_a_tap_effect(tmp_path):
    mirror = LaggingMirror(.2, KEYPAD)
    mirror.frame = keypad_edit(pointer=True, pointer_at=(.5, .28))
    phone = calibrated_phone(tmp_path, mirror)
    phone._pos = None
    result = asyncio.run(observed_tap(phone, .5, .44))
    assert mirror.commands[0] == "home"
    assert mirror.commands.count("click") == 1
    assert result["screen_changed"] is False


def test_previous_button_highlight_fading_is_not_the_next_taps_effect(tmp_path):
    mirror = LaggingMirror(.2, KEYPAD)
    mirror.frame = keypad_edit(highlight=True)
    phone = calibrated_phone(tmp_path, mirror)
    phone._pointer_spots = ((.5, .44),)
    result = asyncio.run(observed_tap(phone, .5, .7))
    assert mirror.commands.count("click") == 1
    assert result["screen_changed"] is False


def test_cold_controller_old_highlight_disappearing_is_inconclusive(tmp_path):
    mirror = LaggingMirror(.2, KEYPAD)
    mirror.frame = keypad_edit(highlight=True)
    phone = calibrated_phone(tmp_path, mirror)
    phone._pos = None
    assert not phone._pointer_spots
    result = asyncio.run(observed_tap(phone, .5, .7))
    assert mirror.commands[0] == "home"
    assert mirror.commands.count("click") == 1
    assert result["screen_changed"] is False
    assert result["warning"]


def disc_frame(x, *, image_format="PNG"):
    image = Image.new("RGB", (498, 1080), (40, 80, 120))
    ImageDraw.Draw(image).ellipse((x - 12, 488, x + 12, 512), fill=(150, 150, 150))
    buffer = io.BytesIO()
    image.save(buffer, image_format)
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
    result = asyncio.run(observed_tap(phone, .5, 500 / 1080))
    assert mirror.commands == ["move 249 500", "click"]
    assert result["clicked"] and not result["pointer_seen"]
    assert list(phone._seen["x"]) == list(phone._seen["y"]) == [(.1, .1), (.5, .5), (.9, .9)]
    assert phone._file.read_bytes() == original_calibration


def test_queued_real_correction_frame_is_not_a_tap_effect(tmp_path):
    mirror = LaggingMirror(.2, (FIXTURES / "live_tap_corrected.jpg").read_bytes())
    mirror.frame = (FIXTURES / "live_tap_landed.jpg").read_bytes()
    phone = calibrated_phone(tmp_path, mirror)

    async def landed(*args, **kwargs):
        return 570.1 / 720, 663.7 / 1560

    phone._find_pointer = landed
    result = asyncio.run(observed_tap(phone, .75, .40))
    assert mirror.commands.count("click") == 1
    assert result["pointer_seen"] is True
    assert result["screen_changed"] is False


def test_quick_finder_does_not_treat_receipt_stamps_as_capture_times(tmp_path):
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
    assert result is None  # receipt stamps cannot establish a capture within the 120 ms wake
    assert mirror.commands == ["move 2 0", "move -2 0"]
    assert mirror.settled_calls == 0


def test_localize_uses_visible_pair_even_if_pointer_is_hidden_in_first_eligible_frame(tmp_path):
    class FadingPointer(LaggingMirror):
        def __init__(self):
            super().__init__()
            buffer = io.BytesIO()
            Image.new("RGB", (498, 1080), (40, 80, 120)).save(buffer, "PNG")
            self.hidden = buffer.getvalue()

        async def send(self, command):
            self.commands.append(command)
            x = 262 if command == "move 12 0" else 250
            self.pending.extend([(time.monotonic() + 1.74, self.hidden),
                                 (time.monotonic() + 1.85, disc_frame(x))])

    mirror = FadingPointer()
    mirror.frame = disc_frame(250)
    phone = calibrated_phone(tmp_path, mirror)
    phone.span = {"w": 498, "h": 1080}
    phone._pace = 4
    found = asyncio.run(phone._localize_pointer((250 / 498, 500 / 1080), 0))
    assert found is not None
    assert found[0] * 498 == pytest.approx(250, abs=2)
    assert mirror.commands == ["move 12 0", "move -12 0"]


@pytest.mark.parametrize("ack_delay", [0.05, 0.20])
def test_ble_localize_pairs_single_early_airplay_frame_per_move(tmp_path, paced_clock, ack_delay):
    class StillAirPlayMirror(LaggingMirror):
        async def send(self, command):
            self.commands.append(command)
            x = {"move 12 0": 263, "move -12 0": 250}[command]
            paced_clock.call_later(0.15, self.publish, disc_frame(x, image_format="JPEG"))
            await asyncio.sleep(ack_delay)

        def publish(self, frame):
            self.frame, self.updated = frame, time.time()

    mirror = StillAirPlayMirror()
    mirror.frame = disc_frame(250, image_format="JPEG")
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone._pace = {"w": 498, "h": 1080}, 4

    found = asyncio.run(phone._localize_pointer((250 / 498, 500 / 1080), 0))

    assert mirror.commands == ["move 12 0", "move -12 0"]
    assert found is not None
    assert found == pytest.approx((250 / 498, 500 / 1080), abs=2 / 498)
    assert phone._pointer_pair_verified


def test_ble_localize_probes_up_from_bottom_left_pen_control(tmp_path, paced_clock):
    width, height = 500, 1080
    x, outward_y, landed_y = 38, 966, 1006
    board = Image.new("RGB", (width, height), (249, 249, 249))
    toolbar = ImageDraw.Draw(board)
    toolbar.ellipse((20, 988, 56, 1024), fill=(42, 143, 182))
    toolbar.line((31, 1013, 45, 999), fill="white", width=4)

    def frame(pointer_y=None):
        image = board
        if pointer_y is not None:
            pointer = Image.new("RGBA", board.size)
            ImageDraw.Draw(pointer).ellipse((x - 12, pointer_y - 12, x + 12, pointer_y + 12),
                                           fill=(110, 110, 110, 120))
            image = Image.alpha_composite(board.convert("RGBA"), pointer).convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=84)
        return buffer.getvalue()

    baseline, outward, landed = frame(), frame(outward_y), frame(landed_y)

    class PenControlMirror(LaggingMirror):
        async def send(self, command):
            self.commands.append(command)
            frame = {"move 0 -24": outward, "move 0 24": landed}.get(command)
            if frame is not None:
                paced_clock.call_later(0.15, self.publish, frame)
            await asyncio.sleep(0.20)

        def publish(self, frame):
            self.frame, self.updated = frame, time.time()

    mirror = PenControlMirror()
    mirror.frame = landed
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone._pace = {"w": 300, "h": 648}, 4
    prior_position = phone._pos
    target = (x / width, landed_y / height)

    found = asyncio.run(phone._localize_pointer(target, 0, reference=(baseline, None)))

    assert mirror.commands == ["move 0 -24", "move 0 24"]
    assert found == pytest.approx(target, abs=.006)
    assert phone._pointer_pair_verified
    assert phone._pointer_overlay == (found, landed)
    assert phone._pos == prior_position


@pytest.mark.parametrize("arrival", ["restored", "changed", "reconnected", "too_early"])
def test_drag_requires_fresh_original_screen_before_press(tmp_path, paced_clock, arrival):
    changed = keypad_edit(digit=True)

    class DragMirror(LaggingMirror):
        generation = 1

        async def send(self, command):
            self.commands.append(command)
            if len(self.commands) == 1:
                self.publish(changed)
                delay = .15 if arrival == "too_early" else feedback.MIRROR_LAG_SECONDS + .1
                followup = changed if arrival == "changed" else KEYPAD
                paced_clock.call_later(delay, self.publish, followup, arrival == "reconnected")
            if command == "press":
                assert self.frame == KEYPAD and self.generation == 1
                assert paced_clock.elapsed > feedback.MIRROR_LAG_SECONDS

        def publish(self, frame, reconnected=False):
            self.frame, self.updated = frame, time.time()
            self.generation += int(reconnected)

    mirror = DragMirror()
    phone = calibrated_phone(tmp_path, mirror)
    points = [(.5, .5), (.55, .56)]

    if arrival == "restored":
        asyncio.run(phone.drag(points, hold=False))
        move_in, press, move_stroke, release = mirror.commands
        assert move_in.startswith("move ") and move_stroke.startswith("move ")
        assert (press, release) == ("press", "release")
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(phone.drag(points, hold=False))
        assert "press" not in mirror.commands
        assert paced_clock.elapsed > feedback.MIRROR_LAG_SECONDS


def test_ble_tap_verifies_departure_of_tinted_pointer_before_clicking(tmp_path):
    width, height = 498, 1080
    target = (250 / width, 475 / height)

    def page(x, y, fill):
        image = Image.new("RGB", (width, height), (30, 80, 160))
        draw = ImageDraw.Draw(image)
        draw.rectangle((145, 430, 360, 625), fill=(236, 235, 235))
        draw.ellipse((x - 12, y - 12, x + 12, y + 12), fill=fill)
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()

    neutral = (150, 150, 150)
    landed = page(250, 475, neutral)
    outward = page(262, 475, neutral)
    before = page(85, 400, (85, 116, 178))

    class MixedPointerMirror(LaggingMirror):
        pace_version = 4

        async def send(self, command):
            self.commands.append(command)
            frames = {"move 250 475": landed, "move 12 0": outward, "move -12 0": landed}
            if command in frames:
                self.pending.append((time.monotonic() + .15, frames[command]))

    mirror = MixedPointerMirror()
    mirror.frame = before
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone.origin, phone._pace = {"w": width, "h": height}, {"x": 0, "y": 0}, 4
    phone._clock, phone._clock_link = 0.0, 0

    result = asyncio.run(phone.tap_verified(*target))

    assert result["clicked"] and result["pointer_seen"]
    assert mirror.commands == ["move 250 475", "move 12 0", "move -12 0", "click"]


def test_stop_during_pointer_check_prevents_click(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)

    async def stopped(*args, **kwargs):
        mirror.abort_epoch += 1
        return None

    phone._find_pointer = stopped
    with pytest.raises(DeviceError, match="紧急停止"):
        asyncio.run(observed_tap(phone, .5, .44))
    assert "click" not in mirror.commands


@pytest.mark.parametrize("failed_command", ["move 12 0", "move -12 0"])
def test_failed_wake_forgets_position_and_never_clicks(tmp_path, failed_command):
    class FailedWake(LaggingMirror):
        async def send(self, command):
            self.commands.append(command)
            if command == failed_command:
                raise DeviceError("lost wake ACK")

    mirror = FailedWake()
    phone = calibrated_phone(tmp_path, mirror)
    phone._pos = (phone._dx(.5), phone._dy(.44))  # no movement; the idle pointer needs a wake
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
        asyncio.run(observed_tap(phone, .5, .44))
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
                at = time.monotonic()
                # Already received by the board before the click, delivered to
                # the host later; this can safely replace a pre-click baseline.
                self.pending.append((at + .01, old_update, time.time() * 1000 - 100))
                if after_click != "none":
                    frame = new_update if after_click == "new" else old_update
                    self.pending.append((at + phone_module.freshness.MIRROR_LAG_SECONDS + .03, frame, None))

        async def fresh(self, since, timeout):
            deadline = time.monotonic() + timeout
            while True:
                while self.pending and self.pending[0][0] <= time.monotonic():
                    _, self.frame, received_at = self.pending.pop(0)
                    self.updated = time.time()
                    self.frame_ts = self.updated * 1000 if received_at is None else received_at
                if self.updated > since:
                    return self.frame
                if time.monotonic() >= deadline:
                    return None
                await asyncio.sleep(.005)

    mirror = TimestampMirror()
    phone = calibrated_phone(tmp_path, mirror)
    phone._clock, phone._clock_link = 0.0, 0
    result = asyncio.run(observed_tap(phone, .5, .44))
    assert mirror.commands.count("click") == 1
    assert result["fresh"] is (after_click != "none")
    assert result["screen_changed"] is (after_click == "new")


def test_replaying_mirror_refuses_verified_click(tmp_path):
    mirror = LaggingMirror()
    mirror.frame_ts = (time.time() - 10) * 1000
    phone = calibrated_phone(tmp_path, mirror)
    phone._clock, phone._clock_link = 0.0, 0
    with pytest.raises(RuntimeError, match="重放"):
        asyncio.run(observed_tap(phone, .5, .44))
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
