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
from hidagent.screen import FrameSnapshot, Screen
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
        self.t0 = time.monotonic()
        self.lag, self.pos, self.sent, self.links = lag, None, [], 0
        self.frame = frame("00_home_1500ms")
        self.updated = time.time()
        self.snapshot = FrameSnapshot(self.frame, self.board_ms() - 300, time.monotonic())
        self.frame_ts = self.snapshot.board_ts
        self._pending = []
        self.live = True
        self.aspect = W / H

    def board_ms(self) -> int:
        return (3_600_000 + int((time.monotonic() - self.t0) * 1000)) % 2**32

    async def query(self, command: str) -> str:
        assert command == "status"
        return json.dumps({"up": self.board_ms()})

    def _publish(self, jpeg, ts):
        self.snapshot = FrameSnapshot(jpeg, ts, time.monotonic())
        self.frame, self.frame_ts, self.updated = jpeg, ts, time.time()

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
            # The board receives the resulting AirPlay image after the HID command, even at short mirror lag.
            self._pending.append((time.monotonic() + self.lag, frame(name), (self.board_ms() + 10) % 2**32))

    def _deliver(self) -> None:
        now = time.monotonic()
        while self._pending and self._pending[0][0] <= now:
            _, jpeg, ts = self._pending.pop(0)
            self._publish(jpeg, ts)

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

    async def fresh_snapshot(self, since: float, timeout: float = 0.8):
        deadline = time.monotonic() + timeout
        while True:
            self._deliver()
            if self.snapshot.arrived > since:
                return self.snapshot
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


@pytest.mark.parametrize("wrap", [False, True])
def test_verified_tap_clicks_on_the_real_pointer_with_a_slow_mirror(tmp_path, monkeypatch, wrap):
    """The 1.2 s nudge window used to run out before a late frame came: 'pointer could not be verified'."""
    monkeypatch.setattr(phone_module, "POINTER_WAIT", 2.0)
    mirror = MirrorPhone(1.4)
    if wrap:
        mirror.t0 -= (2**32 - 3_600_000 - 500) / 1000  # millis wraps during calibration
        mirror._publish(mirror.frame, (mirror.board_ms() - 300) % 2**32)
    monkeypatch.setattr(time, "time", lambda: -10_000)  # wall-clock corrections cannot affect frame ordering
    phone = Phone(mirror, tmp_path / "calibration.json", mirror)
    assert asyncio.run(phone.start_calibration())
    phone._pos = None  # cold: re-homed before the move, then the conservative check
    mirror.sent.clear()
    asyncio.run(phone.tap_verified(463.5 / W, 371.3 / H))
    assert mirror.sent[0] == "home" and mirror.sent[-1] == "click"
    assert mirror.sent.count("move 12 0") == mirror.sent.count("move -12 0") >= 1


class ReplayingMirror(MirrorPhone):
    """Right after a reconnect the board replays old video as fast as it can: frames whose board timestamp is
    minutes old, here a pointer being nudged somewhere else entirely. Live frames carry a current timestamp."""

    def __init__(self, lag: float, replay_for: float):
        super().__init__(lag)
        self.replay_until = self.t0 + replay_for
        self.replay_deliveries = 0
        self._replay = [frame("04_m200_2000ms"), frame("05_nudge_r_400ms")]
        self._publish(self._replay[0], 1000)

    async def replay(self):
        # Independent producer: catch_up does not call fresh/settled. The old test delivered zero replay frames.
        while time.monotonic() < self.replay_until:
            self._publish(self._replay[self.replay_deliveries % 2], 1000 + self.replay_deliveries)
            self.replay_deliveries += 1
            await asyncio.sleep(.05)
        self._publish(frame("01_m150_2000ms"), self.board_ms())


