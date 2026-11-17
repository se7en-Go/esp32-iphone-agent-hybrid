"""Single actions wake a silent source while retaining their consumed original evidence."""

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest
from test_control_input_context import Link
from test_controls import jpeg
from test_host_observed_click import no_effect

from hidagent.app import ControlAction
from hidagent.control import Controls
from hidagent.device import Device
from hidagent.phone import Phone
from hidagent.screen import Screen


@pytest.fixture
def rig(tmp_path, monkeypatch):
    wall = [time.time()]
    monkeypatch.setattr(time, "time", lambda: wall[0])
    monkeypatch.setattr("hidagent.freshness.MIRROR_LAG_SECONDS", .001)
    screen, device, link = Screen(), Device(), Link()
    device.attach(link)
    screen._on_jpeg(jpeg())
    phone = Phone(device, tmp_path / "cal.json", screen)
    phone._clock, phone._clock_link = 0, device.links
    controls = Controls(phone, screen)
    request = ControlAction(action="tap", screen_id=controls.observation()["screen_id"],
                            x=.5, y=.4, target_box=(.4, .3, .6, .5))
    original = controls._observation
    send = phone._send

    async def deliver_after_wake(epoch, command, **kwargs):
        result = await send(epoch, command, **kwargs)
        if command == "move -2 0":
            def returned_source():
                wall[0] += .1
                screen._on_jpeg(screen.frame)
            # Schedule only after the real return ACK completes, then publish
            # when the caller yields after recording returned_at. A short timer
            # from link.write can fire before ACK on a coarse Windows clock.
            asyncio.get_running_loop().call_soon(returned_source)
        return result

    phone._send = deliver_after_wake

    async def positioned(x, y, **_kwargs):
        phone._pointer_confirmed = True
        return x, y

    phone.point_verified = positioned
    phone._tap_effect = no_effect
    wall[0] += 10  # a valid 30s ticket, but its still-source receipt is older than 5s
    yield SimpleNamespace(phone=phone, device=device, link=link, screen=screen, controls=controls,
                          request=request, original=original, wall=wall)
    device.close()


def commands(rig):
    return [line.split(b" ", 1)[1].strip() for line in rig.link.written]


@pytest.mark.parametrize("clock_resolution", [None, .02], ids=["host-clock", "coarse-clock"])
def test_stale_single_tap_refreshes_once_then_clicks_under_original_target(rig, monkeypatch, clock_resolution):
    seen = []
    original_resolve = rig.controls._resolve_text

    async def resolve(plan, before, observed_at):
        seen.append((plan["observation"], before))
        return await original_resolve(plan, before, observed_at)

    monkeypatch.setattr(rig.controls, "_resolve_text", resolve)
    async def scenario():
        if clock_resolution is not None:
            # Windows may dispatch timers up to one coarse monotonic tick early.
            asyncio.get_running_loop()._clock_resolution = clock_resolution
        return await rig.controls.perform(rig.request)

    result = asyncio.run(scenario())
    assert result["command_acknowledged"] is True
    assert seen == [(rig.original, rig.original.frame)]
    assert commands(rig) == [b"move 2 0", b"move -2 0", b"click"]
    assert result["screen_id"] is None  # no new click feedback was synthesized


@pytest.mark.parametrize("transition", ["links", "abort", "generation", "pixels", "expired", "invalid"])
def test_rejected_original_evidence_cannot_start_a_wake(rig, transition):
    if transition == "links":
        rig.device.links += 1
    elif transition == "abort":
        rig.device._abort_epoch += 1
    elif transition == "generation":
        rig.screen._on_ready()
        rig.screen._on_jpeg(jpeg())
    elif transition == "pixels":
        rig.screen._on_jpeg(jpeg("black"))
    elif transition == "expired":
        token = rig.controls.ticket
        rig.controls.ticket = (token[0], token[1] - 31, *token[2:])
    else:
        rig.request.screen_id = "unknown"
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(rig.controls.perform(rig.request))
    assert commands(rig) == [] and rig.controls.ticket is None


def test_invalid_action_parameters_are_checked_before_wake(rig):
    rig.request.x = 500
    with pytest.raises(ValueError, match="normalized"):
        asyncio.run(rig.controls.perform(rig.request))
    assert commands(rig) == []


def test_wake_does_not_reanchor_to_new_page(rig):
    old_write = rig.link.write

    def changed(data):
        old_write(data)
        if b"move -2 0" in data:
            rig.screen._on_jpeg(jpeg("black"))

    rig.link.write = changed
    with pytest.raises((ValueError, RuntimeError), match="changed"):
        asyncio.run(rig.controls.perform(rig.request))
    assert commands(rig) == [b"move 2 0", b"move -2 0"]
    assert rig.controls.ticket is None


