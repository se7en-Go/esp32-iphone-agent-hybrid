"""A clicked Home may arrive late; confirmation is passive and stays bounded."""

import asyncio
from types import SimpleNamespace

import pytest
from test_assistive_confirmation import rig as early_rig
from test_assistive_home import frame

from hidagent import assistive_vision, freshness
from hidagent.operation import operation_scope, remaining_operation_time
from hidagent.screen import FrameSnapshot, Screen


def rig(tmp_path, monkeypatch, *, outcome="home", delay=2.5, transition=None, remaining=None):
    phone, device, clock = early_rig(tmp_path, monkeypatch, early_home=False)
    screen = Screen()
    screen._on_ready()
    screen._on_jpeg(device.frame)
    screen._freshness_phone = phone
    phone.screen = screen
    monkeypatch.setattr(device, "show", screen._on_jpeg)
    navigator = phone.assistive_touch
    original_wait = navigator._wait
    waits = []
    original_receipt = screen.snapshot

    def publish():
        if transition == "stop":
            device.abort_epoch += 1
        elif transition == "link":
            device.links += 1
        elif transition == "identity":
            phone.device = SimpleNamespace(links=device.links, abort_epoch=device.abort_epoch)
        elif transition == "generation":
            screen._on_ready()
        elif transition == "disconnect":
            screen.stop()
        if outcome == "silent":
            return
        screen._on_jpeg(frame(menu=outcome == "menu", home=outcome in {"home", "old_receipt"}))
        if outcome == "old_receipt":
            # New pixels carrying the old receipt still cannot prove an effect.
            current = screen.snapshot
            screen._snapshot = FrameSnapshot(current.frame, None, original_receipt.arrived,
                                             original_receipt.updated, current.generation, True)
            screen.updated = original_receipt.updated

    async def record_wait(epoch, since, timeout, accept, *, stable=False):
        waits.append((clock.elapsed, timeout, since))
        if len(waits) == 1:
            clock.call_later(timeout + delay, publish)
        result = await original_wait(epoch, since, timeout, accept, stable=stable)
        if len(waits) == 1 and remaining is not None:
            clock.advance(max(0, remaining_operation_time() - remaining))
        return result

    monkeypatch.setattr(navigator, "_wait", record_wait)
    return phone, device, screen, clock, waits


def test_home_accepts_late_source_with_original_click_without_more_hid(tmp_path, monkeypatch):
    phone, device, screen, clock, waits = rig(tmp_path, monkeypatch)
    asyncio.run(phone.go_home())
    assert device.sent[1:] == ["click"]
    assert len(waits) == 2 and waits[1][1] <= 3.2
    assert waits[0][2] == waits[1][2] == phone.last_tap["completed_at"]
    accepted = phone.assistive_touch._last_wait_observation
    assert accepted.frame == screen.frame and assistive_vision.is_home_screen(accepted.frame)
    assert accepted.updated > waits[0][2] + freshness.MIRROR_LAG_SECONDS
    assert phone.assistive_touch._saved["verified_route"] == "assistive-touch"
    assert 2.5 <= clock.elapsed - waits[1][0] < 3.2


def test_home_in_first_window_does_not_start_late_wait(tmp_path, monkeypatch):
    phone, device, _, _, waits = rig(tmp_path, monkeypatch, delay=-.6)
    asyncio.run(phone.go_home())
    assert len(waits) == 1
    assert device.sent[1:] == ["click"]
    assert phone.assistive_touch._saved["verified_route"] == "assistive-touch"


def test_early_static_refresh_remains_an_alternative_not_a_stacked_retry(tmp_path, monkeypatch):
    phone, device, _ = early_rig(tmp_path, monkeypatch)
    original_wait = phone.assistive_touch._wait
    waits = []

    async def record(*args, **kwargs):
        waits.append(args[2])
        return await original_wait(*args, **kwargs)

    monkeypatch.setattr(phone.assistive_touch, "_wait", record)
    asyncio.run(phone.go_home())
    assert len(waits) == 2
    assert device.sent[1:] == ["click", "move 2 0", "move -2 0"]


@pytest.mark.parametrize("outcome,delay", [
    ("silent", 2.5), ("menu", 2.5), ("other", 2.5), ("old_receipt", 2.5), ("home", 3.3),
])
def test_late_wait_never_confirms_missing_old_or_other_scene(tmp_path, monkeypatch, outcome, delay):
    phone, device, _, clock, waits = rig(tmp_path, monkeypatch, outcome=outcome, delay=delay)
    with pytest.raises(RuntimeError):
        asyncio.run(phone.go_home())
    assert device.sent[1:] == ["click"]
    assert len(waits) == 2 and waits[1][1] <= 3.2
    assert clock.elapsed - waits[1][0] <= 3.25
    assert "verified_route" not in phone.assistive_touch._saved


@pytest.mark.parametrize("transition", ["stop", "link", "identity", "generation", "disconnect"])
def test_late_home_stays_with_original_device_and_mirror(tmp_path, monkeypatch, transition):
    phone, device, _, _, waits = rig(tmp_path, monkeypatch, transition=transition)
    with pytest.raises(RuntimeError):
        asyncio.run(phone.go_home())
    assert len(waits) == 2
    assert device.sent[1:] == ["click"]
    assert "verified_route" not in phone.assistive_touch._saved


@pytest.mark.parametrize("remaining", [.2, .8])
def test_late_home_does_not_extend_original_operation_budget(tmp_path, monkeypatch, remaining):
    phone, device, _, _, waits = rig(tmp_path, monkeypatch, remaining=remaining, outcome="silent")

    async def scenario():
        async with operation_scope(45):
            with pytest.raises(RuntimeError):
                await phone.go_home()
            assert remaining_operation_time() > 0

    asyncio.run(scenario())
    assert device.sent[1:] == ["click"]
    assert len(waits) == (1 if remaining == .2 else 2)
    if len(waits) == 2:
        assert 0 < waits[1][1] <= remaining - .05 + 1e-6
    assert "verified_route" not in phone.assistive_touch._saved
