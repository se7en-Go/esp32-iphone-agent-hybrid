"""A historical effect frame must never become a current continuation ticket."""

import asyncio
import base64
from types import SimpleNamespace

import pytest
from fake_clock import ManualClock
from test_controls import FakePhone, jpeg

from hidagent import feedback, freshness, screen_diff
from hidagent.app import ControlAction
from hidagent.control import Controls
from hidagent.freshness import snapshot
from hidagent.operation import operation_scope
from hidagent.screen import Screen


@pytest.fixture
def rig(monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)
    screen, phone = Screen(), FakePhone()
    screen._started = True
    screen._on_ready()
    phone.device = SimpleNamespace(links=1, abort_epoch=0, input_sequence=0)
    screen._on_jpeg(jpeg("white"))
    controls = Controls(phone, screen)
    effect, final = jpeg("black"), jpeg("red")

    async def tap(*_args, before_click=None, **_kwargs):
        if before_click:
            before_click()
        phone.calls.append(("click",))
        completed = clock.time()
        clock.advance(freshness.MIRROR_LAG_SECONDS + .1)
        screen._on_jpeg(effect)
        accepted = snapshot(screen)
        clock.advance(.01)
        screen._on_jpeg(final)
        return {"clicked": True, "screen_changed": True, "effect_observed": True,
                "effect_verified": False, "fresh": True, "completed_at": completed,
                "_after_frame": effect, "_after_snapshot": accepted}

    async def absent_feedback(*_args, **_kwargs):
        return None

    phone.tap_verified = tap
    monkeypatch.setattr(feedback, "wait_after_action", absent_feedback)
    return SimpleNamespace(clock=clock, screen=screen, phone=phone, controls=controls,
                           effect=effect, final=final)


def action(rig, **kwargs):
    return ControlAction(action="tap", x=.5, y=.5,
                         screen_id=rig.controls.observation()["screen_id"], **kwargs)


def test_superseded_effect_is_replaced_by_current_settled_evidence(rig):
    result = asyncio.run(rig.controls.perform(action(rig)))
    assert result["fresh"] and result["screen_id"]
    assert base64.b64decode(result["image_base64"]) == rig.final
    assert result["frame_received_at"] == rig.screen.updated
    assert rig.controls.consume(result["screen_id"])[0] == rig.final
    assert result["command_acknowledged"] and result["effect_observed"]
    assert result["effect_verified"] is False
    assert not any(key.startswith("_") for key in result)


def test_async_ocr_timeout_keeps_ack_but_does_not_issue_continuation_ticket(rig, monkeypatch):
    summarize = screen_diff.summarize
    ocr_calls = []

    async def expired(before, after, **kwargs):
        if kwargs.get("ocr_enabled"):
            ocr_calls.append((before, after))
            # This is distinct from built-in TimeoutError on supported Python 3.10.
            raise asyncio.TimeoutError()
        return await summarize(before, after, **kwargs)

    monkeypatch.setattr(screen_diff, "summarize", expired)
    result = asyncio.run(rig.controls.perform(action(rig, ocr_diff=True)))
    assert len(ocr_calls) == 1 and ocr_calls[0][1] == rig.final
    assert result["command_acknowledged"] and result["effect_observed"]
    assert not result["fresh"] and result["screen_id"] is None
    assert rig.controls.ticket is None and rig.phone.calls == [("click",)]


def test_page_keeps_changing_after_effect_gets_no_ticket(rig):
    original = rig.phone.tap_verified

    async def changing(*args, **kwargs):
        result = await original(*args, **kwargs)
        for i in range(1, 24):
            rig.clock.call_later(i * .1, rig.screen._on_jpeg, jpeg("blue" if i % 2 else "red"))
        return result

    rig.phone.tap_verified = changing
    result = asyncio.run(rig.controls.perform(action(rig)))
    assert result["screen_id"] is None and rig.controls.ticket is None
    assert not result["fresh"] and result["changed"] is None
    assert result["screen_diff"]["status"] == "stale"
    assert result["command_acknowledged"] and result["effect_observed"]


@pytest.mark.parametrize("transition", ["links", "epoch", "generation", "disconnect"])
def test_response_wait_cannot_reauthorize_a_changed_session(rig, transition):
    original = rig.phone.tap_verified

    def changed_session():
        if transition == "links":
            rig.phone.device.links += 1
        elif transition == "epoch":
            rig.phone.device.abort_epoch += 1
        elif transition == "generation":
            rig.screen._on_ready()
            rig.screen._on_jpeg(rig.final)
        else:
            rig.screen._on_disconnect()

    async def tap(*args, **kwargs):
        result = await original(*args, **kwargs)
        rig.clock.call_later(.1, changed_session)
        return result

    rig.phone.tap_verified = tap
    result = asyncio.run(rig.controls.perform(action(rig)))
    assert result["screen_id"] is None and rig.controls.ticket is None
    assert result["fresh"] is False and result["command_acknowledged"] is True
    assert result["effect_verified"] is False


