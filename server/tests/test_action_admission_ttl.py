"""Ticket TTL admits the first physical input; it does not restart an operation budget."""

import asyncio
import time
from types import SimpleNamespace

import pytest
import test_action_admission_refresh as admission_tests
from test_controls import jpeg

from hidagent import control, ocr
from hidagent.app import ControlAction
from hidagent.operation import OperationDeadline, operation_scope

rig = admission_tests.rig
commands = admission_tests.commands


@pytest.fixture
def clock(rig, monkeypatch):
    now = [rig.controls.ticket[1] + 29]
    monkeypatch.setattr(control, "time", SimpleNamespace(
        monotonic=lambda: now[0], time=time.time, perf_counter=time.perf_counter))
    return now


async def response(_before, _completed, _began, _ready, acknowledged, error, *_args):
    return {"command_acknowledged": acknowledged, "error": error}


@pytest.mark.parametrize("action", ["tap", "tap_text"])
def test_refresh_first_write_at_29_allows_completion_after_31(rig, clock, monkeypatch, action):
    write = rig.link.write

    def first_write(data):
        write(data)
        if b"move 2 0" in data:
            clock[0] += 2

    monkeypatch.setattr(rig.link, "write", first_write)
    if action == "tap_text":
        async def found(*_args):
            return [ocr.TextBox("Settings", (.4, .3, .6, .5), (.5, .4), .98)]
        monkeypatch.setattr(ocr, "find_matches_async", found)
        rig.request = ControlAction(action=action, screen_id=rig.request.screen_id, text="Settings", exact=True)
    result = asyncio.run(rig.controls.perform(rig.request))
    assert result["command_acknowledged"]
    assert commands(rig) == [b"move 2 0", b"move -2 0", b"click"]
    assert rig.controls.ticket is None


@pytest.mark.parametrize("transition", [None, "links", "abort", "generation", "target"])
def test_first_pointer_write_does_not_drop_original_guards_after_ticket_ttl(rig, clock, monkeypatch, transition):
    rig.screen._on_jpeg(jpeg())  # no wake; the measured point is the first real HID

    async def point(x, y, **_kwargs):
        await rig.device.send("move 1 0")
        clock[0] += 2
        if transition == "links":
            rig.device.links += 1
        elif transition == "abort":
            rig.device._abort_epoch += 1
        elif transition == "generation":
            rig.screen._on_ready()
        rig.screen._on_jpeg(jpeg("black" if transition == "target" else "white"))
        rig.phone._pointer_confirmed = True
        return x, y

    monkeypatch.setattr(rig.phone, "point_verified", point)
    result = asyncio.run(rig.controls.perform(rig.request))
    assert result["command_acknowledged"] is (transition is None)
    assert commands(rig) == ([b"move 1 0", b"click"] if transition is None else [b"move 1 0"])


def test_existing_45_second_operation_is_not_restarted_after_first_input(rig, clock, monkeypatch):
    rig.screen._on_jpeg(jpeg())
    monkeypatch.setattr(rig.controls, "_respond", response)

    async def scenario():
        with pytest.raises(OperationDeadline):
            async with operation_scope(45) as operation:
                deadline = operation.deadline

                async def home(**_kwargs):
                    await rig.device.send("homescreen")
                    clock[0] += 2
                    assert operation.deadline == deadline
                    operation.deadline = time.monotonic() - .01
                    await rig.device.send("key tab")

                monkeypatch.setattr(rig.phone, "go_home", home)
                result = await rig.controls.perform(ControlAction(action="home", screen_id=rig.request.screen_id))
                assert not result["command_acknowledged"] and "deadline" in result["error"]
        assert commands(rig) == [b"homescreen"]

    asyncio.run(scenario())


def test_ocr_expiry_before_first_input_still_sends_nothing(rig, clock, monkeypatch):
    rig.screen._on_jpeg(jpeg())

    async def found(*_args):
        clock[0] += 2
        return [ocr.TextBox("Settings", (.4, .3, .6, .5), (.5, .4), .98)]

    monkeypatch.setattr(ocr, "find_matches_async", found)
    request = ControlAction(action="tap_text", screen_id=rig.request.screen_id, text="Settings", exact=True)
    with pytest.raises((ValueError, RuntimeError), match="expired"):
        asyncio.run(rig.controls.perform(request))
    assert commands(rig) == [] and rig.controls.ticket is None


@pytest.mark.parametrize("earlier", ["background", "release", "ping"])
def test_unrelated_or_cleanup_input_does_not_admit_an_expired_action(rig, clock, monkeypatch, earlier):
    rig.screen._on_jpeg(jpeg())
    monkeypatch.setattr(rig.controls, "_respond", response)

    async def scenario():
        if earlier == "background":
            await rig.device.send("move 1 0")
        async def home(**_kwargs):
            if earlier != "background":
                await rig.device.send(earlier)
            clock[0] += 2
            await rig.device.send("homescreen")

        monkeypatch.setattr(rig.phone, "go_home", home)
        result = await rig.controls.perform(ControlAction(action="home", screen_id=rig.request.screen_id))
        assert not result["command_acknowledged"] and "expired" in result["error"]
        assert commands(rig) == [b"move 1 0" if earlier == "background" else earlier.encode()]

    asyncio.run(scenario())
