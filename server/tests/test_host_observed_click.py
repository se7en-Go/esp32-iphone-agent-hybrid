"""A separate host-observed click never moves, recalibrates, or claims CV verification."""

import asyncio
import time
from dataclasses import replace

import pytest
from fake_clock import ManualClock
from test_control_input_context import Link
from test_controls import jpeg

from hidagent.device import Device, DeviceError
from hidagent.freshness import snapshot
from hidagent.phone import Phone
from hidagent.screen import Screen


def setup(tmp_path):
    screen, device, link = Screen(), Device(), Link()
    screen._on_jpeg(jpeg())
    device.attach(link)
    # Model an already-established connection; dedicated tests below exercise
    # the actual attach boundary and its elapsed delivery allowance.
    device.session_started_at = time.time() - 2
    phone = Phone(device, tmp_path / "cal.json", screen)
    phone._clock, phone._clock_link = 0, device.links
    return phone, device, screen, link


def evidence(phone, screen):
    return {"observation": snapshot(screen), "pointer_xy": (.5, .5), "target_box": (.4, .4, .6, .6),
            "expected_device": phone.device, "expected_links": phone.device.links,
            "expected_abort_epoch": phone.device.abort_epoch, "expected_input_seq": phone.device.input_sequence}


async def no_effect(*_args, **_kwargs):
    return False, False


def test_single_click_without_any_pointer_movement_or_cv_claim(tmp_path):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)

        async def forbidden(*_args, **_kwargs):
            pytest.fail("host-observed click must not acquire or move the pointer")

        phone.ensure_observation = phone.point = phone.point_verified = forbidden
        phone._tap_effect = no_effect
        try:
            result = await phone.click_observed(**evidence(phone, screen))
            assert link.written == [b"1 click\n"]
            assert device.input_sequence == 1
            assert result["clicked"] is True
            assert result["evidence_source"] == "host_observed"
            assert result["pointer_seen"] is False and result["effect_verified"] is False
            assert not phone._pointer_confirmed and not phone._pointer_pair_verified
            assert result["fresh"] is False and result["_after_frame"] is None
        finally:
            device.close()

    asyncio.run(scenario())


def test_observed_hold_is_stationary_and_acknowledges_both_edges(tmp_path):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        phone._tap_effect = no_effect

        async def forbidden(*_args, **_kwargs):
            pytest.fail("observed hold must never move or acquire the pointer")

        phone.ensure_observation = phone.point = phone.point_verified = forbidden
        began = time.monotonic()
        try:
            result = await phone.click_observed(**evidence(phone, screen), duration=.3)
            assert [line.split(b" ", 1)[1].strip() for line in link.written] == [b"press", b"release"]
            assert time.monotonic() - began >= .3
            assert result["held"] and result["hold_seconds"] == .3
            assert result["click_count"] == 0 and result["clicked"] is False
            assert result["evidence_source"] == "host_observed" and not result["effect_verified"]
            assert not phone._pointer_confirmed and not phone._pointer_pair_verified
            assert device.input_sequence == 2 and device._pressed_link is None
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("duration", [True, 0, -.1, .299, 3.001, float("nan"), float("inf"), "1"])
def test_observed_hold_rejects_invalid_duration_without_input(tmp_path, duration):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        try:
            with pytest.raises(ValueError, match="duration"):
                await phone.click_observed(**evidence(phone, screen), duration=duration)
            assert link.written == []
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["lost_press_ack", "press_rejected", "stop", "cancel", "release_rejected"])
def test_observed_hold_failure_releases_once_and_never_retries_contact(tmp_path, failure):
    async def scenario():
        phone, device, screen, _ = setup(tmp_path)
        pressed = asyncio.Event()

        class HoldLink(Link):
            def write(self, data):
                command = data.split(b" ", 1)[1].strip()
                if command == b"press":
                    pressed.set()
                    if failure == "lost_press_ack":
                        self.written.append(data)
                        raise OSError("press may have arrived")
                if ((command == b"release" and failure == "release_rejected")
                        or (command == b"press" and failure == "press_rejected")):
                    self.written.append(data)
                    self.replies.put_nowait(data.split(b" ", 1)[0] + b" error release\n")
                else:
                    super().write(data)

        link = HoldLink()
        device.attach(link)
        device.session_started_at = time.time() - 2
        phone._clock_link = device.links
        phone._tap_effect = no_effect
        task = asyncio.create_task(phone.click_observed(**evidence(phone, screen), duration=.3))
        try:
            if failure in {"stop", "cancel"}:
                await asyncio.wait_for(pressed.wait(), 1)
                await asyncio.sleep(.01)
                if failure == "stop":
                    device.request_abort()
                else:
                    task.cancel()
            with pytest.raises((RuntimeError, asyncio.CancelledError)):
                await task
            assert [line.split(b" ", 1)[1].strip() for line in link.written] == [b"press", b"release"]
            assert device._pressed_link is None
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


