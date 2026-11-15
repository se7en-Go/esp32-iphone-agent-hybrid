"""Only a known single Home contact can use the existing late response window."""

import asyncio
import base64
from types import SimpleNamespace

import pytest
from fake_clock import ManualClock
from test_assistive_home import frame
from test_assistive_late_frame import rig as late_rig
from test_control_input_context import Link

from hidagent.app import ControlAction
from hidagent.assistive import HomeUnconfirmed
from hidagent.control import Controls
from hidagent.device import Device, DeviceError
from hidagent.freshness import MIRROR_LAG_SECONDS, require_fresh
from hidagent.operation import operation_scope
from hidagent.phone import Phone
from hidagent.screen import Screen
from hidagent.text_target import PointerLandingError, TargetChangedError


def rig(tmp_path, monkeypatch, *, outcome="home"):
    phone, device, screen, clock, waits = late_rig(
        tmp_path, monkeypatch, outcome=outcome, delay=5.2)
    screen._started = True
    device.input_sequence, device.last_input_at = 0, 0.0
    send = device.send

    async def tracked_send(command):
        device.input_sequence += 1
        device.last_input_at = clock.time()
        await send(command)

    monkeypatch.setattr(device, "send", tracked_send)
    controls = Controls(phone, screen)
    request = ControlAction(action="home", screen_id=controls.observation()["screen_id"])
    return phone, device, screen, clock, waits, controls, request


def test_single_home_click_uses_late_settled_response_without_another_input(tmp_path, monkeypatch):
    phone, device, screen, clock, waits, controls, request = rig(tmp_path, monkeypatch)
    response, cutoffs = controls._response_frame, []

    async def record_cutoff(since, *args, **kwargs):
        cutoffs.append(since)
        return await response(since, *args, **kwargs)

    monkeypatch.setattr(controls, "_response_frame", record_cutoff)
    result = asyncio.run(controls.perform(request))
    assert result["fresh"] and result["screen_id"]
    assert result["command_acknowledged"] and result["effect_verified"]
    assert result["error"] is None
    assert base64.b64decode(result["image_base64"]) == frame(home=True)
    assert controls.consume(result["screen_id"])[0] == screen.frame
    assert device.sent[1:] == ["click"] and len(device.clicks) == 1
    assert len(waits) == 2
    # The response still waits from failure, rather than settling on the menu
    # using the much earlier click timestamp as its freshness boundary.
    assert len(cutoffs) == 1 and cutoffs[0] > phone.last_tap["completed_at"] + 6
    assert result["frame_received_at"] > cutoffs[0] + MIRROR_LAG_SECONDS
    assert clock.elapsed < 15


def test_direct_home_call_keeps_existing_failure_and_one_click(tmp_path, monkeypatch):
    phone, device, _, _, waits, _, _ = rig(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="辅助触控菜单已打开"):
        asyncio.run(phone.go_home())
    assert device.sent[1:] == ["click"] and len(waits) == 2


@pytest.mark.parametrize("outcome", ["silent", "menu", "other", "old_receipt"])
def test_known_home_contact_without_current_home_stays_unconfirmed(tmp_path, monkeypatch, outcome):
    _, device, _, _, _, controls, request = rig(tmp_path, monkeypatch, outcome=outcome)
    result = asyncio.run(controls.perform(request))
    assert result["command_acknowledged"]
    assert result["effect_verified"] is False and result["error"]
    assert device.sent[1:] == ["click"]


@pytest.mark.parametrize("transition", ["identity", "link", "stop", "generation", "disconnect", "input", "input_time"])
def test_response_home_cannot_cross_original_contact_session(tmp_path, monkeypatch, transition):
    phone, device, screen, clock, _, controls, request = rig(tmp_path, monkeypatch)
    original_response = controls._response_frame

    def change():
        if transition == "identity":
            phone.device = SimpleNamespace(**device.__dict__)
        elif transition == "link":
            device.links += 1
        elif transition == "stop":
            device.abort_epoch += 1
        elif transition == "generation":
            screen._on_ready()
        elif transition == "disconnect":
            screen._on_disconnect()
        elif transition == "input":
            device.input_sequence += 1
        else:
            device.last_input_at = clock.time()

    async def response(*args, **kwargs):
        clock.call_later(1.8, change)
        return await original_response(*args, **kwargs)

    monkeypatch.setattr(controls, "_response_frame", response)
    result = asyncio.run(controls.perform(request))
    assert result["command_acknowledged"]
    assert result["effect_verified"] is False and result["error"]
    assert device.sent[1:] == ["click"]


