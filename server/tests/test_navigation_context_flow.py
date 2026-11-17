"""Known system navigation keeps its anchors while unrelated list content refreshes."""

import asyncio
import io
import time
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from test_agent_text import setup as setup_agent
from test_controls import FakePhone
from test_navigation_back import setup_phone
from test_navigation_vision import navigation_frame

from hidagent.agent import ObservationContentChanged
from hidagent.app import ActionStep, ControlAction
from hidagent.control import Controls
from hidagent.freshness import snapshot
from hidagent.navigation_context import from_navigation
from hidagent.screen import Screen
from hidagent.screen_compare import observation_changed, verify_cursor_reference
from hidagent.text_target import TextTargetGuard


def frame(*, list_changed=False, title_changed=False, pointer=None):
    image = Image.open(io.BytesIO(navigation_frame())).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle((214, 78, 282, 93), fill="#222222")
    if list_changed:
        # Four separate glyph footprints previously got mistaken for four old pointers.
        for x in (40, 90, 160, 220):
            draw.rectangle((x, 978, x + 16, 994), fill="#222222")
    if title_changed:
        draw.rectangle((214, 78, 227, 93), fill="#eeeeee")
    if pointer:
        x, y = pointer[0] * image.width, pointer[1] * image.height
        draw.ellipse((x-12, y-12, x+12, y+12), fill=(145, 145, 145))
    data = io.BytesIO()
    image.save(data, "PNG")
    return data.getvalue()


def screen_with(before):
    screen = Screen()
    screen._on_ready()
    screen._on_jpeg(before)
    return screen


@pytest.mark.parametrize("title_changed", [False, True])
def test_navigation_pointer_reference_rejects_anchor_edits_but_not_list_scans(title_changed):
    before = frame()
    screen = screen_with(before)
    context = from_navigation("back", snapshot(screen))
    pair = ((.095, .04), context.target.center)
    outward = frame(list_changed=True, title_changed=title_changed, pointer=pair[0])
    returned = frame(list_changed=True, title_changed=title_changed, pointer=pair[1])
    assert verify_cursor_reference(before, outward, returned, pair, None)[0] is False
    accepted, _ = verify_cursor_reference(before, outward, returned, pair, None, context_regions=context.regions)
    assert accepted is not title_changed


@pytest.mark.parametrize("title_changed", [False, True])
def test_write_guard_consumes_same_navigation_and_measured_pointer(title_changed):
    before = frame()
    screen = screen_with(before)
    context = from_navigation("back", snapshot(screen))
    verified = frame(list_changed=True, pointer=context.target.center)
    screen._on_jpeg(frame(list_changed=True, title_changed=title_changed, pointer=context.target.center))
    evidence = {"cursor": (context.target.center, verified)}
    with pytest.raises(RuntimeError):
        TextTargetGuard(screen, before, context.target)(**evidence)
    if title_changed:
        with pytest.raises(RuntimeError):
            context.validate_current(screen, **evidence)
    else:
        assert context.validate_current(screen, **evidence).frame == screen.frame


def test_changed_pointer_crop_cannot_hide_a_navigation_target_edit():
    before = frame()
    screen = screen_with(before)
    context = from_navigation("back", snapshot(screen))
    verified = frame(list_changed=True, pointer=context.target.center)
    image = Image.open(io.BytesIO(verified)).convert("RGB")
    x, y = context.target.center[0] * image.width, context.target.center[1] * image.height
    ImageDraw.Draw(image).rectangle((x-4, y-4, x+4, y+4), fill="black")
    data = io.BytesIO()
    image.save(data, "PNG")
    screen._on_jpeg(data.getvalue())
    with pytest.raises(RuntimeError):
        context.validate_current(screen, cursor=(context.target.center, verified))


def test_single_back_ticket_and_write_share_context_but_batch_does_not(monkeypatch):
    screen, phone = screen_with(frame()), FakePhone()
    control = Controls(phone, screen)
    contexts = []

    async def back(*, before_action, navigation_context):
        before_action()
        contexts.append(navigation_context)
        return time.time()

    async def respond(*args, **kwargs):
        return {"error": args[5]}

    phone.go_back = back
    monkeypatch.setattr(control, "_respond", respond)
    ticket = control.observation()["screen_id"]
    screen._on_jpeg(frame(list_changed=True))
    result = asyncio.run(control.perform(ControlAction(screen_id=ticket, action="back")))
    assert result["error"] is None
    assert len(contexts) == 1 and contexts[0].observation.frame == frame()

    screen._on_jpeg(frame())
    ticket = control.observation()["screen_id"]
    screen._on_jpeg(frame(list_changed=True))
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(control.perform_batch(ticket, [ActionStep(action="back")]))
    assert len(contexts) == 1


@pytest.mark.parametrize("action", ["tap", "type", "key"])
def test_unrelated_actions_cannot_use_navigation_background_exemption(action):
    screen, phone = screen_with(frame()), FakePhone()
    control = Controls(phone, screen)
    ticket = control.observation()["screen_id"]
    screen._on_jpeg(frame(list_changed=True))
    request = ControlAction(screen_id=ticket, action=action, x=.5, y=.5, text="a")
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(control.perform(request))
    assert not phone.calls


@pytest.mark.parametrize("action,title_changed", [("back", False), ("back", True), ("tap", False)])
def test_agent_first_action_uses_only_detected_system_navigation(action, title_changed):
    agent, _, screen = setup_agent()
    screen.frame, screen.updated = frame(), time.time()
    observed = snapshot(screen)
    command = {"action": action, "_shot": observed.frame, "_observation": observed}
    screen.frame, screen.updated = frame(list_changed=True, title_changed=title_changed), time.time()
    if action == "back" and not title_changed:
        agent._validate_context(command)
        assert command["_navigation_context"].observation is observed
    else:
        with pytest.raises(ObservationContentChanged):
            agent._validate_context(command)


def test_scoped_tap_cleans_up_context_and_cannot_reuse_it_for_ordinary_click(tmp_path):
    phone, screen, _, commands = setup_phone(tmp_path)
    screen._on_jpeg(frame())
    context = from_navigation("back", snapshot(screen))
    screen._on_jpeg(frame(list_changed=True))
    asyncio.run(phone.tap_verified(*context.target.center, target_box=context.target.box,
                                  target_frame=context.observation.frame, navigation_context=context))
    assert commands == ["click"]
    assert phone._navigation_context is None
    assert observation_changed(context.observation.frame, screen.frame)
    with pytest.raises(RuntimeError):
        asyncio.run(phone.tap_verified(*context.target.center, target_box=context.target.box,
                                      target_frame=context.observation.frame))
    assert commands == ["click"]


def test_external_lookalike_context_is_not_a_permission_to_mask_the_screen(tmp_path):
    phone, screen, _, commands = setup_phone(tmp_path)
    bogus = SimpleNamespace(regions=((0, 0, 1, .18),), observation=snapshot(screen))
    with pytest.raises(ValueError):
        asyncio.run(phone.tap_verified(.1, .08, navigation_context=bogus))
    assert not commands
    assert phone._navigation_context is None