def test_response_does_not_issue_before_async_diff_finishes(rig, monkeypatch):
    summarize = screen_diff.summarize
    calls = 0

    async def changing_diff(before, after, **kwargs):
        nonlocal calls
        result = await summarize(before, after, **kwargs)
        if kwargs["fresh"]:
            calls += 1
            rig.clock.advance(.01)
            rig.screen._on_jpeg(jpeg("blue" if calls % 2 else "red"))
        return result

    monkeypatch.setattr(screen_diff, "summarize", changing_diff)
    result = asyncio.run(rig.controls.perform(action(rig)))
    assert result["screen_id"] is None and rig.controls.ticket is None
    assert result["fresh"] is False
    assert result["screen_diff"]["status"] == "stale"
    assert result["command_acknowledged"] and result["effect_observed"]
    # All restabilization attempts share the original 2s response window.
    assert rig.clock.elapsed <= 1.81 + 2.0 + .02


@pytest.mark.parametrize("phase", ["diff", "ticket"])
def test_last_moment_source_replacement_is_restabilized_within_original_window(rig, monkeypatch, phase):
    replacement = jpeg("blue")
    changed = False
    seen = []
    summarize = screen_diff.summarize
    observation = rig.controls.observation

    def replace_once():
        nonlocal changed
        if not changed:
            changed = True
            rig.clock.advance(.01)
            rig.screen._on_jpeg(replacement)

    async def diff(before, after, **kwargs):
        result = await summarize(before, after, **kwargs)
        if kwargs["fresh"]:
            seen.append(after)
            if phase == "diff":
                replace_once()
        return result

    def issuing(accepted=None):
        result = observation(accepted)
        if phase == "ticket" and accepted is not None:
            replace_once()
        return result

    monkeypatch.setattr(screen_diff, "summarize", diff)
    monkeypatch.setattr(rig.controls, "observation", issuing)
    result = asyncio.run(rig.controls.perform(action(rig)))
    assert result["fresh"] and result["screen_id"]
    assert base64.b64decode(result["image_base64"]) == replacement
    assert seen == [rig.final, replacement]  # recompute, never borrow the first frame's diff
    assert rig.controls.consume(result["screen_id"])[0] == replacement
    assert .7 <= result["timing_ms"]["wait_settled"] / 1000 <= 2
    assert rig.phone.calls == [("click",)]
    assert result["effect_observed"] and result["effect_verified"] is False


@pytest.mark.parametrize("transition", ["links", "epoch", "generation", "dimensions"])
def test_final_source_retry_cannot_cross_session_or_dimensions(rig, monkeypatch, transition):
    summarize = screen_diff.summarize
    calls = 0

    async def diff(before, after, **kwargs):
        nonlocal calls
        result = await summarize(before, after, **kwargs)
        if kwargs["fresh"]:
            calls += 1
            if transition == "links":
                rig.phone.device.links += 1
            elif transition == "epoch":
                rig.phone.device.abort_epoch += 1
            elif transition == "generation":
                rig.screen._on_ready()
                rig.screen._on_jpeg(rig.final)
            else:
                import io

                from PIL import Image

                encoded = io.BytesIO()
                Image.new("RGB", (80, 120), "red").save(encoded, "JPEG")
                rig.screen._on_jpeg(encoded.getvalue())
        return result

    monkeypatch.setattr(screen_diff, "summarize", diff)
    result = asyncio.run(rig.controls.perform(action(rig)))
    assert result["screen_id"] is None and rig.controls.ticket is None
    assert not result["fresh"] and calls == 1
    assert result["command_acknowledged"] and rig.phone.calls == [("click",)]


def test_too_little_original_operation_budget_returns_no_ticket(rig):
    async def run():
        request = action(rig)
        # The action consumes 1.81s; the original operation has <.35s left.
        async with operation_scope(2):
            return await rig.controls.perform(request)

    result = asyncio.run(run())
    assert result["screen_id"] is None and rig.controls.ticket is None
    assert result["fresh"] is False and result["command_acknowledged"]
    assert rig.phone.calls == [("click",)]
    assert rig.clock.elapsed < 2


