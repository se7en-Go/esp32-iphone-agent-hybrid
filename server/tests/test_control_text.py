import asyncio
import base64
from dataclasses import asdict
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from test_controls import FakePhone, Screen, instant_mirror, jpeg  # noqa: F401

from hidagent import ocr, screen_diff
from hidagent.app import ControlAction, FindTextBody
from hidagent.control import Controls


@pytest.fixture
def matches(monkeypatch):
    found = [ocr.TextBox("设置", (.1, .2, .5, .3), (.3, .25), .98),
             ocr.TextBox("设置", (.1, .6, .5, .7), (.3, .65), .95)]
    monkeypatch.setattr(ocr, "find_matches", lambda *_args: found)
    monkeypatch.setattr(ocr, "available", lambda: False)
    return found


def test_text_lookup_keeps_ticket_then_text_tap_uses_verified_phone_path(matches):
    phone, screen = FakePhone(), Screen()
    controls = Controls(phone, screen)
    token = controls.observation()["screen_id"]
    result = asyncio.run(controls.find_text(token, "设置", nth=2, exact=True))
    assert result["match"] == asdict(matches[1])
    assert result["matches"] == [asdict(match) for match in matches]
    assert controls.ticket[0] == token and not phone.calls
    response = asyncio.run(controls.perform(ControlAction(
        action="tap_text", screen_id=token, text="设置", nth=2, exact=True)))
    assert phone.calls == [("tap", .3, .65)]
    assert response["text_match"] == asdict(matches[1])
    assert response["screen_id"] != token
    assert response["screen_diff"]["changed_fraction"] == 0
    assert response["effect_verified"] is False


@pytest.mark.parametrize("text,nth", [("missing", 3), ("　 \t", 1)])
def test_missing_or_empty_text_never_sends_hid(matches, text, nth):
    phone = FakePhone()
    controls = Controls(phone, Screen())
    with pytest.raises(ValueError):
        asyncio.run(controls.perform(ControlAction(
            action="tap_text", screen_id=controls.observation()["screen_id"], text=text, nth=nth)))
    assert not phone.calls and controls.ticket is None


@pytest.mark.parametrize("nth", [0, -1, 1.5, True])
def test_text_index_is_a_positive_integer(nth):
    with pytest.raises(ValidationError):
        ControlAction(action="tap_text", screen_id="token", text="设置", nth=nth)
    with pytest.raises(ValidationError):
        FindTextBody(screen_id="token", text="设置", nth=nth)


@pytest.mark.parametrize("lookup", [True, False])
def test_screen_change_during_ocr_rejects_lookup_or_tap(monkeypatch, matches, lookup):
    phone, screen = FakePhone(), Screen()
    controls = Controls(phone, screen)
    token = controls.observation()["screen_id"]

    def changed(*_args):
        screen.frame = jpeg("black")
        return matches

    monkeypatch.setattr(ocr, "find_matches", changed)
    with pytest.raises(ValueError):
        if lookup:
            asyncio.run(controls.find_text(token, "设置"))
        else:
            asyncio.run(controls.perform(ControlAction(action="tap_text", screen_id=token, text="设置")))
    assert not phone.calls


def test_identical_image_reconnect_during_ocr_rejects_tap(monkeypatch, matches):
    phone, screen = FakePhone(), Screen()
    screen.generation = 0
    controls = Controls(phone, screen)
    token = controls.observation()["screen_id"]

    async def reconnected(*_args):
        screen.generation += 1
        return matches

    monkeypatch.setattr(ocr, "find_matches_async", reconnected)
    with pytest.raises(ValueError):
        asyncio.run(controls.perform(ControlAction(action="tap_text", screen_id=token, text="设置")))
    assert not phone.calls


def test_lookup_reports_no_match_without_consuming_token(monkeypatch):
    monkeypatch.setattr(ocr, "find_matches", lambda *_args: [])
    controls = Controls(FakePhone(), Screen())
    token = controls.observation()["screen_id"]
    result = asyncio.run(controls.find_text(token, "不存在"))
    assert result["match"] is None and result["matches"] == []
    assert controls.ticket[0] == token


