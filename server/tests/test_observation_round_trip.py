"""A wake returns its pointer before authorizing an observation or any action."""

import asyncio
import time
from dataclasses import replace

import pytest
from fake_clock import ManualClock
from PIL import ImageChops
from test_control_input_context import Link
from test_pointer_fade_context import encode, label_frame, pointer

from hidagent import freshness, screen_compare
from hidagent.device import Device, DeviceError
from hidagent.operation import Operation, _current
from hidagent.phone import Phone
from hidagent.screen import Screen


@pytest.fixture
def clock(monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)
    return clock


def setup(tmp_path, clock, *, forward_delay=1.71, return_delay=1.71, board_delay=None):
    original = encode(pointer(label_frame()))
    # A deliberately conservative comparison rejects this tiny pointer motion.
    # The wake must restore it, not teach the guard to ignore arbitrary changes.
    forward = encode(ImageChops.offset(pointer(ImageChops.offset(label_frame(), -1, 0)), 1, 0))
    screen, device, events = Screen(), Device(), []
    screen._on_jpeg(original)
    screen._snapshot = replace(screen.snapshot, updated=time.time() - 18, arrived=time.monotonic() - 18)
    screen.updated = screen.snapshot.updated

    def publish(frame):
        screen._on_jpeg(frame)
        if board_delay is not None:
            screen._snapshot = replace(screen.snapshot, board_ts=int((time.time() - board_delay) * 1000))

    class MirrorLink(Link):
        def write(self, data):
            super().write(data)
            command = data.decode().strip().split(" ", 1)[1]
            events.append((clock.elapsed, command))
            if command == "move 2 0":
                clock.call_later(forward_delay, publish, forward)
            elif command == "move -2 0" and return_delay is not None:
                clock.call_later(return_delay, publish, original)

    link = MirrorLink()
    device.attach(link)
    phone = Phone(device, tmp_path / "cal.json", screen)
    phone._clock, phone._clock_link = 0, device.links

    def original_layout():
        current = freshness.require_fresh(screen, max_age=float("inf"))
        if screen_compare.observation_changed(original, current.frame):
            raise RuntimeError("original layout changed")

    return phone, device, screen, events, original, forward, original_layout


def test_own_forward_frame_does_not_prevent_the_only_return(tmp_path, clock):
    async def scenario():
        phone, device, _, events, original, forward, guard = setup(tmp_path, clock, forward_delay=.01)
        assert screen_compare.observation_changed(original, forward)
        try:
            accepted = await phone.ensure_observation(before_refresh=guard)
            assert [command for _, command in events] == ["move 2 0", "move -2 0"]
            assert events[1][0] == pytest.approx(.05)
            assert accepted.frame == original
            assert accepted.updated > clock.wall_origin + events[1][0] + freshness.MIRROR_LAG_SECONDS
            assert phone._pos is None
        finally:
            device.close()
    asyncio.run(scenario())


def test_ordinary_observe_does_not_return_a_forward_frame_after_reverse_ack(tmp_path, clock):
    async def scenario():
        phone, device, _, events, original, _, _ = setup(tmp_path, clock)
        try:
            accepted = await phone.ensure_observation()
            assert accepted.frame == original
            assert accepted.updated > clock.wall_origin + events[1][0] + freshness.MIRROR_LAG_SECONDS
            assert clock.elapsed >= events[1][0] + freshness.MIRROR_LAG_SECONDS
        finally:
            device.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("board_delay", [None, .06])
def test_forward_or_board_receipt_before_return_boundary_cannot_authorize(tmp_path, clock, board_delay):
    async def scenario():
        # In the board case the return arrives after 1.71s, but its board stamp
        # is only 1.65s after the return. A fresh host receipt cannot repair it.
        phone, device, _, events, _, _, _ = setup(
            tmp_path, clock, return_delay=None if board_delay is None else 1.71, board_delay=board_delay)
        try:
            with pytest.raises(RuntimeError, match="新鲜"):
                await phone.ensure_observation()
            assert [command for _, command in events] == ["move 2 0", "move -2 0"]
            assert clock.elapsed < 4
        finally:
            device.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("return_delay,accepted", [(1.71, False), (2.01, True)])
def test_causal_boundary_is_reverse_ack_completion_not_reverse_send(tmp_path, clock, return_delay, accepted):
    async def scenario():
        phone, device, _, events, original, _, _ = setup(tmp_path, clock, return_delay=return_delay)
        link = device._link
        original_read = link.readline

        async def delayed_ack():
            if len(link.written) == 2:
                await asyncio.sleep(.2)
            return await original_read()

        link.readline = delayed_ack
        try:
            if accepted:
                frame = await phone.ensure_observation()
                assert frame.frame == original
                assert frame.updated > clock.wall_origin + events[1][0] + .2 + freshness.MIRROR_LAG_SECONDS
            else:
                with pytest.raises(RuntimeError, match="新鲜"):
                    await phone.ensure_observation()
            assert len(events) == 2
        finally:
            device.close()
    asyncio.run(scenario())


def test_unknown_forward_ack_does_not_authorize_an_inverse(tmp_path, clock):
    async def scenario():
        phone, device, _, events, _, _, _ = setup(tmp_path, clock)

        async def missing_ack():
            raise asyncio.TimeoutError

        device._link.readline = missing_ack
        try:
            with pytest.raises(DeviceError):
                await phone.ensure_observation()
            assert [command for _, command in events] == ["move 2 0"]
        finally:
            device.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["forward", "return"])
@pytest.mark.parametrize("transition", ["generation", "device", "link", "abort", "deadline", "cancelled"])
def test_actual_write_rechecks_original_session_even_for_inverse(tmp_path, stage, transition):
    async def scenario():
        # Real event-loop waits here expose the Device lock boundary; no clock
        # scheduling or delivery is stubbed after the lock has been acquired.
        clock = ManualClock()
        phone, device, screen, events, original, _, _ = setup(tmp_path, clock)
        operation = Operation(time.monotonic() + 10)
        token = _current.set(operation)
        original_send = phone._send
        queued = asyncio.Event()

        async def send(epoch, command, **kwargs):
            if command == ("move 2 0" if stage == "forward" else "move -2 0"):
                await device._lock.acquire()
                queued.set()
            return await original_send(epoch, command, **kwargs)

        phone._send = send
        task = asyncio.create_task(phone.ensure_observation())
        try:
            await asyncio.wait_for(queued.wait(), 1)
            while not device._lock._waiters:
                if task.done():
                    await task
                await asyncio.sleep(0)
            if transition == "generation":
                screen._on_ready()
                screen._on_jpeg(original)
            elif transition == "device":
                phone.device = Device()
            elif transition == "link":
                device.links += 1
            elif transition == "abort":
                device._abort_epoch += 1
            elif transition == "deadline":
                operation.deadline = time.monotonic() - 1
            else:
                operation.cancelled = True
            device._lock.release()
            with pytest.raises((RuntimeError, asyncio.CancelledError)):
                await task
            assert [command for _, command in events] == ([] if stage == "forward" else ["move 2 0"])
        finally:
            if device._lock.locked():
                device._lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            _current.reset(token)
            device.close()
    asyncio.run(scenario())
