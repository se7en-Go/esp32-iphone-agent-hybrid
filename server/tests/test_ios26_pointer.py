"""Pointer finding and calibration on real iPhone 15 Pro Max / iOS 26.3.1 mirror frames (see fixtures/ios26)."""

import asyncio
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageEnhance

from hidagent import freshness
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
    # This fake ties each image to its command; no unknown capture pipeline.
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 0)
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
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 0)
    monkeypatch.setattr(phone_module, "POINTER_WAIT", 0.3)
    mirror = MirrorPhone(0.1)
    mirror.FRAMES = {(0, 0): "00_home_1500ms", (150, 150): "01_m150_2000ms", (162, 150): "02_nudge_r0_400ms"}
    phone = Phone(mirror, tmp_path / "calibration.json", mirror)
    assert asyncio.run(phone.start_calibration()) is False  # the second stop never shows up
    assert mirror.sent[-2:] == ["home", f"move {phone_module.CALIBRATION_MOVE} {phone_module.CALIBRATION_MOVE}"]


def test_two_point_calibration_does_not_license_a_blind_slow_mirror_tap(tmp_path, monkeypatch):
    """Steering: two fitted points and no target bounds do not authorize an unseen-pointer click."""
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 0)
    monkeypatch.setattr(phone_module, "POINTER_WAIT", 2.0)
    mirror = MirrorPhone(1.4)
    phone = Phone(mirror, tmp_path / "calibration.json", mirror)
    assert asyncio.run(phone.start_calibration())
    phone._pos = None  # cold: re-home, then use calibration if the disc arrives too late
    mirror.sent.clear()
    with pytest.raises(RuntimeError, match="目标|落点"):
        asyncio.run(phone.tap_verified(463.5 / W, 371.3 / H))
    assert mirror.sent[0] == "home" and "click" not in mirror.sent
    assert "move 2 0" not in mirror.sent and "move -2 0" not in mirror.sent


class ReplayingMirror(MirrorPhone):
    """Right after a reconnect the board replays old video as fast as it can: frames whose board timestamp is
    minutes old, here a pointer being nudged somewhere else entirely. Live frames carry a current timestamp."""

    def __init__(self, lag: float, replay_for: float):
        super().__init__(lag)
        self.t0 = time.monotonic()
        self.replay_until = self.t0 + replay_for
        self.frame_ts = 0
        self._live_baseline = False
        self._replay = [frame("04_m200_2000ms"), frame("05_nudge_r_400ms")]

    def board_ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000) + 3_600_000

    async def query(self, command: str) -> str:
        assert command == "status"
        return json.dumps({"up": self.board_ms()})

    def _deliver(self) -> None:
        now = time.monotonic()
        if now < self.replay_until:
            self.frame = self._replay[int(now * 10) % 2]
            self.frame_ts, self.updated = 1000, time.time()  # an hour behind the board's clock
            return
        if not self._live_baseline:
            self._live_baseline = True
            self.frame = frame("01_m150_2000ms")
            self.frame_ts, self.updated = self.board_ms(), time.time()
        while self._pending and self._pending[0][0] <= now:
            _, self.frame = self._pending.pop(0)
            self.frame_ts = self.board_ms()  # firmware stamps board receipt, after mirror transport
            self.updated = time.time()


def test_pointer_search_waits_out_the_board_replaying_old_video(tmp_path, monkeypatch):
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 0)
    monkeypatch.setattr(phone_module, "POINTER_WAIT", 2.0)
    mirror = ReplayingMirror(lag=0.3, replay_for=1.5)
    mirror.pos = (150, 150)
    mirror._pending.clear()
    phone = Phone(mirror, tmp_path / "calibration.json", mirror)
    found = asyncio.run(phone._find_pointer())
    assert found is not None  # the replayed nudge at (463, 371) never counts
    assert found[0] * W == pytest.approx(250.9, abs=2) and found[1] * H == pytest.approx(158.8, abs=2)


@pytest.mark.parametrize("before, after, at", [
    ("live_tap_landed", "live_tap_landed_nudged", (570.1, 663.7)),
    ("live_tap_corrected", "live_tap_corrected_nudged", (540.5, 624.0)),  # a nudge of only 5.1 px
])
def test_live_verified_tap_frames(before, after, at):
    found = pointer(frame(before), frame(after), near=(0.75, 0.40), shift=12 / 678)
    assert found[0] * W == pytest.approx(at[0], abs=2) and found[1] * H == pytest.approx(at[1], abs=2)


def test_a_verified_tap_learns_how_far_long_moves_go(tmp_path):
    """Live: home + 422/587 units landed at (570.1, 663.7) px aiming at (540, 624); the next aim allows for it."""
    calibration = {"w": 678, "h": 1466, "x0": 0.1273, "y0": -0.0006,
                   "seen": {"x": [[0.3486, 0.3486], [0.6435, 0.6434]], "y": [[0.1018, 0.1018], [0.2382, 0.2382]]}}
    (tmp_path / "calibration.json").write_text(json.dumps(calibration))
    sent = []

    async def send(command): sent.append(command)

    async def no_frame(*args, **kwargs):
        return None
    screen = SimpleNamespace(live=True, frame=frame("live_tap_landed"), updated=time.time(), fresh=no_frame)
    phone = Phone(SimpleNamespace(send=send, links=0), tmp_path / "calibration.json", screen)
    seen = iter([(570.1 / W, 663.7 / H), (540.5 / W, 624.0 / H)])

    async def locate(near, **kwargs): return next(seen)

    phone._find_pointer = locate
    asyncio.run(phone.tap_verified(0.75, 0.40, target_box=(.62, .27, .88, .53), target_frame=screen.frame))
    assert sent[1] == "move 422 587" and sent[-2:] == ["move -28 -37", "click"]
    # the long move went 1.135 px per unit, not the 1.063 of the calibration stops: aim shorter next time
    assert phone._dx(0.75) < 412 and phone._dy(0.40) < 570
    assert json.loads((tmp_path / "calibration.json").read_text())["seen"]["x"][-1][1] == pytest.approx(570.1 / W)


def test_a_still_screen_is_not_mistaken_for_a_replay(tmp_path):
    """Its last frame was live when it came, however long ago; only frames that arrive late are a replay."""
    now = time.time()
    screen = SimpleNamespace(frame=b"x", frame_ts=1_000_000, updated=now - 30, live=True)
    phone = Phone(SimpleNamespace(), tmp_path / "calibration.json", screen)
    clock = 1_000_000 + 300 - (now - 30) * 1000  # it reached the board 0.3 s before it got here, 30 s ago
    assert phone._frame_age(clock) == pytest.approx(0.3, abs=0.01)
    screen.frame_ts = 1_000_000 - 1_500_000  # 25 minutes old when it came: the board replaying its GOP
    assert phone._frame_age(clock) > phone_module.REPLAY_LAG
