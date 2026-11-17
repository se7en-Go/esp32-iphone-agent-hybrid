"""A host can read a ticket without causing or racing pointer wake movements."""

import asyncio
import time
from dataclasses import replace

import pytest
from fake_clock import ManualClock
from test_controls import jpeg
from test_host_observed_click import evidence, no_effect, setup

from hidagent.phone import HostObservation


def test_host_qualification_cannot_be_publicly_constructed():
    with pytest.raises(TypeError):
        HostObservation()


def test_passive_observation_and_click_never_wake_or_move(tmp_path):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)

        async def forbidden(*args, **kwargs):
            pytest.fail("host observation must not wake or move the pointer")

        phone.ensure_observation = phone._refresh_observation = phone.point = forbidden
        phone._tap_effect = no_effect
        try:
            qualified = await phone.observe_host_pointer()
            assert qualified.snapshot.frame == screen.frame
            assert qualified.input_sequence == 0
            assert link.written == []
            result = await phone.click_observed(**evidence(phone, screen), host_observation=qualified)
            assert link.written == [b"1 click\n"]
            assert result["evidence_source"] == "host_observed"
            assert not result["effect_verified"] and not result["pointer_seen"]
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("publish", [False, True])
def test_passive_issuance_requires_source_after_last_input_not_elapsed_host_time(tmp_path, monkeypatch, publish):
    clock = ManualClock()
    clock.install(monkeypatch)

    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        device.last_input_at = clock.time() - 1.6
        original = screen.snapshot
        if publish:
            clock.call_later(.12, screen._on_jpeg, original.frame)
        try:
            if publish:
                qualified = await phone.observe_host_pointer(timeout=.3)
                assert qualified.snapshot.updated > device.last_input_at + 1.7
                assert qualified.snapshot.updated > original.updated
            else:
                with pytest.raises(RuntimeError, match="镜像延迟"):
                    await phone.observe_host_pointer(timeout=.3)
            assert link.written == [] and device.input_sequence == 0
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("publish", [False, True])
def test_passive_issuance_preserves_original_board_causality(tmp_path, monkeypatch, publish):
    clock = ManualClock()
    clock.install(monkeypatch)

    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        device.last_input_at = clock.time() - 2
        screen._snapshot = replace(screen.snapshot, board_ts=int((clock.time() - 1) * 1000) % 2**32)

        def new_source():
            screen._on_jpeg(jpeg())
            screen._snapshot = replace(screen.snapshot, board_ts=int(clock.time() * 1000) % 2**32)

        if publish:
            clock.call_later(.12, new_source)
        try:
            if publish:
                qualified = await phone.observe_host_pointer(timeout=.3)
                assert qualified.snapshot.frame_ts == int(qualified.snapshot.updated * 1000) % 2**32
            else:
                with pytest.raises(RuntimeError, match="镜像延迟"):
                    await phone.observe_host_pointer(timeout=.3)
            assert link.written == []
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["link", "generation", "abort", "input"])
def test_passive_issuance_cannot_rebind_a_session_while_waiting(tmp_path, monkeypatch, transition):
    clock = ManualClock()
    clock.install(monkeypatch)

    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        clock.advance(6)

        def change():
            if transition == "link":
                device.links += 1
            elif transition == "generation":
                screen._on_ready()
            elif transition == "abort":
                device._abort_epoch += 1
            else:
                device.input_sequence += 1
            screen._on_jpeg(jpeg())

        clock.call_later(.05, change)
        try:
            with pytest.raises(RuntimeError, match="变化"):
                await phone.observe_host_pointer(timeout=.3)
            assert link.written == []
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("qualified_path", [False, True])
def test_ten_second_reading_time_needs_issued_qualification_and_identical_new_source(
        tmp_path, monkeypatch, qualified_path):
    clock = ManualClock()
    clock.install(monkeypatch)

    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        phone._tap_effect = no_effect
        qualified = await phone.observe_host_pointer()
        original = evidence(phone, screen)
        clock.advance(10)
        screen._on_jpeg(qualified.snapshot.frame)
        try:
            if qualified_path:
                await phone.click_observed(**original, host_observation=qualified)
                assert link.written == [b"1 click\n"]
            else:
                with pytest.raises(RuntimeError, match="过期"):
                    await phone.click_observed(**original)
                assert link.written == []
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("result", ["same", "none", "different", "ttl", "input", "generation"])
def test_qualified_click_passively_waits_without_reacquiring_or_reanchoring(tmp_path, monkeypatch, result):
    clock = ManualClock()
    clock.install(monkeypatch)

    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        phone._tap_effect = no_effect
        qualified = await phone.observe_host_pointer()
        original = evidence(phone, screen)
        clock.advance(29.9 if result == "ttl" else 10)

        def next_frame():
            if result == "input":
                device.input_sequence += 1
            if result == "generation":
                screen._on_ready()
            screen._on_jpeg(jpeg("black") if result == "different" else qualified.snapshot.frame)

        if result != "none":
            clock.call_later(.2, next_frame)
        if result == "different":
            clock.call_later(.5, screen._on_jpeg, qualified.snapshot.frame)
        began = clock.monotonic()
        try:
            if result == "same":
                await phone.click_observed(**original, host_observation=qualified)
                assert link.written == [b"1 click\n"]
            else:
                with pytest.raises(RuntimeError):
                    await phone.click_observed(**original, host_observation=qualified)
                assert link.written == []
                if result == "different":
                    assert clock.monotonic() - began < .5  # never wait for B to become A again
                if result == "none":
                    assert clock.monotonic() - began <= 3.51
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["pixels", "link", "input", "expired", "stale"])
def test_qualified_click_rechecks_at_actual_locked_write(tmp_path, transition):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        phone._tap_effect = no_effect
        qualified = await phone.observe_host_pointer()
        original = evidence(phone, screen)
        await device._lock.acquire()
        task = asyncio.create_task(phone.click_observed(**original, host_observation=qualified))
        try:
            async def waiting():
                while not device._lock._waiters:
                    if task.done():
                        await task
                    await asyncio.sleep(0)

            await asyncio.wait_for(waiting(), 1)
            if transition == "pixels":
                screen._on_jpeg(jpeg("black"))
            elif transition == "link":
                device.links += 1
            elif transition == "input":
                device.input_sequence += 1
            elif transition == "expired":
                object.__setattr__(qualified, "issued_monotonic", time.monotonic() - 31)
            else:
                screen._snapshot = replace(screen.snapshot, updated=time.time() - 6)
            device._lock.release()
            with pytest.raises(RuntimeError):
                await task
            assert link.written == []
        finally:
            if device._lock.locked():
                device._lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["observe", "click"])
def test_passive_wait_respects_operation_deadline_without_later_input(tmp_path, operation):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        qualified = await phone.observe_host_pointer()
        original = evidence(phone, screen)
        if operation == "observe":
            qualified.lease.release()
        screen._snapshot = replace(screen.snapshot, updated=time.time() - 6)
        screen.updated = screen.snapshot.updated
        phone.operation_timeout = .02
        try:
            with pytest.raises(RuntimeError, match="deadline"):
                if operation == "observe":
                    await phone.observe_host_pointer()
                else:
                    await phone.click_observed(**original, host_observation=qualified)
            screen._on_jpeg(jpeg())
            await asyncio.sleep(.02)
            assert link.written == []
        finally:
            device.close()

    asyncio.run(scenario())


def test_qualification_cannot_be_paired_with_a_different_original_snapshot(tmp_path):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        qualified = await phone.observe_host_pointer()
        original = evidence(phone, screen)
        original["observation"] = replace(original["observation"], updated=original["observation"].updated - 1)
        try:
            with pytest.raises(ValueError, match="does not belong"):
                await phone.click_observed(**original, host_observation=qualified)
            assert link.written == []
        finally:
            device.close()

    asyncio.run(scenario())
