"""Only background idle movement waits while a host reads pointer evidence."""

import asyncio

import pytest
from fake_clock import ManualClock
from test_control_input_context import Link
from test_controls import jpeg
from test_host_observed_click import evidence, setup

from hidagent.device import DeviceError


def test_fixed_lease_does_not_rewrite_input_time_or_renew_itself(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)
    _, device, _, _ = setup(tmp_path)
    original = device.input_sequence, device.last_input_at, device.session_started_at
    first = device.acquire_host_observation_lease()
    expires = first.expires_monotonic
    clock.advance(10)
    with pytest.raises(DeviceError):
        device.acquire_host_observation_lease()
    assert first.expires_monotonic == expires
    assert (device.input_sequence, device.last_input_at, device.session_started_at) == original
    clock.advance(20)
    assert not first.active and not device._idle_nudge_paused()
    second = device.acquire_host_observation_lease()
    first.release()  # an old request's cleanup must not cancel a newer lease
    assert second.active
    second.release()
    second.release()
    assert not device._idle_nudge_paused()
    device.close()


def test_keep_awake_keeps_heartbeats_but_defers_only_idle_moves(tmp_path, monkeypatch):
    async def scenario():
        _, device, _, link = setup(tmp_path)
        lease = device.acquire_host_observation_lease()
        device._last -= 30
        device._beat -= 30
        beats = 0

        async def heartbeat(_delay):
            nonlocal beats
            beats += 1
            if beats > 1:
                raise asyncio.CancelledError

        monkeypatch.setattr("hidagent.device.asyncio.sleep", heartbeat)
        try:
            with pytest.raises(asyncio.CancelledError):
                await device.keep_awake()
            assert link.written == [b"1 ping\n"]
            assert device.input_sequence == 0
            # Explicit commands are not exempted from normal input accounting,
            # nor blocked by an idle-only lease.
            await device.send("key x")
            assert device.input_sequence == 1
            lease.release()
            await device._nudge_idle()
            assert link.written[-2:] == [b"3 move 1 0\n", b"4 move -1 0\n"]
        finally:
            device.close()

    asyncio.run(scenario())


def test_queued_background_forward_is_rechecked_at_actual_write(tmp_path):
    async def scenario():
        _, device, _, link = setup(tmp_path)
        await device._lock.acquire()
        task = asyncio.create_task(device._nudge_idle())
        try:
            async def queued():
                while not device._lock._waiters:
                    await asyncio.sleep(0)

            await asyncio.wait_for(queued(), 1)
            lease = device.acquire_host_observation_lease()
            device._lock.release()
            with pytest.raises(DeviceError, match="paused"):
                await task
            assert link.written == [] and device.input_sequence == 0
            assert not device._idle_nudge_inflight
            lease.release()
        finally:
            if device._lock.locked():
                device._lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


def test_started_background_return_drains_before_observation_binds_input_sequence(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)

    class DelayedForward(Link):
        def write(self, data):
            self.written.append(data)
            if len(self.written) > 1:
                self.replies.put_nowait(data.split(b" ", 1)[0] + b" ok\n")

    async def scenario():
        phone, device, screen, _ = setup(tmp_path)
        link = DelayedForward()
        device.attach(link)
        device.session_started_at = clock.time() - 2
        phone._clock_link = device.links
        nudge = asyncio.create_task(device._nudge_idle())
        observation = None
        try:
            while not link.written:
                await asyncio.sleep(0)
            observation = asyncio.create_task(phone.observe_host_pointer())
            while device._host_observation_lease is None:
                await asyncio.sleep(0)
            assert not observation.done()
            assert device._idle_nudge_inflight
            link.replies.put_nowait(b"1 ok\n")
            await nudge
            assert link.written == [b"1 move 1 0\n", b"2 move -1 0\n"]
            clock.advance(1.71)
            screen._on_jpeg(jpeg())
            qualified = await observation
            assert qualified.input_sequence == device.input_sequence == 2
            assert qualified.snapshot.updated > device.last_input_at + 1.7
            assert qualified.expires_monotonic - clock.monotonic() < 30
            qualified.lease.release()
        finally:
            for task in (nudge, observation):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (nudge, observation) if t is not None), return_exceptions=True)
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("lock_name", ["phone", "device"])
def test_cancelled_queued_click_releases_observation_lease(tmp_path, lock_name):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        qualified = await phone.observe_host_pointer()
        lock = phone._interaction_lock if lock_name == "phone" else device._lock
        await lock.acquire()
        task = asyncio.create_task(phone.click_observed(**evidence(phone, screen), host_observation=qualified))
        try:
            async def queued():
                while not lock._waiters:
                    await asyncio.sleep(0)

            await asyncio.wait_for(queued(), 1)
            assert qualified.lease.active
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not qualified.lease.active
            lock.release()
            await asyncio.sleep(0)
            assert link.written == []
        finally:
            if lock.locked():
                lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())