def test_pointer_search_waits_out_the_board_replaying_old_video(tmp_path, monkeypatch):
    monkeypatch.setattr(phone_module, "POINTER_WAIT", 2.0)
    mirror = ReplayingMirror(lag=0.3, replay_for=1.5)
    mirror.pos = (150, 150)
    mirror._pending.clear()
    phone = Phone(mirror, tmp_path / "calibration.json", mirror)
    compared = []
    original = phone_module.vision.pointer_move

    def compare(before, after, *args):
        compared.append((before, after))
        return original(before, after, *args)

    monkeypatch.setattr(phone_module.vision, "pointer_move", compare)

    async def scenario():
        producer = asyncio.create_task(mirror.replay())
        try:
            return await phone._find_pointer()
        finally:
            await producer

    found = asyncio.run(scenario())
    assert mirror.replay_deliveries >= 10
    assert compared and all(before not in mirror._replay and after not in mirror._replay
                            for before, after in compared), "old replay frames must not enter verification"
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

    screen = SimpleNamespace(live=True)
    phone = Phone(SimpleNamespace(send=send, links=0), tmp_path / "calibration.json", screen)
    seen = iter([(570.1 / W, 663.7 / H), (540.5 / W, 624.0 / H)])

    async def locate(near, **kwargs): return next(seen)

    phone._find_pointer = locate
    asyncio.run(phone.tap_verified(0.75, 0.40))
    assert sent[1] == "move 422 587" and sent[-2:] == ["move -28 -37", "click"]
    # the long move went 1.135 px per unit, not the 1.063 of the calibration stops: aim shorter next time
    assert phone._dx(0.75) < 412 and phone._dy(0.40) < 570
    assert json.loads((tmp_path / "calibration.json").read_text())["seen"]["x"][-1][1] == pytest.approx(570.1 / W)


def test_a_still_screen_is_not_mistaken_for_a_replay(tmp_path):
    """Its last frame was live when it came, however long ago; only frames that arrive late are a replay."""
    now = time.monotonic()
    screen = SimpleNamespace(snapshot=FrameSnapshot(b"x", 1_000_000, now - 30))
    phone = Phone(SimpleNamespace(), tmp_path / "calibration.json", screen)
    clock = 1_000_000 + 300 - (now - 30) * 1000  # it reached the board 0.3 s before it got here, 30 s ago
    assert phone._frame_age(clock) == pytest.approx(0.3, abs=0.01)
    screen.snapshot = FrameSnapshot(b"x", (1_000_000 - 1_500_000) % 2**32, now - 30)
    assert phone._frame_age(clock) > phone_module.REPLAY_LAG


@pytest.mark.parametrize("board_at_nudge", [1000, 2**32 - 20])
@pytest.mark.parametrize("delta", [-100, 0, 40])
def test_nudge_candidate_must_be_newer_than_the_command_even_across_wrap(
        tmp_path, monkeypatch, board_at_nudge, delta):
    monkeypatch.setattr(phone_module, "POINTER_WAIT", .08)
    screen = Screen()
    phone = Phone(SimpleNamespace(), tmp_path / "calibration.json", screen)
    comparisons = []
    original = phone_module.vision.pointer_move

    def compare(*args):
        comparisons.append(args)
        return original(*args)

    monkeypatch.setattr(phone_module.vision, "pointer_move", compare)
    sent = time.monotonic()
    clock = board_at_nudge - sent * 1000
    screen._on_ts((board_at_nudge + delta) % 2**32)
    screen._on_jpeg(frame("live_tap_corrected_nudged"))
    found = asyncio.run(phone._watch(0, [frame("live_tap_corrected")], (.75, .4),
                                    12 / 678, sent, False, True, clock))
    if delta <= 0:
        assert found is None and not comparisons
    else:
        assert comparisons and found is not None
        assert found[0] * W == pytest.approx(540.5, abs=2)


def test_still_screen_can_establish_a_baseline_without_waiting_for_unsolicited_frames(tmp_path, monkeypatch):
    mirror = MirrorPhone(.15)
    mirror.pos = (150, 150)
    mirror.frame = frame("01_m150_2000ms")
    mirror.snapshot = FrameSnapshot(mirror.frame, mirror.board_ms() - 30_300, time.monotonic() - 30)
    mirror.frame_ts = mirror.snapshot.board_ts
    phone = Phone(mirror, tmp_path / "calibration.json", mirror)
    monkeypatch.setattr(time, "time", lambda: -10_000)
    started = time.monotonic()
    found = asyncio.run(phone._find_pointer())
    assert found is not None and found[0] * W == pytest.approx(250.9, abs=2)
    assert time.monotonic() - started < 2


