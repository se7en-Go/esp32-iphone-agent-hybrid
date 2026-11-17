"""Review regressions using board-receipt timestamps and an in-memory Device."""
import asyncio
import time

import pytest
from fake_clock import ManualClock
from test_verified_tap import KEYPAD, LaggingMirror, calibrated_phone, keypad_edit

from hidagent import freshness
from hidagent.device import Device


@pytest.fixture(autouse=True)
def paced_clock(monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)
    return clock


def test_board_receipt_timestamp_does_not_make_queued_preclick_image_evidence():
    view = LaggingMirror()
    clicked_at = time.time()
    view.updated = clicked_at + .031
    view.frame_ts = view.updated * 1000  # firmware stamps arrival, not capture
    assert freshness.fresh_frame(view, clock=0, since=clicked_at, now=view.updated) is None
    view.updated = clicked_at + freshness.MIRROR_LAG_SECONDS + .02
    view.frame_ts = view.updated * 1000
    assert freshness.fresh_frame(view, clock=0, since=clicked_at, now=view.updated)


class MemoryLink:
    def __init__(self):
        self.commands = []
        self.replies = asyncio.Queue()
        self.open = True

    def is_open(self):
        return self.open

    def close(self):
        self.open = False

    def write(self, data):
        seq, command = data.decode().strip().split(' ', 1)
        self.commands.append(command)
        self.replies.put_nowait(f'{seq} ok\n'.encode())

    async def readline(self):
        return await self.replies.get()


@pytest.mark.parametrize('transition', ['content', 'generation', 'stale'])
def test_click_rechecks_after_real_device_lock_wait(tmp_path, transition):
    async def run():
        view = LaggingMirror()
        device, link = Device(), MemoryLink()
        device.attach(link)
        phone = calibrated_phone(tmp_path, view)
        phone.device = device
        phone._clock, phone._clock_link = 0, device.links
        positioned = asyncio.Event()

        async def position(x, y):
            phone._pointer_confirmed = True
            positioned.set()
            return x, y

        phone.point_verified = position
        await device._lock.acquire()
        task = asyncio.create_task(phone.tap_verified(.5, .44, target_frame=KEYPAD,
                                  target_box=(.4, .3, .6, .6)))
        await positioned.wait()
        while not device._lock._waiters:
            if task.done():
                raise AssertionError(await task)
            await asyncio.sleep(0)
        if transition == 'content':
            view.frame = keypad_edit(digit=True)
        elif transition == 'generation':
            view.generation = 1
        else:
            view.updated -= 10
        device._lock.release()
        with pytest.raises(RuntimeError, match='变化|新鲜|连接'):
            await task
        assert 'click' not in link.commands

    asyncio.run(run())


def test_observation_recovers_idle_without_external_touch(tmp_path, monkeypatch):
    monkeypatch.setattr(freshness, 'MIRROR_LAG_SECONDS', .03)

    class IdleMirror(LaggingMirror):
        async def send(self, command):
            await super().send(command)
            if command.startswith('move '):
                self.pending.append((time.monotonic() + freshness.MIRROR_LAG_SECONDS + .01, self.frame))

        async def fresh(self, since, timeout):
            result = await super().fresh(since, timeout)
            if result is not None:
                self.frame_ts = self.updated * 1000
            return result

    async def run():
        view = IdleMirror()
        view.updated -= 20
        view.frame_ts = view.updated * 1000
        phone = calibrated_phone(tmp_path, view)
        phone._clock, phone._clock_link = 0, 0
        first, second = await asyncio.gather(phone.ensure_observation(), phone.ensure_observation())
        assert first.frame == second.frame == KEYPAD
        assert freshness.fresh_frame(view)
        assert view.commands == ['move 2 0', 'move -2 0']
        assert phone._pos is None

    asyncio.run(run())


def test_idle_recovery_never_accepts_replay_or_clicks(tmp_path, monkeypatch):
    monkeypatch.setattr(freshness, 'MIRROR_LAG_SECONDS', .01)
    view = LaggingMirror()
    view.updated -= 20
    view.frame_ts = (view.updated - 30) * 1000
    phone = calibrated_phone(tmp_path, view)
    phone._clock, phone._clock_link = 0, 0
    with pytest.raises(RuntimeError, match='新鲜|重放'):
        asyncio.run(phone.ensure_observation())
    assert not view.commands


@pytest.mark.parametrize('later_evidence', [False, True])
def test_receipt_stamped_preclick_change_is_not_tap_evidence(tmp_path, monkeypatch, later_evidence):
    from hidagent import phone as phone_module

    monkeypatch.setattr(freshness, 'MIRROR_LAG_SECONDS', .06)
    monkeypatch.setattr(phone_module, 'TAP_EFFECT_WAIT', .02)
    view = LaggingMirror(.031, keypad_edit(digit=True), paced=True)
    original_send = view.send
    receipts = []
    original_fresh = view.fresh

    async def send(command):
        await original_send(command)
        if command == 'click' and later_evidence:
            # Virtual firmware completion precedes both receipts. The second is
            # beyond the full mirror lag and inside the unchanged effect budget.
            view.pending.append((time.monotonic() + .07, view.effect))

    async def fresh(since, timeout):
        result = await original_fresh(since, timeout)
        if result is not None:
            receipts.append(view.frame_ts)
        return result

    view.send = send
    view.fresh = fresh
    phone = calibrated_phone(tmp_path, view)
    phone._clock, phone._clock_link = 0, 0
    phone._pos = (phone._dx(.5), phone._dy(.44))
    for axis in ('x', 'y'):
        phone._seen[axis].extend([(.1, .1), (.5, .5), (.9, .9)])
    result = asyncio.run(phone.tap_verified(.5, .44, target_frame=KEYPAD, target_box=(.4, .3, .6, .6)))
    assert result['clicked'] is True
    assert result['fresh'] is later_evidence
    assert result['effect_observed'] is (True if later_evidence else None)
    assert result['screen_changed'] is later_evidence and result['effect_verified'] is False
    assert view.commands == ['click']
    deltas = [freshness.board_delta(stamp, result['completed_at'] * 1000) for stamp in receipts]
    assert deltas == pytest.approx([31, 70] if later_evidence else [31], abs=.001)
    assert (deltas[-1] >= freshness.MIRROR_LAG_SECONDS * 1000) is later_evidence


def test_idle_refresh_timeout_is_bounded_and_serializes_normal_keys(tmp_path, monkeypatch):
    from hidagent import phone as phone_module

    monkeypatch.setattr(freshness, 'MIRROR_LAG_SECONDS', .01)
    monkeypatch.setattr(phone_module, 'OBSERVATION_REFRESH_WAIT', .08)

    async def run():
        view = LaggingMirror()
        view.updated -= 20
        phone = calibrated_phone(tmp_path, view)
        nudged, resume = asyncio.Event(), asyncio.Event()
        original_send = view.send

        async def send(command):
            await original_send(command)
            if command == 'move 2 0':
                nudged.set()
                await resume.wait()

        view.send = send
        refresh = asyncio.create_task(phone.ensure_observation())
        await nudged.wait()
        key = asyncio.create_task(phone.key('tab'))
        while not phone._interaction_lock._waiters:
            if key.done():
                raise AssertionError(await key)
            await asyncio.sleep(0)
        assert not key.done() and view.commands == ['move 2 0']
        resume.set()
        with pytest.raises(RuntimeError, match='新鲜'):
            await refresh
        await key
        assert view.commands == ['move 2 0', 'move -2 0', 'key tab']

    started = time.monotonic()
    asyncio.run(run())
    assert time.monotonic() - started < .3