@pytest.mark.parametrize("error", [RuntimeError, PointerLandingError, TargetChangedError, DeviceError])
def test_generic_or_precontact_failure_cannot_be_promoted_by_later_home(tmp_path, monkeypatch, error):
    phone, device, screen, clock, _, controls, request = rig(tmp_path, monkeypatch)

    async def failed(**kwargs):
        clock.call_later(1.8, screen._on_jpeg, frame(home=True))
        raise error("not an acknowledged menu contact")

    monkeypatch.setattr(phone, "go_home", failed)
    result = asyncio.run(controls.perform(request))
    assert result["fresh"]
    assert not result["command_acknowledged"] and not result["effect_verified"]
    assert result["error"] == "not an acknowledged menu contact"
    assert device.sent == []


def test_unknown_click_ack_is_not_a_home_confirmation_proof(tmp_path, monkeypatch):
    phone, device, screen, clock, _, controls, request = rig(tmp_path, monkeypatch)
    send = device.send

    async def uncertain(command):
        if command == "click":
            device.sent.append(command)
            clock.call_later(1.8, screen._on_jpeg, frame(home=True))
            raise DeviceError("click ACK unknown")
        await send(command)

    monkeypatch.setattr(device, "send", uncertain)
    result = asyncio.run(controls.perform(request))
    assert not result["command_acknowledged"] and not result["effect_verified"]
    assert result["error"] == "click ACK unknown"
    assert device.sent.count("click") == 1 and phone.last_tap is None


def test_batch_home_does_not_opt_into_reconciliation_or_continue_after_failure(tmp_path, monkeypatch):
    _, device, _, _, _, controls, request = rig(tmp_path, monkeypatch)
    following = ControlAction(screen_id=request.screen_id, action="key", text="x")
    result = asyncio.run(controls.perform_batch(request.screen_id, [request, following]))
    assert result["fresh"] and result["error"]
    assert not result["command_acknowledged"] and not result["effect_verified"]
    assert result["completed"] == 0 and len(result["steps"]) == 1
    assert device.sent[1:] == ["click"]


def test_no_response_budget_does_not_convert_known_ack_to_verified_home(tmp_path, monkeypatch):
    _, device, _, clock, _, controls, request = rig(tmp_path, monkeypatch)
    response = controls._response_frame

    async def limited(*args, **kwargs):
        async with operation_scope(.1):
            return await response(*args, **kwargs)

    monkeypatch.setattr(controls, "_response_frame", limited)
    result = asyncio.run(controls.perform(request))
    assert result["command_acknowledged"] and result["error"]
    assert not result["fresh"] and not result["effect_verified"]
    assert result["screen_id"] is None and device.sent[1:] == ["click"]
    assert clock.elapsed < 13


def test_home_classifier_cannot_accept_source_replacement_during_classification(tmp_path, monkeypatch):
    from hidagent import assistive_vision

    _, device, screen, _, _, controls, request = rig(tmp_path, monkeypatch)
    response = controls._response_frame
    classify = assistive_vision.is_home_screen
    armed = False

    async def response_done(*args, **kwargs):
        nonlocal armed
        result = await response(*args, **kwargs)
        armed = True
        return result

    def replace(frame_bytes):
        result = classify(frame_bytes)
        if armed and result:
            screen._on_jpeg(frame())
        return result

    monkeypatch.setattr(controls, "_response_frame", response_done)
    monkeypatch.setattr(assistive_vision, "is_home_screen", replace)
    result = asyncio.run(controls.perform(request))
    assert result["command_acknowledged"] and result["error"]
    assert not result["fresh"] and not result["effect_verified"]
    assert result["screen_id"] is None and device.sent[1:] == ["click"]


def test_typed_proof_is_only_issued_after_confirmed_single_contact(tmp_path, monkeypatch):
    phone, device, _, _, _, _, _ = rig(tmp_path, monkeypatch)
    with pytest.raises(HomeUnconfirmed) as failure:
        asyncio.run(phone.go_home(report_unconfirmed=True))
    proof = failure.value
    assert proof.clicked_at == phone.last_tap["completed_at"]
    assert proof.same_contact(phone, phone.screen)
    assert device.sent[1:] == ["click"]
    device.input_sequence += 1
    assert not proof.same_contact(phone, phone.screen)


