"""A measured landing can follow observations only across the same final HID move."""

import asyncio
import time
from copy import copy
from dataclasses import replace

import pytest
from fake_clock import ManualClock
from test_bubble_pointer_localization import _bubble_frame
from test_host_observed_click import setup

from hidagent import freshness
from hidagent.app import ControlAction
from hidagent.control import Controls
from hidagent.device import DeviceError
from hidagent.text_target import PointerLandingError


@pytest.mark.parametrize("background", ["idle", "explicit"])
def test_cold_point_blocks_only_idle_input_between_home_and_move(tmp_path, monkeypatch, background):
    async def scenario():
        phone, device, _, link = setup(tmp_path)
        phone.span = {"w": 500, "h": 1080}
        homed, finished = asyncio.Event(), asyncio.Event()
        send = device.send

        async def pause_after_home(command):
            await send(command)
            if command == "home":
                homed.set()
                await finished.wait()

        async def interleave():
            await homed.wait()
            try:
                if background == "idle":
                    with pytest.raises(DeviceError, match="paused"):
                        await device._nudge_idle()
                else:
                    await send("move 1 0")
            finally:
                finished.set()

        async def verify(x, y, epoch, observed, same_session, **kwargs):
            same_session()
            return x, y

        monkeypatch.setattr(device, "send", pause_after_home)
        monkeypatch.setattr(phone, "_confirm_point_landing", verify)
        task = asyncio.create_task(interleave())
        try:
            point = phone.point_verified(.104, .467, measure_only=True, rehome=True)
            if background == "idle":
                assert await point == (.104, .467)
                final_move = b"move 52 504"
            else:
                with pytest.raises(PointerLandingError, match="sequence=2/3"):
                    await point
                final_move = b"move 1 0"
            await task
            assert [line.split(b" ", 1)[1].strip() for line in link.written] == [b"release", b"home", final_move]
            assert device._host_observation_lease is None
            await device._nudge_idle()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_measurement_waits_for_the_outstanding_idle_return(tmp_path, monkeypatch, cancel):
    async def scenario():
        phone, device, _, link = setup(tmp_path)
        forward, allow_return = asyncio.Event(), asyncio.Event()
        guarded = device.send_guarded

        async def delayed(command, before_write, **kwargs):
            if command == "move -1 0":
                forward.set()
                await allow_return.wait()
            await guarded(command, before_write, **kwargs)

        async def position(*args):
            assert device.input_sequence == 2 and not device._idle_nudge_inflight

        async def verify(x, y, *args, **kwargs):
            return x, y

        monkeypatch.setattr(device, "send_guarded", delayed)
        monkeypatch.setattr(phone, "point", position)
        monkeypatch.setattr(phone, "_confirm_point_landing", verify)
        nudge = asyncio.create_task(device._nudge_idle())
        try:
            await forward.wait()
            point = asyncio.create_task(phone.point_verified(.5, .5, measure_only=True))
            await asyncio.sleep(0)
            assert device._idle_nudge_paused() and not point.done()
            if cancel:
                point.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await point
            allow_return.set()
            await nudge
            if not cancel:
                assert await point == (.5, .5)
            assert device._host_observation_lease is None
            assert [line.split(b" ", 1)[1].strip() for line in link.written] == [b"move 1 0", b"move -1 0"]
        finally:
            allow_return.set()
            await asyncio.gather(nudge, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 0)
    phone, device, screen, link = setup(tmp_path)
    controls = Controls(phone, screen)
    return clock, phone, device, screen, link, controls