def test_observed_hold_stop_while_release_queued_cleans_original_contact(tmp_path):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        phone._tap_effect = no_effect
        task = asyncio.create_task(phone.click_observed(**evidence(phone, screen), duration=.3))

        async def pressed():
            while not link.written:
                if task.done():
                    await task
                await asyncio.sleep(0)

        try:
            await asyncio.wait_for(pressed(), 1)
            await asyncio.sleep(.01)  # the press ACK is consumed before blocking release
            await device._lock.acquire()

            async def release_queued():
                while not device._lock._waiters:
                    if task.done():
                        await task
                    await asyncio.sleep(.005)

            await asyncio.wait_for(release_queued(), 1)
            device.request_abort()
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(task, 2)
            assert [line.split(b" ", 1)[1].strip() for line in link.written] == [b"press", b"release"]
            assert device._pressed_link is None
            device._lock.release()
            await asyncio.sleep(.01)
            assert len(link.written) == 2  # no cancelled queued release later leaks through
        finally:
            if device._lock.locked():
                device._lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


def test_emergency_release_uses_only_held_handle_and_clears_closed_handle():
    device = Device()

    class BrokenRelease(Link):
        closed = False

        def write(self, data):
            self.written.append(data)
            raise OSError("original UART gone")

        def close(self):
            self.closed = True

    original, replacement = BrokenRelease(), Link()
    device.attach(original)
    device._pressed_link = original
    device.attach(replacement)
    device._release_held_nowait(original)
    assert original.closed and device._pressed_link is None
    assert len(original.written) == 1 and original.written[0].endswith(b" release\n")
    assert replacement.written == [] and device._link is replacement
    device._release_held_nowait(original)
    assert len(original.written) == 1
    device.close()


@pytest.mark.parametrize("qualified", [False, True])
def test_observed_hold_rejects_background_move_queued_before_press_ack(tmp_path, qualified):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        phone._tap_effect = no_effect
        original = evidence(phone, screen)
        extra = {}
        if qualified:
            host = await phone.observe_host_pointer()
            original["observation"] = host.snapshot
            extra["host_observation"] = host
        moves = []
        write = link.write

        def queue_idle(data):
            write(data)
            if data.endswith(b" press\n"):
                if qualified:
                    # Lease lifetime is independent of the admitted hold. Its
                    # expiry must not permit background movement while down.
                    host.lease.release()
                moves.append(asyncio.create_task(device._nudge_idle()))

        link.write = queue_idle
        try:
            result = await phone.click_observed(**original, **extra, duration=.3)
            errors = await asyncio.gather(*moves, return_exceptions=True)
            assert len(errors) == 1 and isinstance(errors[0], DeviceError)
            assert result["held"] and device._pressed_link is None
            assert [line.split(b" ", 1)[1].strip() for line in link.written] == [b"press", b"release"]
        finally:
            await asyncio.gather(*moves, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("pending", ["held", "idle_return"])
def test_observed_hold_cannot_take_over_an_existing_contact_or_idle_pair(tmp_path, pending):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        phone._tap_effect = no_effect
        if pending == "held":
            device._pressed_link = link
        else:
            device._idle_nudge_inflight = True
        try:
            with pytest.raises(RuntimeError):
                await phone.click_observed(**evidence(phone, screen), duration=.3)
            assert link.written == []
        finally:
            device._pressed_link = None
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["frame", "generation", "link", "abort", "input", "stale", "device"])
@pytest.mark.parametrize("duration", [None, .3])
def test_original_observation_is_rechecked_inside_device_lock(tmp_path, transition, duration):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        phone._tap_effect = no_effect
        original = evidence(phone, screen)
        await device._lock.acquire()
        task = asyncio.create_task(phone.click_observed(**original, **({"duration": duration} if duration else {})))
        try:
            async def waiting():
                while not device._lock._waiters:
                    if task.done():
                        await task
                    await asyncio.sleep(0)

            await asyncio.wait_for(waiting(), 1)
            if transition == "frame":
                screen._on_jpeg(jpeg("black"))
            elif transition == "generation":
                screen._on_ready()
                screen._on_jpeg(jpeg())
            elif transition == "link":
                device.links += 1
            elif transition == "abort":
                device._abort_epoch += 1
            elif transition == "input":
                device.input_sequence += 1
            elif transition == "device":
                phone.device = Device()
            else:
                screen._snapshot = replace(screen.snapshot, updated=screen.updated - 10,
                                           arrived=time.monotonic() - 10)
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


