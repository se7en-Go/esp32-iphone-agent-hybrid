"""Only detected system navigation may tolerate unrelated screen changes."""

import io
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from test_assistive_home import frame as home_frame
from test_navigation_vision import navigation_frame

from hidagent.assistive import AssistiveTouch
from hidagent.freshness import require_fresh
from hidagent.navigation_context import NavigationContext, for_phone, from_navigation
from hidagent.screen import Screen
from hidagent.text_target import TargetChangedError, TextTargetGuard


def changed(frame, box, color="black"):
    image = Image.open(io.BytesIO(frame)).convert("RGB")
    width, height = image.size
    ImageDraw.Draw(image).rectangle(tuple(round(value * (width if i % 2 == 0 else height))
                                          for i, value in enumerate(box)), fill=color)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def setup(action, frame):
    screen = Screen()
    screen._on_ready()
    screen._on_jpeg(frame)
    context = from_navigation(action, require_fresh(screen))
    assert context is not None
    return screen, context


@pytest.mark.parametrize("action,frame,kind", [
    ("back", navigation_frame(), "back"),
    ("home", home_frame(menu=True), "menu"),
    ("home", home_frame(), "bubble"),
])
def test_detected_system_controls_scope_only_unrelated_pixels(action, frame, kind):
    screen, context = setup(action, frame)
    assert context.kind == kind
    context.validate_target(frame, screen.generation, context.target.center)
    # A list row outside the complete navigation anchor changes.
    screen._on_jpeg(changed(frame, (.02, .20, .08, .23)))
    context.validate_current(screen)
    with pytest.raises(TargetChangedError):
        TextTargetGuard(screen, frame, context.target)()


@pytest.mark.parametrize("box", [(.04, .07, .06, .09), (.46, .07, .55, .10)])
def test_back_requires_unchanged_arrow_and_whole_page_title(box):
    screen, context = setup("back", navigation_frame())
    screen._on_jpeg(changed(screen.frame, box))
    with pytest.raises(TargetChangedError):
        context.validate_current(screen)


@pytest.mark.parametrize("menu", [False, True])
def test_home_keeps_full_control_or_menu_anchor(menu):
    screen, context = setup("home", home_frame(menu=menu))
    left, top, right, bottom = context.regions[0]
    box = (left+.01, top+.01, min(right, left+.05), min(bottom, top+.03))
    screen._on_jpeg(changed(screen.frame, box))
    with pytest.raises(TargetChangedError):
        context.validate_current(screen)


def test_original_generation_and_pixels_cannot_be_rebound():
    screen, context = setup("back", navigation_frame())
    with pytest.raises(RuntimeError):
        context.validate_target(home_frame(), screen.generation, context.target.center)
    screen._on_ready()
    screen._on_jpeg(context.observation.frame)
    with pytest.raises(RuntimeError, match="连接已变化"):
        context.validate_current(screen)


def test_detected_target_cannot_authorize_another_coordinate():
    screen, context = setup("back", navigation_frame())
    with pytest.raises(ValueError, match="detected system control"):
        context.validate_target(screen.frame, screen.generation, (.8, .08))
    with pytest.raises(ValueError, match="detected system control"):
        context.validate_target(screen.frame, screen.generation, (float("nan"), .08))


def test_context_is_immutable_and_constructor_does_not_accept_arbitrary_regions():
    screen, context = setup("back", navigation_frame())
    with pytest.raises(FrozenInstanceError):
        context.regions = ((0, 0, 0, 0),)
    with pytest.raises(TypeError, match="detector"):
        NavigationContext("back", "back", require_fresh(screen), context.target, ((0, 0, 1, .001),))
    with pytest.raises(TypeError, match="detector"):
        replace(context, regions=((0, 0, 1, .001),))


@pytest.mark.parametrize("action", ["tap", "type", "key", "send", "path"])
def test_regular_actions_never_receive_navigation_scope(action):
    screen, _ = setup("back", navigation_frame())
    assert from_navigation(action, require_fresh(screen)) is None


def test_unknown_system_control_never_creates_scoped_context():
    screen = Screen()
    screen._on_ready()
    screen._on_jpeg(navigation_frame(arrow=None))
    assert from_navigation("back", require_fresh(screen)) is None
    assert from_navigation("home", require_fresh(screen)) is None


def test_size_change_still_refuses_navigation():
    screen, context = setup("back", navigation_frame())
    image = Image.open(io.BytesIO(screen.frame)).resize((250, 540))
    output = io.BytesIO()
    image.save(output, "PNG")
    screen._on_jpeg(output.getvalue())
    with pytest.raises(TargetChangedError):
        context.validate_current(screen)


def test_phone_factory_honors_configured_home_slot_only_with_visible_icon(tmp_path):
    screen, default = setup("home", home_frame(menu=True))
    assistive = AssistiveTouch(SimpleNamespace(), tmp_path / "profile.json", layout={"home": (.19, .33)})
    phone = SimpleNamespace(assistive_touch=assistive)
    custom = for_phone(phone, "home", require_fresh(screen))
    assert custom is not None and custom.kind == "menu"
    assert custom.target.center == pytest.approx((100/360, 332/780), abs=.01)
    assert custom.target.center != default.target.center
    # An arbitrary configured slot on empty menu content grants no target.
    assistive.layout["home"] = (.50, .50)
    assert for_phone(phone, "home", require_fresh(screen)) is None


def test_phone_factory_cannot_issue_context_for_regular_actions(tmp_path):
    screen, _ = setup("back", navigation_frame())
    assistive = AssistiveTouch(SimpleNamespace(), tmp_path / "profile.json")
    assert for_phone(SimpleNamespace(assistive_touch=assistive), "tap", require_fresh(screen)) is None