async def measure(rig, monkeypatch, *, intervene=False, unseen=False, settled=True, actual=(.516, .5)):
    clock, phone, device, screen, link, controls = rig
    start, aim = (.3, .45), (.5, .5)
    original, landed = _bubble_frame(pointer=start), _bubble_frame(pointer=actual)
    screen._on_jpeg(original)
    phone._pace = 4
    phone.span = {"w": 500, "h": 1080}
    phone._seen = {"x": [], "y": []}
    phone._save = lambda: pytest.fail("a measured point cannot write calibration")

    async def move(x, y):
        phone._position_before, phone._position_from = original, start
        await phone._send(device.abort_epoch, "move 20 0")
        phone._position_completed = time.time()
        clock.advance(.01)
        screen._on_jpeg(landed)

    async def find(near, *, epoch, **kwargs):
        found = await phone._observe_landing(near, epoch)
        assert found == pytest.approx(actual, abs=.003)
        if intervene:
            await device.send("move 1 0")
        if unseen:
            # Only the first leg was measured. The final write has no observation.
            await phone._send(epoch, "move 1 0")
            return None
        return found

    async def response(completed_at, *args, **kwargs):
        measured = phone._point_measurement
        if settled and measured is not None and measured.current_session(phone):
            assert completed_at == measured.last_input_at
        if settled:
            clock.advance(.02)
            screen._on_jpeg(landed)
        return freshness.snapshot(screen), time.perf_counter(), time.monotonic() + 2

    monkeypatch.setattr(phone, "point", move)
    monkeypatch.setattr(phone, "_find_pointer", find)
    monkeypatch.setattr(controls, "_response_frame", response)
    monkeypatch.setattr(phone, "_localize_pointer", no_landing)
    result = await controls.perform(ControlAction(action="point", x=aim[0], y=aim[1], measure_only=True,
                                                  target_box=(.47, .48, .53, .52),
                                                  screen_id=controls.observation()["screen_id"]))
    assert link.written
    return result, landed


async def no_landing(*args, **kwargs):
    return None


def test_real_off_target_measurement_survives_only_independent_same_input_observations(rig, monkeypatch):
    async def scenario():
        clock, _, _, screen, _, controls = rig
        moved, landed = await measure(rig, monkeypatch)
        assert moved["pointer"] == {"x": .5, "y": .5}  # existing target echo
        assert moved["pointer_seen"] is True
        assert moved["measured_pointer"] == pytest.approx({"x": .516, "y": .5}, abs=.003)
        evidence_id = moved["pointer_measurement_id"]
        clock.advance(.1)
        screen._on_jpeg(landed)
        for shot in [controls.observation(), await controls.observe_for_click()]:
            assert shot["frame_received_at"] > moved["frame_received_at"]
            assert shot["pointer_measurement_id"] == evidence_id
            assert shot["measured_pointer"] == moved["measured_pointer"]

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["input", "same_values_new_device", "generation", "epoch", "link",
                                    "native_pointer_pixels", "expired", "old_capture"])
def test_current_observation_drops_invalid_measurement(rig, monkeypatch, change):
    async def scenario():
        clock, phone, device, screen, _, controls = rig
        moved, landed = await measure(rig, monkeypatch)
        assert moved["pointer_measurement_id"]
        clock.advance(.1)
        if change == "input":
            await device.send("move 1 0")
        elif change == "same_values_new_device":
            phone.device = copy(device)
        elif change == "generation":
            screen.generation += 1
        elif change == "epoch":
            device.request_abort()
        elif change == "link":
            device.links += 1
            phone._clock_link = device.links
        elif change == "native_pointer_pixels":
            landed = _bubble_frame(pointer=(.526, .5))
        elif change == "expired":
            clock.advance(30)
        screen._on_jpeg(landed)
        if change == "old_capture":
            original = freshness.snapshot(screen)
            monkeypatch.setattr(freshness, "snapshot", lambda _: replace(original, capture_age_s=1.))
        assert not controls.observation().get("pointer_measurement_id")

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["intervening_write", "unseen_final_write"])
def test_evidence_from_before_a_later_write_cannot_escape_point(rig, monkeypatch, kind):
    async def scenario():
        result, _ = await measure(rig, monkeypatch, intervene=kind == "intervening_write",
                                  unseen=kind == "unseen_final_write")
        assert not result.get("pointer_measurement_id")
        assert result["measured_pointer"] is None
        assert result["pointer_seen"] is False

    asyncio.run(scenario())


def test_cached_source_alone_does_not_prove_a_measurement(rig, monkeypatch):
    async def scenario():
        result, _ = await measure(rig, monkeypatch, settled=False)
        assert result.get("pointer_measurement_id") is None
        assert result["measured_pointer"] is None and not result["pointer_seen"]

    asyncio.run(scenario())


def correction(moved, token):
    return ControlAction(action="point", measure_only=True, correct_from_measurement_id=moved["pointer_measurement_id"],
                         x=.5, y=.5, target_box=(.47, .48, .53, .52), screen_id=token)