def test_input_during_tap_feedback_cannot_be_reanchored_as_the_home_contact(tmp_path, monkeypatch):
    phone, device, _, clock, _, controls, request = rig(tmp_path, monkeypatch)
    tap = phone.tap

    async def followed_by_input(*args, **kwargs):
        completed = await tap(*args, **kwargs)
        clock.advance(.01)
        device.input_sequence += 1
        device.last_input_at = clock.time()
        return completed

    monkeypatch.setattr(phone, "tap", followed_by_input)
    result = asyncio.run(controls.perform(request))
    assert result["fresh"] and result["command_acknowledged"]
    assert not result["effect_verified"] and result["error"]
    assert device.sent[1:] == ["click"]


def test_non_home_action_never_consumes_typed_home_proof(tmp_path, monkeypatch):
    phone, device, screen, clock, _, controls, request = rig(tmp_path, monkeypatch)

    async def wrong_error(*args, **kwargs):
        clock.call_later(1.8, screen._on_jpeg, frame(home=True))
        raise HomeUnconfirmed(clicked_at=clock.time(), device=device, link=device.links,
                              epoch=device.abort_epoch, generation=screen.generation,
                              sequence=1, last_input_at=clock.time())

    monkeypatch.setattr(phone, "key", wrong_error)
    request = ControlAction(screen_id=request.screen_id, action="key", text="x")
    result = asyncio.run(controls.perform(request))
    assert result["fresh"] and result["error"]
    assert not result["command_acknowledged"] and not result["effect_verified"]
    assert device.sent == []


@pytest.mark.parametrize("extra_input", [False, True])
def test_actual_device_input_between_click_ack_and_parent_resume_is_not_reanchored(
        tmp_path, monkeypatch, extra_input):
    async def scenario():
        clock = ManualClock()
        clock.install(monkeypatch)
        device, screen, extra, effect_sequences = Device(), Screen(), [], []
        screen._started = True
        screen._on_ready()
        screen._on_jpeg(frame(menu=True))

        class QueuedLink(Link):
            def write(self, data):
                super().write(data)
                if extra_input and data.split()[1] == b"click":
                    extra.append(asyncio.create_task(device.send("move 1 0")))

        link = QueuedLink()
        device.attach(link)
        phone = Phone(device, tmp_path / "calibration.json", screen)

        async def ensure(**kwargs):
            return require_fresh(screen)

        async def no_catch_up(*args):
            pass

        async def measured(x, y, **kwargs):
            phone._pointer_confirmed = True
            return x, y

        async def no_effect(*args, **kwargs):
            effect_sequences.append(device.input_sequence)
            return False, True

        async def unconfirmed(*args, **kwargs):
            await asyncio.sleep(.1)
            return None

        monkeypatch.setattr(phone, "ensure_observation", ensure)
        monkeypatch.setattr(phone, "_catch_up", no_catch_up)
        monkeypatch.setattr(phone, "point_verified", measured)
        monkeypatch.setattr(phone, "_tap_effect", no_effect)
        monkeypatch.setattr(phone.assistive_touch, "_wait", unconfirmed)
        controls = Controls(phone, screen)
        response = controls._response_frame

        async def late_home(*args, **kwargs):
            clock.call_later(1.8, screen._on_jpeg, frame(home=True))
            return await response(*args, **kwargs)

        monkeypatch.setattr(controls, "_response_frame", late_home)
        request = ControlAction(action="home", screen_id=controls.observation()["screen_id"])
        try:
            result = await controls.perform(request)
            await asyncio.gather(*extra)
            commands = [row.split()[1:] for row in link.written]
            assert commands == ([[b"click"], [b"move", b"1", b"0"]] if extra_input else [[b"click"]])
            # There is no await between tap's sent timestamp and this feedback
            # entry: the queued exchange already ran before the parent resumed.
            assert effect_sequences == [2 if extra_input else 1]
            assert device.last_input_at <= phone.last_tap["completed_at"]
            assert result["fresh"] and result["command_acknowledged"]
            assert result["effect_verified"] is not extra_input
            assert bool(result["error"]) is extra_input
        finally:
            device.close()

    asyncio.run(scenario())
