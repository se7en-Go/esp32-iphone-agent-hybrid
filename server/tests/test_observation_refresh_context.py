"""Original layout guards forward and final evidence; inverse keeps its session."""

import asyncio
import time
from dataclasses import replace

import pytest
from test_controls import jpeg
from test_host_observed_click import setup

from hidagent.freshness import require_fresh


@pytest.mark.parametrize("stage", ["forward", "return"])
def test_refresh_checks_layout_before_forward_and_after_return_video(tmp_path, monkeypatch, stage):
    monkeypatch.setattr("hidagent.freshness.MIRROR_LAG_SECONDS", .001)

    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        original = screen.frame
        screen._snapshot = replace(screen.snapshot, updated=time.time() - 6)
        screen.updated = screen.snapshot.updated
        checks = []

        def original_context():
            current = require_fresh(screen, max_age=float("inf"))
            checks.append(current.frame)
            if current.frame != original:
                raise RuntimeError("original context changed")

        original_send = phone._send
        locked = asyncio.Event()

        async def send(epoch, command, **kwargs):
            if (stage == "forward" and command == "move 2 0"
                    or stage == "return" and command == "move -2 0"):
                await device._lock.acquire()
                locked.set()
            result = await original_send(epoch, command, **kwargs)
            if command == "move -2 0":
                # A genuinely post-return source frame still has the changed
                # layout. Cleanup must finish, but it must not authorize input.
                asyncio.get_running_loop().call_later(.01, screen._on_jpeg, jpeg("black"))
            return result

        phone._send = send
        task = asyncio.create_task(phone.ensure_observation(before_refresh=original_context))
        try:
            await asyncio.wait_for(locked.wait(), 1)

            async def queued():
                while not device._lock._waiters:
                    await asyncio.sleep(0)

            await asyncio.wait_for(queued(), 1)
            screen._on_jpeg(jpeg("black"))
            device._lock.release()
            with pytest.raises(RuntimeError, match="original context changed"):
                await task
            assert link.written == ([] if stage == "forward" else [b"1 move 2 0\n", b"2 move -2 0\n"])
            assert checks[-1] != original
        finally:
            if device._lock.locked():
                device._lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


def test_fresh_observation_also_validates_original_caller_context(tmp_path):
    async def scenario():
        phone, device, _, link = setup(tmp_path)

        def expired_ticket():
            raise RuntimeError("ticket expired")

        try:
            with pytest.raises(RuntimeError, match="ticket expired"):
                await phone.ensure_observation(before_refresh=expired_ticket)
            assert link.written == []
        finally:
            device.close()

    asyncio.run(scenario())