@pytest.mark.parametrize("cap", [False, True])
def test_relative_correction_uses_measured_delta_and_remeasures_without_rehoming(rig, monkeypatch, cap):
    async def scenario():
        clock, phone, _, screen, link, controls = rig
        actual = (.5, .553) if cap else (.529, .5)
        moved, landed = await measure(rig, monkeypatch, actual=actual)
        clock.advance(.02)
        screen._on_jpeg(landed)
        fresh = controls.observation()
        assert fresh["pointer_measurement_id"] == moved["pointer_measurement_id"]
        final_xy = (.5, .517) if cap else (.503, .5)
        final = _bubble_frame(pointer=final_xy)

        async def observe(near, *, epoch, **kwargs):
            clock.advance(.02)
            screen._on_jpeg(final)
            return await phone._observe_landing(near, epoch)

        async def response(*args, **kwargs):
            clock.advance(.02)
            screen._on_jpeg(final)
            return freshness.snapshot(screen), time.perf_counter(), time.monotonic() + 2

        monkeypatch.setattr(phone, "_find_pointer", observe)
        monkeypatch.setattr(phone, "point", lambda *_: pytest.fail("no absolute move or rehome during correction"))
        monkeypatch.setattr(controls, "_response_frame", response)
        wires_before = len(link.written)
        corrected = await controls.perform(correction(moved, fresh["screen_id"]))
        sent = [line.split(b" ", 1)[1].strip() for line in link.written[wires_before:]]
        if cap:
            assert sent == [b"move 0 -40"]
        else:
            assert len(sent) == 1
            _, dx, dy = sent[0].split()
            assert -16 <= int(dx) <= -13 and int(dy) == 0
        assert corrected["measured_pointer"] == pytest.approx({"x": final_xy[0], "y": final_xy[1]}, abs=.003)
        assert corrected["pointer_measurement_id"] != moved["pointer_measurement_id"]
        assert phone._pos is None

    asyncio.run(scenario())


def test_relative_correction_rejects_reused_frame_and_competing_input_at_write(rig, monkeypatch):
    async def scenario():
        clock, _, device, screen, link, controls = rig
        moved, landed = await measure(rig, monkeypatch)
        writes = len(link.written)
        with pytest.raises(ValueError, match="independent new observation"):
            await controls.perform(correction(moved, moved["screen_id"]))
        clock.advance(.02)
        screen._on_jpeg(landed)
        ticket = controls.observation()["screen_id"]
        guarded = device.send_guarded

        async def race(command, guard, **kwargs):
            device._mark_input_attempt()  # competing input while this command is awaiting the write lock
            return await guarded(command, guard, **kwargs)

        monkeypatch.setattr(device, "send_guarded", race)
        result = await controls.perform(correction(moved, ticket))
        assert result["error"] and not result["command_acknowledged"]
        assert result.get("pointer_measurement_id") is None
        assert len(link.written) == writes

    asyncio.run(scenario())


def test_invalid_measurement_requests_cannot_reach_hid(rig):
    _, _, _, _, link, controls = rig
    for options in [
        {"action": "tap", "measure_only": True}, {"correct_from_measurement_id": "previous"},
        {"correct_from_measurement_id": "previous", "measure_only": True},
        {"correct_from_measurement_id": "previous", "measure_only": True, "rehome": True,
         "target_box": (.4, .4, .6, .6)},
    ]:
        settings = {"action": "point", "x": .5, "y": .5, **options,
                    "screen_id": controls.observation()["screen_id"]}
        with pytest.raises(ValueError):
            asyncio.run(controls.perform(ControlAction(**settings)))
    token = controls.observation()["screen_id"]
    with pytest.raises(ValueError, match="outside a batch"):
        asyncio.run(controls.perform_batch(
            token, [ControlAction(action="point", measure_only=True, x=.5, y=.5, screen_id=token)]))
    assert not link.written


