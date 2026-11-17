"""Refresh near-expiry observations before motion without weakening write freshness."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from test_control_input_context import Link

from hidagent.device import Device
from hidagent.freshness import require_fresh
from hidagent.phone import Phone


def setup_phone(tmp_path, monkeypatch, age):
    wall = [1_800_000_000.0]
    monkeypatch.setattr("time.time", lambda: wall[0])
    screen = SimpleNamespace(frame=b"frame", updated=wall[0] - age,
                             frame_ts=None, generation=1, live=True)
    phone = Phone(SimpleNamespace(links=1, abort_epoch=0), tmp_path / "calibration.json", screen)
    events = []

    async def refresh(stale, epoch, **_kwargs):
        events.append("refresh")
        assert stale.generation == screen.generation == 1
        assert epoch == phone.device.abort_epoch == 0
        wall[0] += 1.8
        screen.updated = wall[0]
        return require_fresh(screen)

    phone._refresh_observation = refresh
    return phone, screen, wall, events


def test_near_expiry_frame_is_reacquired_before_observation_ticket(tmp_path, monkeypatch):
    phone, _, wall, events = setup_phone(tmp_path, monkeypatch, 4.37)
    accepted = asyncio.run(phone.ensure_observation())
    assert events == ["refresh"]
    assert wall[0] - accepted.updated <= 1.0


def test_young_frame_returns_without_hid_refresh(tmp_path, monkeypatch):
    phone, screen, _, events = setup_phone(tmp_path, monkeypatch, .2)
    original_updated = screen.updated
    accepted = asyncio.run(phone.ensure_observation())
    assert events == []
    assert accepted.updated == original_updated


def test_refresh_cannot_return_an_observation_without_movement_headroom(tmp_path, monkeypatch):
    phone, screen, _, _ = setup_phone(tmp_path, monkeypatch, 4.37)

    async def still_old(*_args, **_kwargs):
        return require_fresh(screen)

    phone._refresh_observation = still_old
    with pytest.raises(RuntimeError, match="画面不够新鲜"):
        asyncio.run(phone.ensure_observation())


def test_direct_point_keeps_existing_five_second_expiry(tmp_path, monkeypatch):
    phone, _, wall, events = setup_phone(tmp_path, monkeypatch, 4.37)

    async def point(*_args, **_kwargs):
        events.append("move")
        wall[0] += .64

    async def find(*_args, **_kwargs):
        events.append("wait-for-pointer-video")
        return .5, .5

    phone.point, phone._find_pointer = point, find
    with pytest.raises(RuntimeError, match="画面不够新鲜"):
        asyncio.run(phone.point_verified(.5, .5))
    assert events == ["move"]


@pytest.mark.parametrize("transition", ["generation", "link", "abort"])
def test_real_wake_never_returns_motion_on_a_replacement_session(tmp_path, monkeypatch, transition):
    monkeypatch.setattr("hidagent.freshness.MIRROR_LAG_SECONDS", .01)

    async def scenario():
        screen = SimpleNamespace(frame=b"frame", updated=time.time() - 4.37,
                                 frame_ts=None, generation=1, live=True)
        device = Device()

        class ReconnectAfterWrite(Link):
            def write(self, data):
                super().write(data)
                if len(self.written) == 1:
                    if transition == "generation":
                        screen.generation += 1
                    elif transition == "link":
                        device.links += 1
                    else:
                        device._abort_epoch += 1

        link = ReconnectAfterWrite()
        device.attach(link)
        phone = Phone(device, tmp_path / "cal.json", screen)
        phone._clock, phone._clock_link = 0, device.links

        async def fresh(*_args):
            await asyncio.sleep(.001)
            screen.updated = time.time()
            return screen.frame

        screen.fresh = fresh
        try:
            with pytest.raises(RuntimeError, match="变化|停止"):
                await phone.ensure_observation()
            assert link.written == [b"1 move 2 0\n"]
            assert phone._pos is None
        finally:
            device.close()

    asyncio.run(scenario())


def test_newer_frame_in_original_session_is_accepted_after_refresh(tmp_path, monkeypatch):
    phone, screen, wall, _ = setup_phone(tmp_path, monkeypatch, 4.37)
    original = phone._refresh_observation

    async def newer(*args, **kwargs):
        refreshed = await original(*args, **kwargs)
        wall[0] += .01
        screen.frame, screen.updated = b"newer frame in the same session", wall[0]
        return refreshed

    phone._refresh_observation = newer
    accepted = asyncio.run(phone.ensure_observation())
    assert accepted.frame == screen.frame
    assert accepted.generation == 1