def test_retry_does_not_reset_the_original_operation_budget(rig, monkeypatch):
    summarize = screen_diff.summarize

    async def replaced(before, after, **kwargs):
        result = await summarize(before, after, **kwargs)
        if kwargs["fresh"]:
            rig.clock.advance(.01)
            rig.screen._on_jpeg(jpeg("blue"))
        return result

    monkeypatch.setattr(screen_diff, "summarize", replaced)

    async def run():
        request = action(rig)
        # Enough time for the first quiet interval, but not a second one.
        async with operation_scope(2.5):
            return await rig.controls.perform(request)

    result = asyncio.run(run())
    assert result["screen_id"] is None and rig.controls.ticket is None
    assert result["fresh"] is False and result["command_acknowledged"]
    assert rig.phone.calls == [("click",)]
    assert rig.clock.elapsed < 2.5


def test_later_current_frame_cannot_rewrite_the_observed_action_effect(rig):
    original = rig.phone.tap_verified

    async def no_initial_effect(*args, **kwargs):
        result = await original(*args, **kwargs)
        result["screen_changed"] = result["effect_observed"] = False
        return result

    rig.phone.tap_verified = no_initial_effect
    result = asyncio.run(rig.controls.perform(action(rig)))
    assert result["fresh"] and result["changed"]  # current frame compared with original input image
    assert result["screen_changed"] is False and result["effect_observed"] is False
    assert result["effect_verified"] is False


@pytest.mark.parametrize("batch", [False, True])
def test_original_action_session_is_not_recaptured_after_execution(rig, batch):
    original = rig.phone.tap_verified

    async def reconnect(*args, **kwargs):
        result = await original(*args, **kwargs)
        rig.phone.device.links += 1
        return result

    rig.phone.tap_verified = reconnect
    request = action(rig)
    call = (rig.controls.perform_batch(request.screen_id, [request]) if batch
            else rig.controls.perform(request))
    result = asyncio.run(call)
    assert result["command_acknowledged"] and result["effect_observed"]
    assert result["screen_id"] is None and rig.controls.ticket is None


def test_raw_action_response_also_requires_a_quiet_current_source(rig):
    completed = rig.clock.time()
    rig.clock.advance(freshness.MIRROR_LAG_SECONDS + .1)
    rig.screen._on_jpeg(rig.effect)
    rig.clock.call_later(.1, rig.screen._on_jpeg, rig.final)
    began = rig.clock.monotonic()
    result = asyncio.run(rig.controls._respond(jpeg(), completed, began, began, True, None, {}))
    assert rig.clock.monotonic() - began >= .45
    assert result["fresh"] and base64.b64decode(result["image_base64"]) == rig.final
    assert rig.controls.consume(result["screen_id"])[0] == rig.final


def test_batch_returns_current_frame_without_continuing_unverified_effect(rig):
    request = action(rig)
    following = ControlAction(action="point", x=.2, y=.3, screen_id=request.screen_id)
    result = asyncio.run(rig.controls.perform_batch(request.screen_id, [request, following]))
    assert result["completed"] == 1 and result["stopped_before_step"] == 2
    assert rig.phone.calls == [("click",)]
    assert base64.b64decode(result["image_base64"]) == rig.final
    assert rig.controls.consume(result["screen_id"])[0] == rig.final
    assert not result["effect_verified"]


def test_same_pixels_with_new_receipt_during_diff_use_the_current_snapshot(rig, monkeypatch):
    summarize = screen_diff.summarize

    async def newer_receipt(before, after, **kwargs):
        result = await summarize(before, after, **kwargs)
        rig.clock.advance(.01)
        rig.screen._on_jpeg(rig.final)
        return result

    monkeypatch.setattr(screen_diff, "summarize", newer_receipt)
    result = asyncio.run(rig.controls.perform(action(rig)))
    assert result["fresh"] and result["frame_received_at"] == rig.screen.updated
    assert rig.controls._observation.updated == rig.screen.updated
    assert rig.controls.consume(result["screen_id"])[0] == rig.final


def test_response_cancellation_leaves_no_ticket_and_sends_no_extra_input(rig):
    original = rig.phone.tap_verified

    async def cancel_while_waiting(*args, **kwargs):
        result = await original(*args, **kwargs)
        rig.clock.call_later(.1, asyncio.current_task().cancel)
        return result

    rig.phone.tap_verified = cancel_while_waiting
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rig.controls.perform(action(rig)))
    assert rig.controls.ticket is None and rig.phone.calls == [("click",)]


def test_effect_evidence_does_not_authorize_a_current_frame_before_pipeline_boundary(rig):
    original = rig.phone.tap_verified

    async def premature(*args, **kwargs):
        result = await original(*args, **kwargs)
        # Deliberately inconsistent effect metadata must not replace the shared
        # causal policy: current video is only 1s after the completed input.
        result["completed_at"] = rig.clock.time() - 1
        return result

    rig.phone.tap_verified = premature
    result = asyncio.run(rig.controls.perform(action(rig)))
    assert result["command_acknowledged"] and result["effect_observed"]
    assert result["fresh"] is False and result["screen_id"] is None
