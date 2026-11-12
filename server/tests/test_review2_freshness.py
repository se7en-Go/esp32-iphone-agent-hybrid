"""Freshness must gate public recovery and each calibration nudge."""

import asyncio
import json
import time

import pytest
from fake_clock import ManualClock
from test_assistive_fallback import Device, Screen, make_phone
from test_ios26_pointer import MirrorPhone, frame

from hidagent import assistive, freshness, vision
from hidagent import phone as phone_module
from hidagent.phone import Phone


@pytest.fixture(autouse=True)
def bounded_waits(monkeypatch):
    monkeypatch.setattr(assistive, "RESULT_WAIT", .02)
    monkeypatch.setattr(phone_module, "CATCH_UP_WAIT", .02)
    monkeypatch.setattr(phone_module, "POINTER_WAIT", .08)
    monkeypatch.setattr(phone_module, "HOLD", 0)
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", .03)


def test_public_home_refuses_recently_delivered_replay_without_hid(tmp_path):
    screen, device = Screen(), Device()
    screen.frame_ts = (screen.updated - 10) * 1000

    async def query(command):
        return json.dumps({"up": time.time() * 1000})

    device.query = query
    phone = make_phone(tmp_path, device, screen)
    with pytest.raises(RuntimeError, match="新鲜|重放"):
        asyncio.run(phone.go_home())
    assert device.sent == []


def test_public_home_propagates_target_context_rejection(tmp_path):
    screen, device = Screen(), Device()
    phone = make_phone(tmp_path, device, screen)

    async def changed(epoch):
        raise RuntimeError("目标观察后画面连接已变化，未发送点击；请重新观察。")

    phone.assistive_touch._assistive_home = changed
    with pytest.raises(RuntimeError, match="连接已变化"):
        asyncio.run(phone.go_home())
    assert device.sent == []


@pytest.mark.parametrize("lost", ["disconnect", "generation", "replay"])
def test_public_home_revalidates_before_the_next_fallback(tmp_path, lost):
    screen = Screen()

    def lose_evidence(command):
        if command == "release":
            if lost == "disconnect":
                screen.live = False
            elif lost == "generation":
                screen.generation = 1
            else:
                screen.frame_ts = (screen.updated - 10) * 1000

    device = Device(lose_evidence)
    phone = make_phone(tmp_path, device, screen)
    with pytest.raises(RuntimeError, match="新鲜|连接"):
        asyncio.run(phone.go_home())
    assert "release" in device.sent
    assert "homescreen" not in device.sent and "key cmd+h" not in device.sent


class QueuedCalibration(MirrorPhone):
    """Actual fixture motion arrives now but was stamped before the nudge."""

    def __init__(self):
        super().__init__(0)
        self.frame_ts = self.updated * 1000
        self.queued = []

    async def query(self, command):
        return json.dumps({"up": time.time() * 1000})

    async def settled(self, **kwargs):
        pass

    async def send(self, command):
        self.sent.append(command)
        if command == "home":
            self.pos = (0, 0)
        elif command.startswith("move "):
            dx, dy = map(int, command.split()[1:])
            self.pos = self.pos[0] + dx, self.pos[1] + dy
        name = self.FRAMES.get(self.pos)
        if name is not None:
            image = frame(name)
            if command in {"move 12 0", "move -12 0"}:
                self.queued.append((image, (time.time() - .2) * 1000))
            else:
                self.frame, self.updated = image, time.time()
                self.frame_ts = self.updated * 1000

    async def fresh(self, since, timeout=.8):
        if self.queued:
            self.frame, self.frame_ts = self.queued.pop(0)
            self.updated = time.time()
            return self.frame
        await asyncio.sleep(min(timeout, .005))
        return None


def test_calibration_never_learns_queued_pre_nudge_motion(tmp_path):
    # This is real detector evidence, not a stubbed pointer return.
    assert vision.pointer_move(frame("01_m150_2000ms"), frame("02_nudge_r0_400ms"),
                               (.5, .5), .025, (.5, .5)) is not None
    mirror = QueuedCalibration()
    saved = tmp_path / "calibration.json"
    phone = Phone(mirror, saved, mirror)
    assert asyncio.run(phone.start_calibration()) is False
    assert not saved.exists() and phone.span is None
    assert all(not samples for samples in phone._seen.values())
    assert mirror.sent[-2:] == ["home", "move 200 200"]


def test_watch_keeps_operation_boundary_separate_from_receive_cursor(tmp_path):
    async def run():
        mirror = QueuedCalibration()
        mirror.frame = frame("01_m150_2000ms")
        mirror.pos = (150, 150)
        phone = Phone(mirror, tmp_path / "calibration.json", mirror)
        started = time.time()
        sent = time.monotonic()
        # _watch's clock is board millis minus monotonic millis. This mirror stamps wall time.
        clock = started * 1000 - sent * 1000
        await mirror.send("move 12 0")
        found = await phone._watch(0, [frame("01_m150_2000ms")], None, .025,
                                   sent, False, True, clock)
        assert freshness.fresh_frame(mirror, clock=0) is not None
        assert freshness.fresh_frame(mirror, clock=0, since=started) is None
        assert found is None

    asyncio.run(run())


def test_watch_accepts_eligible_motion_after_rejecting_a_queued_frame(tmp_path, monkeypatch):
    manual_clock = ManualClock()
    manual_clock.install(monkeypatch)

    async def run():
        mirror = QueuedCalibration()
        mirror.pos = (150, 150)
        phone = Phone(mirror, tmp_path / "calibration.json", mirror)
        started = time.time()
        sent = time.monotonic()
        clock = started * 1000 - sent * 1000
        await mirror.send("move 12 0")
        delivered = False
        original_fresh = mirror.fresh

        async def fresh(since, timeout):
            nonlocal delivered
            if mirror.queued:
                return await original_fresh(since, timeout)
            if not delivered:
                await asyncio.sleep(max(0, started + .04 - time.time()))
                mirror.updated = time.time()
                mirror.frame_ts = mirror.updated * 1000
                delivered = True
                return mirror.frame
            return await original_fresh(since, timeout)

        mirror.fresh = fresh
        found = await phone._watch(0, [frame("01_m150_2000ms")], None, .025,
                                   sent, False, True, clock)
        assert found == pytest.approx((250.9 / 720, 158.8 / 1560), abs=.003)

    asyncio.run(run())


def test_calibration_rejects_reconnect_during_nudge(tmp_path):
    mirror = QueuedCalibration()
    phone = Phone(mirror, tmp_path / "calibration.json", mirror)
    original_send = mirror.send

    async def send(command):
        await original_send(command)
        if command == "move 12 0":
            mirror.generation = 1

    mirror.send = send
    with pytest.raises(RuntimeError, match="连接已变化"):
        asyncio.run(phone.start_calibration())
    assert phone.span is None and not phone._file.exists()