@pytest.mark.parametrize("stage", ["forward", "return"])
def test_original_ticket_rechecked_at_actual_wake_write(rig, stage):
    async def scenario():
        original_send = rig.phone._send
        reached = asyncio.Event()

        async def queued(epoch, command, **kwargs):
            if command == ("move 2 0" if stage == "forward" else "move -2 0"):
                await rig.device._lock.acquire()
                reached.set()
            return await original_send(epoch, command, **kwargs)

        rig.phone._send = queued
        task = asyncio.create_task(rig.controls.perform(rig.request))
        try:
            await asyncio.wait_for(reached.wait(), 1)
            while not rig.device._lock._waiters:
                await asyncio.sleep(0)
            rig.screen._on_jpeg(jpeg("black"))
            rig.device._lock.release()
            with pytest.raises((ValueError, RuntimeError), match="changed"):
                await task
            assert commands(rig) == ([] if stage == "forward" else [b"move 2 0", b"move -2 0"])
            assert rig.controls.ticket is None
        finally:
            if rig.device._lock.locked():
                rig.device._lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_raw_host_click_and_read_only_query_do_not_get_active_refresh(rig):
    from hidagent.app import HostClickBody

    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(rig.controls.find_text(rig.request.screen_id, "Settings", 1, True, None))
    assert commands(rig) == []
    body = HostClickBody(screen_id=rig.request.screen_id, pointer_x=.5, pointer_y=.4,
                         target_box=(.4, .3, .6, .5))
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(rig.controls.click_observed(body))
    assert commands(rig) == []


@pytest.mark.parametrize("action", ["point", "home"])
@pytest.mark.parametrize("transition", ["links", "abort", "expired"])
def test_original_action_session_follows_nested_input_until_actual_write(rig, monkeypatch, action, transition):
    from hidagent import control as module

    async def respond(_before, _completed, _began, _ready, acknowledged, error, *_args):
        return {"command_acknowledged": acknowledged, "error": error}

    async def scenario():
        rig.screen._on_jpeg(jpeg())
        request = ControlAction(action=action, screen_id=rig.request.screen_id, x=.5, y=.4)

        async def nested(*_args, **_kwargs):
            async with rig.phone._interaction_lock:
                await rig.device.send("move 2 0" if action == "point" else "homescreen")
                return .5, .4

        monkeypatch.setattr(rig.phone, "point_verified" if action == "point" else "go_home", nested)
        monkeypatch.setattr(rig.controls, "_respond", respond)
        await rig.device._lock.acquire()
        task = asyncio.create_task(rig.controls.perform(request))
        try:
            async def waiting():
                while not rig.device._lock._waiters:
                    if task.done():
                        await task
                    await asyncio.sleep(0)

            await asyncio.wait_for(waiting(), 1)
            if transition == "links":
                rig.device.links += 1
            elif transition == "abort":
                rig.device._abort_epoch += 1
            else:
                monotonic = time.monotonic
                monkeypatch.setattr(module, "time", SimpleNamespace(
                    monotonic=lambda: monotonic() + 31, time=time.time, perf_counter=time.perf_counter))
            rig.device._lock.release()
            result = await task
            assert not result["command_acknowledged"]
            assert "changed" in result["error"] or "expired" in result["error"]
            assert commands(rig) == []
        finally:
            if rig.device._lock.locked():
                rig.device._lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["before_refresh", "final_validation"])
@pytest.mark.skipif(os.name != "posix", reason="private diagnostic files require POSIX permissions")
def test_admission_diagnostics_keep_exact_rejected_frame_not_later_frame(rig, tmp_path, monkeypatch, phase):
    from hidagent import screen_compare

    directory = tmp_path / "diagnostics"
    monkeypatch.setenv("HIDAGENT_POINTER_DIAGNOSTICS", str(directory))
    rejected_frame = jpeg("black")
    rig.screen._on_jpeg(rejected_frame)
    rejected = rig.screen.snapshot
    if phase == "before_refresh":
        rig.wall[0] += 10
    real_compare = screen_compare.observation_changed

    def changed(before, after, **kwargs):
        result = real_compare(before, after, **kwargs)
        rig.screen._on_jpeg(jpeg())  # source recovers after comparison, before the error is reported
        return result

    monkeypatch.setattr(screen_compare, "observation_changed", changed)
    rig.request.text = "private request body must not enter diagnostic metadata"
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(rig.controls.perform(rig.request))
    metadata_text = (directory / "action-admission-attempt.json").read_text()
    data = json.loads(metadata_text)
    event = data["events"][-1]
    assert event["rejection_phase"] == phase and event["reason"] == "layout_changed"
    assert event["original"]["generation"] == rig.original.generation
    assert event["original"]["updated"] == rig.original.updated
    assert event["current"]["updated"] == rejected.updated
    assert event["current"]["dimensions"] == [101, 201]
    assert (directory / data["frames"][0]["file"]).read_bytes() == rig.original.frame
    assert (directory / data["frames"][1]["file"]).read_bytes() == rejected_frame
    assert rig.screen.frame != rejected_frame
    assert rig.request.screen_id not in metadata_text and rig.request.text not in metadata_text
    assert commands(rig) == [] and rig.controls.ticket is None


def test_admission_diagnostics_disabled_or_unwritable_do_not_change_rejection(rig, tmp_path, monkeypatch):
    directory = tmp_path / "diagnostics"
    directory.write_text("not a directory")
    monkeypatch.setenv("HIDAGENT_POINTER_DIAGNOSTICS", str(directory))
    rig.screen._on_jpeg(jpeg("black"))
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(rig.controls.perform(rig.request))
    assert directory.read_text() == "not a directory"
    assert commands(rig) == []


def test_missing_private_file_support_does_not_change_action_rejection(rig, tmp_path, monkeypatch):
    directory = tmp_path / "diagnostics"
    monkeypatch.setenv("HIDAGENT_POINTER_DIAGNOSTICS", str(directory))
    monkeypatch.delattr(os, "fchmod", raising=False)
    rig.screen._on_jpeg(jpeg("black"))
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(rig.controls.perform(rig.request))
    assert not directory.exists()
    assert commands(rig) == [] and rig.controls.ticket is None
