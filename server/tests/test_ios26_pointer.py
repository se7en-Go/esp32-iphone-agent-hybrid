"""Pointer finding and calibration on real iPhone 15 Pro Max / iOS 26.3.1 mirror frames (see fixtures/ios26)."""

import asyncio
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageEnhance

from hidagent import phone as phone_module
from hidagent.phone import Phone
from hidagent.vision import pointer, pointer_move

FIXTURES = Path(__file__).parent / "fixtures" / "ios26"
W, H = 720, 1560
AT_BUTTON = (600, 1180, 700, 1280)  # AssistiveTouch, px


def frame(name: str) -> bytes:
    return (FIXTURES / f"{name}.jpg").read_bytes()


def edited(jpeg: bytes, *, size=None, fade_button=False) -> bytes:
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if fade_button:  # AssistiveTouch fades to its idle opacity: a big round change that must not count
        spot = img.crop(AT_BUTTON)
        img.paste(ImageEnhance.Brightness(spot).enhance(1.25), AT_BUTTON[:2])
    if size:
        img = img.resize(size, Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


@pytest.mark.parametrize("before, after, at", [
    ("01_m150_2000ms", "02_nudge_r0_400ms", (250.9, 158.8)),
    ("04_m200_2000ms", "05_nudge_r_400ms", (463.5, 371.3)),
])
def test_ios26_grey_pointer_is_found_from_a_real_nudge(before, after, at):
    for shift in (12 / 677, 0.025):  # calibrated span, and the guess used while calibrating
        found = pointer(frame(before), frame(after), near=(0.5, 0.5), shift=shift, reach=(0.5, 0.5))
        assert found is not None
        assert found[0] * W == pytest.approx(at[0], abs=2) and found[1] * H == pytest.approx(at[1], abs=2)


def test_the_way_back_gives_the_resting_spot():
    moved = pointer_move(frame("02_nudge_r0_400ms"), frame("01_m150_2000ms"), (0.5, 0.5), -0.025, (0.5, 0.5))
    assert moved is not None
    assert moved[1][0] * W == pytest.approx(250.9, abs=2) and moved[1][1] * H == pytest.approx(158.8, abs=2)


def test_wrong_direction_or_a_real_move_is_not_a_nudge():
    assert pointer(frame("01_m150_2000ms"), frame("02_nudge_r0_400ms"), (0.5, 0.5), -0.025, (0.5, 0.5)) is None
    # a 200 unit diagonal move, mid-way and at the end: far more than a nudge, and not sideways
    assert pointer(frame("04_m200_200ms"), frame("04_m200_2000ms"), (0.5, 0.5), 0.025, (0.5, 0.5)) is None
    assert pointer(frame("01_m150_2000ms"), frame("04_m200_200ms"), (0.5, 0.5), 0.025, (0.5, 0.5)) is None
    # the pointer appears out of the half hidden home spot: one disc, nothing to pair, no centroid guess
    assert pointer(frame("00_home_1500ms"), frame("01_m150_2000ms"), (0.5, 0.5), 0.025, (0.5, 0.5)) is None


def test_assistive_touch_button_is_never_the_pointer():
    still = frame("01_m150_2000ms")
    assert pointer(still, edited(still, fade_button=True), (0.5, 0.5), 0.025, (0.5, 0.5)) is None
    assert pointer(still, edited(still, fade_button=True), (650 / W, 1230 / H), 0.025) is None
    found = pointer(still, edited(frame("02_nudge_r0_400ms"), fade_button=True), (0.5, 0.5), 0.025, (0.5, 0.5))
    assert found[0] * W == pytest.approx(250.9, abs=2) and found[1] * H == pytest.approx(158.8, abs=2)


def test_pointer_is_found_on_a_473_wide_frame_too():
    size = (473, 1024)
    found = pointer(edited(frame("01_m150_2000ms"), size=size), edited(frame("02_nudge_r0_400ms"), size=size),
                    (0.5, 0.5), 0.025, (0.5, 0.5))
    assert found[0] * W == pytest.approx(250.9, abs=3) and found[1] * H == pytest.approx(158.8, abs=3)


class MirrorPhone:
    """The phone plus its AirPlay mirror, replaying the real frames.

    Each command changes which frame the phone shows; the mirror delivers it `lag` seconds later and then stays
    silent (AirPlay pushes nothing while the screen is still), the way the captures behaved.
    """

    FRAMES = {(0, 0): "00_home_1500ms", (150, 150): "01_m150_2000ms", (162, 150): "02_nudge_r0_400ms",
              (350, 350): "04_m200_2000ms", (362, 350): "05_nudge_r_400ms"}

    def __init__(self, lag: float):
        self.lag, self.pos, self.sent, self.links = lag, None, [], 0
        self.frame = frame("00_home_1500ms")
        self.updated = time.time()
        self._pending = []
        self.live = True
        self.aspect = W / H

    async def send(self, command: str) -> None:
        self.sent.append(command)
        if command == "home":
            self.pos = (0, 0)
        elif command.startswith("move "):
            dx, dy = map(int, command.split()[1:3])
            self.pos = (self.pos[0] + dx, self.pos[1] + dy)
        name = next((n for (x, y), n in self.FRAMES.items()
                     if abs(x - self.pos[0]) <= 3 and abs(y - self.pos[1]) <= 3), None)
        if name:
            self._pending.append((time.monotonic() + self.lag, frame(name)))

    def _deliver(self) -> None:
        now = time.monotonic()
        while self._pending and self._pending[0][0] <= now:
            self.frame, self.updated = self._pending.pop(0)[1], time.time()

    async def settled(self, **kwargs) -> None:
        await asyncio.sleep(kwargs.get("least", 0.25))
        self._deliver()

    async def fresh(self, since: float, timeout: float = 0.8):
        deadline = time.monotonic() + timeout
        while True:
            self._deliver()
            if self.updated > since:
                return self.frame
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("lag", [0.15, 0.9, 1.6])  # the nudges in the captures showed up 0.15 to 1.5 s late
def test_auto_calibration_succeeds_on_the_real_frames_despite_mirror_lag(tmp_path, monkeypatch, lag):
    monkeypatch.setattr(phone_module, "POINTER_WAIT", 2.0)
    mirror = MirrorPhone(lag)
    phone = Phone(mirror, tmp_path / "calibration.json", mirror)
    assert asyncio.run(phone.start_calibration()) is True
    # 200 units moved the pointer 212.6 px across and 212.5 px down; home stopped 91 px right of the corner
    assert phone.span["w"] == pytest.approx(677, abs=8) and phone.span["h"] == pytest.approx(1468, abs=15)
    assert phone.origin["x"] * W == pytest.approx(91, abs=3) and abs(phone.origin["y"] * H) < 3
    assert json.loads((tmp_path / "calibration.json").read_text())["x0"] == pytest.approx(phone.origin["x"])
    # each nudge is taken back, so the pointer ends where the last stop put it
    assert mirror.pos == (350, 350) and mirror.sent.count("move 12 0") == mirror.sent.count("move -12 0")
    # a tap aimed at the middle of the screen now lands there (1.063 px per unit for these moves)
    assert 91 + phone._dx(0.5) * 212.6 / 200 == pytest.approx(W / 2, abs=4)


def test_old_calibration_without_origin_still_loads(tmp_path):
    (tmp_path / "calibration.json").write_text(json.dumps({"w": 400, "h": 800}))
    phone = Phone(SimpleNamespace(), tmp_path / "calibration.json", None)
    assert phone.origin == {"x": 0.0, "y": 0.0} and phone._dx(0.5) == 200


def test_manual_finish_counts_from_the_rounded_corner(tmp_path):
    # after home + move 200 200 the pointer sat at (91 + 212.6, 212.5) px; the user clicked it there
    x, y = (91 + 212.6) / W, 212.5 / H
    phone = Phone(SimpleNamespace(), tmp_path / "calibration.json", SimpleNamespace(frame=b"x", aspect=W / H))
    phone.finish_calibration(x, y)
    assert phone.origin["x"] * W == pytest.approx(91, abs=3)
    for target in (0.2, 0.5, 0.8):  # one point used to leave up to ~48 px of error at the left of the screen
        landed = 91 + phone._dx(target) * 212.6 / 200
        assert landed == pytest.approx(target * W, abs=3)


def test_manual_finish_without_a_frame_keeps_the_one_point_scale(tmp_path):
    phone = Phone(SimpleNamespace(), tmp_path / "calibration.json", None)
    phone.finish_calibration(0.5, 0.25)
    assert phone.span == {"w": 400, "h": 800} and phone.origin == {"x": 0.0, "y": 0.0}


def test_failed_auto_calibration_leaves_the_pointer_where_manual_finish_expects_it(tmp_path, monkeypatch):
    monkeypatch.setattr(phone_module, "POINTER_WAIT", 0.3)
    mirror = MirrorPhone(0.1)
    mirror.FRAMES = {(0, 0): "00_home_1500ms", (150, 150): "01_m150_2000ms", (162, 150): "02_nudge_r0_400ms"}
    phone = Phone(mirror, tmp_path / "calibration.json", mirror)
    assert asyncio.run(phone.start_calibration()) is False  # the second stop never shows up
    assert mirror.sent[-2:] == ["home", f"move {phone_module.CALIBRATION_MOVE} {phone_module.CALIBRATION_MOVE}"]


def test_verified_tap_clicks_on_the_real_pointer_with_a_slow_mirror(tmp_path, monkeypatch):
    """The 1.2 s nudge window used to run out before a late frame came: 'pointer could not be verified'."""
    monkeypatch.setattr(phone_module, "POINTER_WAIT", 2.0)
    mirror = MirrorPhone(1.4)
    phone = Phone(mirror, tmp_path / "calibration.json", mirror)
    assert asyncio.run(phone.start_calibration())
    phone._pos = None  # cold: re-homed before the move, then the conservative check
    mirror.sent.clear()
    asyncio.run(phone.tap_verified(463.5 / W, 371.3 / H))
    assert mirror.sent[0] == "home" and mirror.sent[-1] == "click"
    assert mirror.sent.count("move 12 0") == mirror.sent.count("move -12 0") >= 1