@pytest.mark.parametrize("outward,intervene", [("stale", False), ("causal", True), ("causal", False)])
def test_probe_measurement_requires_own_causal_outward_and_return(rig, monkeypatch, outward, intervene):
    from hidagent import feedback, vision

    async def scenario():
        clock, phone, device, screen, link, controls = rig
        phone._pace, phone.span = 4, {"w": 500, "h": 1080}
        monkeypatch.setattr(feedback, "MIRROR_LAG_SECONDS", .05)
        monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", .05)
        near, away = (.5, .5), (.55, .5)
        initial = _bubble_frame(pointer=(.3, .5))
        returned, outward_frame = _bubble_frame(pointer=near), _bubble_frame(pointer=away)
        screen._on_jpeg(initial)
        write = link.write

        def publish_outward():
            if intervene:
                device._mark_input_attempt()
            screen._on_jpeg(outward_frame)

        def write_probe(data):
            write(data)
            if data.endswith(b"move 25 0\n"):
                clock.call_later(.01 if outward == "stale" else .09, publish_outward)
            if data.endswith(b"move -25 0\n"):
                clock.call_later(.09, screen._on_jpeg, returned)

        async def initial_move(x, y):
            await phone._send(device.abort_epoch, "move 9 0")

        async def paired(near, *, epoch, **kwargs):
            return await phone._localize_pointer(near, epoch, probe=(0, 25))

        def pair(first, last, *args, **kwargs):
            return (away, near) if (first, last) == (outward_frame, returned) else None

        async def response(*args, **kwargs):
            clock.advance(.1)
            screen._on_jpeg(returned)
            return freshness.snapshot(screen), time.perf_counter(), time.monotonic() + 2

        monkeypatch.setattr(phone, "_extend_escape_probe", lambda *_: None)
        monkeypatch.setattr(link, "write", write_probe)
        monkeypatch.setattr(phone, "point", initial_move)
        monkeypatch.setattr(phone, "_find_pointer", paired)
        monkeypatch.setattr(vision, "pointer_move", pair)
        monkeypatch.setattr(controls, "_response_frame", response)
        moved = await controls.perform(ControlAction(action="point", x=.5, y=.5, measure_only=True,
                                                     screen_id=controls.observation()["screen_id"]))
        assert bool(moved.get("pointer_measurement_id")) is (outward == "causal" and not intervene)

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["cleared_after_prepare", "later_scope_cannot_expand", "pointer_edited"])
def test_pointer_proof_keeps_its_original_caret_scope_across_prepare(rig, monkeypatch, case):
    from test_wechat_send_caret import FIELD, OUTSIDE_FIELD, SEND_POINTER, edited, load, seed_history

    async def scenario():
        clock, phone, device, screen, _, controls = rig
        before = load("send_point_on")
        screen._on_jpeg(before)
        seed_history(screen)

        async def move(x, y):
            await phone._send(device.abort_epoch, "move 12 0")

        async def visual_measurement(near, **kwargs):
            clock.advance(.05)
            screen._on_jpeg(before)
            phone._record_pointer_measurement(SEND_POINTER, freshness.snapshot(screen))
            return SEND_POINTER

        async def response(*args, **kwargs):
            clock.advance(.05)
            screen._on_jpeg(before)
            return freshness.snapshot(screen), time.perf_counter(), time.monotonic() + 2

        monkeypatch.setattr(phone, "point", move)
        monkeypatch.setattr(phone, "_find_pointer", visual_measurement)
        monkeypatch.setattr(controls, "_response_frame", response)
        scope = OUTSIDE_FIELD if case == "later_scope_cannot_expand" else FIELD
        moved = await controls.perform(ControlAction(
            action="point", x=SEND_POINTER[0], y=SEND_POINTER[1], measure_only=True,
            caret_scope=scope, screen_id=controls.observation()["screen_id"]))
        assert moved["pointer_measurement_id"]
        controls.clear_verified_caret_scope()
        if case == "later_scope_cannot_expand":
            controls.remember_verified_caret_scope(FIELD, device=device, links=device.links,
                                                   epoch=device.abort_epoch, generation=screen.generation)
        clock.advance(.05)
        current = (edited("send_forclick_mid", lambda draw: draw.rectangle((435, 922, 447, 938), fill="red"))
                   if case == "pointer_edited" else load("send_forclick_mid"))
        screen._on_jpeg(current)
        seed_history(screen)
        observed = await controls.observe_for_click()
        assert (observed.get("pointer_measurement_id") == moved["pointer_measurement_id"]) == (
            case == "cleared_after_prepare")

    asyncio.run(scenario())