def test_host_click_deadline_cancels_queued_exchange_without_input(tmp_path):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        phone.operation_timeout = .02
        await device._lock.acquire()
        try:
            with pytest.raises(RuntimeError, match="deadline"):
                await phone.click_observed(**evidence(phone, screen))
            device._lock.release()
            await asyncio.sleep(.02)
            assert link.written == []
            assert device.input_sequence == 0
        finally:
            if device._lock.locked():
                device._lock.release()
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("delay", [0, .5, 1.699])
def test_frame_before_previous_hid_delivery_is_not_host_observation(tmp_path, delay):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        device.last_input_at = screen.updated - delay
        try:
            with pytest.raises(RuntimeError):
                await phone.click_observed(**evidence(phone, screen))
            assert link.written == []
        finally:
            device.close()

    asyncio.run(scenario())


def test_unknown_click_ack_is_not_retried_and_invalidates_input_sequence(tmp_path):
    async def scenario():
        phone, device, screen, _ = setup(tmp_path)

        class LostAck(Link):
            def write(self, data):
                self.written.append(data)
                raise OSError("unknown delivered input")

        link = LostAck()
        device.attach(link)
        device.session_started_at = time.time() - 2
        phone._clock_link = device.links
        original = evidence(phone, screen)
        try:
            with pytest.raises(DeviceError):
                await phone.click_observed(**original)
            assert link.written == [b"1 click\n"]
            assert device.input_sequence == original["expected_input_seq"] + 1
            assert device.last_input_at > 0
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["generation", "link", "abort", "input"])
def test_click_ack_then_session_change_never_sends_a_second_click(tmp_path, transition):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)

        async def change_after(*_args, **_kwargs):
            if transition == "generation":
                screen._on_ready()
                screen._on_jpeg(jpeg())
            elif transition == "link":
                device.links += 1
            elif transition == "abort":
                device._abort_epoch += 1
            else:
                device.input_sequence += 1
            return False, False

        phone._tap_effect = change_after
        try:
            with pytest.raises(RuntimeError):
                await phone.click_observed(**evidence(phone, screen))
            assert link.written == [b"1 click\n"]
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["move 1 0", "release", "click", "key tab", "press", "home"])
def test_input_sequence_tracks_actual_hid_attempt_before_ack(command):
    async def scenario():
        device = Device()

        class InspectWrite(Link):
            def write(self, data):
                assert device.input_sequence == 1
                assert began <= device.last_input_at <= time.time()
                super().write(data)

        link = InspectWrite()
        device.attach(link)
        began = time.time()
        try:
            await device.send(command)
            assert device.input_sequence == 1
        finally:
            device.close()

    asyncio.run(scenario())


def test_emergency_release_counts_but_status_query_does_not():
    async def scenario():
        device, link = Device(), Link()
        device.attach(link)
        try:
            await device.send("status")
            assert device.input_sequence == 0 and device.last_input_at == 0
            device._write_release(link)
            assert device.input_sequence == 1 and device.last_input_at > 0
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("new_worker", [False, True])
def test_a_frame_from_before_attach_cannot_authorize_the_new_device_session(tmp_path, new_worker):
    async def scenario():
        phone, device, screen, _ = setup(tmp_path)
        original_frame = snapshot(screen)
        if new_worker:
            device.close()
            device = Device()
            phone.device = device
        link = Link()
        device.attach(link)
        phone._clock_link = device.links
        phone._tap_effect = no_effect
        try:
            request = evidence(phone, screen)
            assert request["observation"] == original_frame
            with pytest.raises(RuntimeError, match="镜像延迟"):
                await phone.click_observed(**request)
            assert link.written == []
        finally:
            device.close()

    asyncio.run(scenario())


def test_post_attach_observation_after_delivery_allowance_can_click(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)

    async def scenario():
        device, screen, link = Device(), Screen(), Link()
        device.attach(link)
        clock.advance(1.71)
        screen._on_jpeg(jpeg())
        phone = Phone(device, tmp_path / "cal.json", screen)
        phone._clock, phone._clock_link = 0, device.links
        phone._tap_effect = no_effect
        try:
            await phone.click_observed(**evidence(phone, screen))
            assert link.written == [b"1 click\n"]
        finally:
            device.close()

    asyncio.run(scenario())


def test_late_host_arrival_does_not_upgrade_original_board_timestamp(tmp_path):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        now = time.time()
        device.last_input_at = now - 2
        # Host receipt is recent, but board receipt is only one second after the
        # prior HID. That original image is still inside the delivery window.
        screen._on_jpeg(jpeg())
        screen._snapshot = replace(screen.snapshot, board_ts=int((now - 1) * 1000) % (2**32))
        phone._tap_effect = no_effect
        try:
            with pytest.raises(RuntimeError, match="镜像延迟"):
                await phone.click_observed(**evidence(phone, screen))
            assert link.written == []
        finally:
            device.close()

    asyncio.run(scenario())