def test_text_batch_rejected_before_any_hid(matches):
    phone = FakePhone()
    controls = Controls(phone, Screen())
    token = controls.observation()["screen_id"]
    steps = [ControlAction(action="key", screen_id=token, text="a"),
             ControlAction(action="tap_text", screen_id=token, text="设置")]
    with pytest.raises(ValueError, match="单步"):
        asyncio.run(controls.perform_batch(token, steps))
    assert not phone.calls


def test_diff_describes_exact_returned_image_even_if_feed_advances(monkeypatch, request):
    from hidagent.screen import Screen as ReceivedScreen

    clock = request.getfixturevalue("instant_mirror")
    phone, screen = FakePhone(), ReceivedScreen()
    screen._started = True
    screen._on_ready()
    original_frame, first_effect = jpeg("white"), jpeg("black")
    screen._on_jpeg(original_frame)
    controls = Controls(phone, screen)
    token = controls.observation()["screen_id"]
    actual_summarize = screen_diff.summarize
    compared = []

    async def tap(x, y, **kwargs):
        completed = clock.time()
        clock.advance(.01)
        screen._on_jpeg(first_effect)
        return completed

    async def summarize(before, after, **kwargs):
        assert before == original_frame and kwargs["fresh"]
        compared.append(after)
        result = await actual_summarize(before, after, **kwargs)
        if len(compared) == 1:
            # Publish a real later receiver event while A's diff is computed.
            clock.advance(.01)
            screen._on_jpeg(original_frame)
        return result

    phone.tap_verified = tap
    monkeypatch.setattr(screen_diff, "summarize", summarize)
    result = asyncio.run(controls.perform(ControlAction(action="tap", screen_id=token, x=.5, y=.5)))
    assert compared == [first_effect, original_frame]
    assert result["fresh"] and result["screen_id"] != token
    assert base64.b64decode(result["image_base64"]) == original_frame
    assert result["changed"] is False
    assert result["screen_diff"]["changed_fraction"] == 0
    assert controls._observation.frame == controls.ticket[2] == original_frame
    assert controls.ticket[2] != first_effect  # Historical A never authorizes the next action.
    with pytest.raises(ValueError, match="invalid or consumed"):
        controls.consume(token)  # Equal pixels do not revive the consumed pre-action ticket.


def test_screen_diff_can_be_disabled(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("diff was disabled")

    monkeypatch.setattr(screen_diff, "summarize", forbidden)
    controls = Controls(FakePhone(), Screen())
    result = asyncio.run(controls.perform(ControlAction(
        action="tap", screen_id=controls.observation()["screen_id"], x=.5, y=.5, include_diff=False)))
    assert result["screen_diff"] is None and result["image_base64"]


def test_text_routes_return_evidence_and_missing_dependency_is_503(monkeypatch, matches):
    from hidagent import app as module

    async def scenario():
        phone = FakePhone()
        controls = Controls(phone, Screen())
        monkeypatch.setattr(module, "controls", controls)
        monkeypatch.setattr(module, "manual_lock", asyncio.Lock())
        monkeypatch.setattr(module, "agent", SimpleNamespace(running=False))
        monkeypatch.setattr(module, "guard", SimpleNamespace(tripped=False))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://local") as client:
            token = (await client.get("/api/control/observe")).json()["screen_id"]
            found = await client.post("/api/control/find_text", json={"screen_id": token, "text": "设置"})
            assert found.status_code == 200 and found.json()["match"]["text"] == "设置"
            result = await client.post("/api/control/action", json={
                "action": "tap_text", "screen_id": token, "text": "设置", "nth": 2})
            assert result.status_code == 200
            assert result.json()["text_match"]["center"] == [.3, .65]
            assert "screen_diff" in result.json() and phone.calls == [("tap", .3, .65)]

            def missing(*_args):
                raise ocr.OCRUnavailable("请安装 OCR")

            monkeypatch.setattr(ocr, "find_matches", missing)
            token = result.json()["screen_id"]
            for path, extra in (("find_text", {}), ("action", {"action": "tap_text"})):
                failed = await client.post(f"/api/control/{path}", json={
                    "screen_id": token, "text": "设置", **extra})
                assert failed.status_code == 503 and "OCR" in failed.json()["detail"]
            assert phone.calls == [("tap", .3, .65)]

    asyncio.run(scenario())