def test_verified_tap_rejects_the_last_five_seconds_of_replay(tmp_path, monkeypatch):
    """Review's spec_replay_tail_repro.py: old target/nudge frames must not confirm a wrong live landing."""
    monkeypatch.setattr(phone_module, "POINTER_WAIT", 0.1)
    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps({"w": 678, "h": 1466, "x0": .1273, "y0": -.0006}))
    screen = Screen()

    class Board:
        links = 0
        abort_epoch = 0

        def __init__(self):
            self.started = time.monotonic()
            self.sent = []
            self.replayed = 0
            self.actual = None
            self.clicked = None

        def ms(self):
            return 3_600_000 + int((time.monotonic() - self.started) * 1000)

        async def query(self, command):
            assert command == "status"
            return json.dumps({"up": self.ms()})

        async def send(self, command):
            self.sent.append(command)
            if command == "home":
                self.actual = (91.6, 0)
            elif command == "move 422 587":
                self.actual = (570.1, 663.7)
            elif command in {"move 12 0", "move -12 0"}:
                self.actual = (580.7 if command == "move 12 0" else 570.1, 663.7)
                await asyncio.sleep(.01)
                screen._on_ts(self.ms() - 4000)
                screen._on_jpeg(frame("live_tap_corrected_nudged" if command == "move 12 0"
                                      else "live_tap_corrected"))
                self.replayed += 1
            elif command == "click":
                self.clicked = self.actual

    board = Board()
    screen._on_ts(board.ms() - 4200)
    screen._on_jpeg(frame("live_tap_corrected"))
    phone = Phone(board, calibration, screen)
    with pytest.raises(RuntimeError, match="pointer could not be verified"):
        asyncio.run(phone.tap_verified(.75, .40))
    assert board.replayed >= 2, "the test must actually deliver old frames"
    assert board.clicked is None and "click" not in board.sent


def test_a_frame_from_before_the_actual_serial_write_cannot_verify_a_queued_nudge(tmp_path, monkeypatch):
    """Device.send may queue behind a serial exchange; the command boundary is link.write, not its caller."""
    from hidagent.device import Device

    monkeypatch.setattr(phone_module, "POINTER_WAIT", .05)
    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps({"w": 678, "h": 1466, "x0": .1273, "y0": -.0006}))

    async def scenario():
        screen = Screen()
        device = Device()
        started = time.monotonic()

        def board_ms():
            return 3_600_000 + int((time.monotonic() - started) * 1000)

        class Link:
            def __init__(self):
                self.replies = asyncio.Queue()
                self.written = []
                self.actual = (570.1, 663.7)
                self.clicked = None

            def write(self, data):
                req_id, command = data.decode().strip().split(" ", 1)
                self.written.append((command, board_ms()))
                if command.startswith("move "):
                    dx, dy = map(int, command.split()[1:])
                    self.actual = (self.actual[0] + dx, self.actual[1] + dy)
                elif command == "click":
                    self.clicked = self.actual
                reply = json.dumps({"up": board_ms()}) if command == "status" else "ok"
                self.replies.put_nowait(f"{req_id} {reply}\n".encode())

            async def readline(self):
                return await self.replies.get()

            def is_open(self):
                return True

            def close(self):
                pass

        link = Link()
        device.attach(link)
        phone = Phone(device, calibration, screen)
        phone._pos, phone._link = (422, 587), device.links
        replayed = []
        producers = []

        async def replay_while_queued():
            try:
                await asyncio.sleep(.02)
                timestamp = board_ms()
                screen._on_ts(timestamp)
                screen._on_jpeg(frame("live_tap_corrected_nudged"))
                replayed.append(timestamp)
                await asyncio.sleep(.02)
            finally:
                device._lock.release()

        async def settle_and_queue(**_kwargs):
            screen._on_ts(board_ms() - 4200)
            screen._on_jpeg(frame("live_tap_corrected"))
            await device._lock.acquire()
            producers.append(asyncio.create_task(replay_while_queued()))

        screen.settled = settle_and_queue
        screen._on_ts(board_ms() - 4200)
        screen._on_jpeg(frame("live_tap_corrected"))
        try:
            with pytest.raises(RuntimeError, match="pointer could not be verified"):
                await phone.tap_verified(.75, .40)
        finally:
            await asyncio.gather(*producers)
        nudge_writes = [timestamp for command, timestamp in link.written if command == "move 12 0"]
        assert replayed and len(replayed) == len(nudge_writes)
        assert all(old < sent for old, sent in zip(replayed, nudge_writes, strict=True))
        assert link.clicked is None and all(command != "click" for command, _ in link.written)

    asyncio.run(scenario())
