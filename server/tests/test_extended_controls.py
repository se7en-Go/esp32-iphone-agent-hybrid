"""Public gestures must share observed coordinates and guarded device execution."""

import asyncio
import math

import httpx
import pytest
from pydantic import ValidationError
from test_controls import FakePhone, Screen
from test_verified_tap import LaggingMirror, calibrated_phone, observed_tap, paced_clock  # noqa: F401

from hidagent.app import ActionStep, ControlAction
from hidagent.control import Controls


def plan(**kwargs):
    request = ControlAction(screen_id="test", **kwargs)
    return Controls(FakePhone(), Screen())._plan(request, 101, 201)


def test_path_normalizes_every_pixel_and_preserves_hold():
    value = plan(action="path", points=[(10, 40), (90, 180)], coordinate_space="pixels", hold=False)
    assert value["points"] == [(0.1, 0.2), (0.9, 0.9)]
    assert value["hold"] is False


def test_circle_has_closed_in_bounds_pixel_geometry():
    value = plan(action="circle", x=50, y=100, radius_x=20, radius_y=20,
                 coordinate_space="pixels", segments=24)
    points = value["points"]
    assert len(points) == 25
    assert points[0] == points[-1]
    assert max(x for x, _ in points) == pytest.approx(.7)
    assert max(y for _, y in points) == pytest.approx(.6)


@pytest.mark.parametrize("kwargs", [
    dict(action="path", points=[(.2, .2)]),
    dict(action="path", points=[(.2, .2), (math.nan, .3)]),
    dict(action="path", points=[(.2, .2), (-.1, .3)]),
    dict(action="path", points=[(.2, .2), (1.1, .3)]),
    dict(action="circle", x=.95, y=.5, radius_x=.1, radius_y=.1),
    dict(action="circle", x=.5, y=.5, radius_x=.1),
    dict(action="long_press", x=.5, y=.5, duration=8),
])
def test_gesture_validation_refuses_invalid_input_before_hid(kwargs):
    with pytest.raises((ValidationError, ValueError)):
        plan(**kwargs)


def test_batch_and_single_action_share_gesture_schema():
    step = ActionStep(action="path", points=[(.1, .2), (.3, .4)], hold=False)
    single = ControlAction(screen_id="test", **step.model_dump())
    assert single.points == step.points


def test_capabilities_disclose_mouse_transport_limits(monkeypatch):
    from types import SimpleNamespace

    from hidagent import app as api

    monkeypatch.setattr(api, "phone", SimpleNamespace(clipboard=None, app_search_available=False))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://local") as client:
            response = await client.get("/api/control/capabilities")
        assert response.status_code == 200
        result = response.json()
        assert {"path", "circle", "long_press", "double_tap", "open_app"} <= set(result["actions"])
        assert result["features"]["multitouch"]["supported"] is False
        assert result["features"]["drawing"]["closed_loop_during_stroke"] is False
        assert result["features"]["clipboard"]["configured"] is False

    asyncio.run(run())


def test_long_press_releases_on_cancellation(tmp_path, monkeypatch):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)
    sleep = asyncio.sleep

    async def cancel_hold(seconds):
        if seconds == .8:
            raise asyncio.CancelledError
        await sleep(seconds)

    monkeypatch.setattr(asyncio, "sleep", cancel_hold)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(observed_tap(phone, .5, .44, duration=.8))
    assert "press" in mirror.commands
    assert mirror.commands[-1] == "release"
    assert "click" not in mirror.commands


def test_double_tap_sends_exactly_two_clicks_without_reposition(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)
    result = asyncio.run(observed_tap(phone, .5, .44, click_count=2))
    first = mirror.commands.index("click")
    assert mirror.commands[first:] == ["click", "click"]
    assert result["click_count"] == 2


def test_invalid_contact_never_moves_pointer(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)
    with pytest.raises(ValueError):
        asyncio.run(observed_tap(phone, .5, .44, duration=.8, click_count=2))
    assert mirror.commands == []
