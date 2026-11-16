"""Regression 1: the Home hover highlight drives the complete click flow.

Uses the real white->blue Home frames from the round-3 device attempt
(item2-menu-tap3 unhighlighted, item2-after-openapp2 highlighted). The
pointer disc is invisible over the dark menu, so the landing comes only
from the observed Home highlight; the requested coordinates are never
written down as a measurement.

- Valid highlight (white -> blue on the same Home item): exactly one click.
- Static highlight (already blue before the move): zero clicks.
- Target changed (the highlight is valid but another menu item also
  changed): zero clicks -- the pre-click guard still intercepts it.
"""
import asyncio
import io
import pathlib
import time

import pytest
from PIL import Image, ImageChops, ImageDraw
from test_pointer_session import setup_phone

from hidagent.freshness import FrameSnapshot
from hidagent.navigation_context import from_navigation
from hidagent.text_target import PointerLandingError, TargetChangedError

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "menu_highlight_click"
MENU_HIGHLIGHT_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "menu_highlight"


def load(name):
    return (FIXTURES / name).read_bytes()


def rig(tmp_path, before_name, after_name, *, after_hook=None):
    """A phone whose screen shows `before`, then `after` once the pointer moves.

    The device's move switches the mirror frame to `after`, which is how the
    real hover highlight appears when the pointer arrives at Home. The disc
    itself is invisible, so _find_pointer returns None and the landing must
    come from the highlight.
    """
    phone, device, mirror = setup_phone(tmp_path)
    before = load(before_name)
    after = load(after_name)
    mirror.frame = before
    mirror.updated = time.time()
    context = from_navigation(
        "home", FrameSnapshot(before, time.time(), None, mirror.generation, True))
    assert context is not None and context.kind == "menu"
    phone._navigation_context = context
    phone._tap_target_frame = before

    async def send(command):
        device.commands.append(command)
        if command.startswith("move "):
            frame = after_hook(after) if after_hook is not None else after
            mirror.frame = frame
            mirror.updated = time.time()

    device.send = send

    async def no_pointer(*args, **kwargs):
        return None

    async def no_localize(*args, **kwargs):
        return None

    async def no_effect(*args, **kwargs):
        return False, True

    phone._find_pointer = no_pointer
    phone._localize_pointer = no_localize
    phone._tap_effect = no_effect
    return phone, device, mirror, context


def test_valid_highlight_sends_exactly_one_click(tmp_path):
    phone, device, mirror, context = rig(
        tmp_path, "unhighlighted.jpg", "highlighted.jpg")
    near = context.target.center

    result = asyncio.run(phone.tap_verified(
        *near, target_frame=mirror.frame, navigation_context=context))

    assert result["clicked"] is True
    assert device.commands.count("click") == 1
    # The highlight evidence is recorded separately from the pointer
    # calibration, and its centre is the observed highlight -- not the
    # requested coordinates.
    evidence = phone._menu_highlight_evidence
    assert evidence["before_blue"] == 0
    assert evidence["after_blue"] > 100
    assert evidence["after_other_max"] == 0
    centre = evidence["centre"]
    assert centre == pytest.approx((0.584, 0.838), abs=0.01)
    assert centre != pytest.approx(near, abs=1e-9)
    # The calibration learned a real (requested, measured) pair.
    assert phone._seen["x"] and phone._seen["y"]


def test_highlight_centroid_inside_box_but_outside_aim_tolerance_sends_one_click(tmp_path):
    # Live diagnosis: the highlight centroid (0.584, 0.838) can sit 0.009
    # from the requested target centre in y, just outside AIM_TOLERANCE
    # (0.008), while the disc was never pair-verified over the blue
    # highlight. The centroid is still inside the Home target box, and the
    # blue highlight only appears while the pointer hovers Home, so the
    # landing must be accepted and exactly one click sent.
    phone, device, mirror, context = rig(
        tmp_path, "unhighlighted.jpg", "highlighted.jpg")
    # Request a point inside the Home box but 0.010 above the highlight
    # centroid in y, so the miss exceeds AIM_TOLERANCE.
    near = (context.target.center[0], 0.828)

    result = asyncio.run(phone.tap_verified(
        *near, target_frame=mirror.frame, navigation_context=context))

    assert result["clicked"] is True
    assert device.commands.count("click") == 1
    centre = phone._menu_highlight_evidence["centre"]
    assert centre == pytest.approx((0.584, 0.838), abs=0.01)


def test_static_highlight_sends_zero_clicks(tmp_path):
    # Home is already blue before the move: there is no highlight change
    # caused by this move, so no landing is found and nothing is sent.
    phone, device, mirror, context = rig(
        tmp_path, "highlighted.jpg", "highlighted.jpg")
    near = context.target.center

    with pytest.raises(PointerLandingError):
        asyncio.run(phone.tap_verified(
            *near, target_frame=mirror.frame, navigation_context=context))

    assert device.commands.count("click") == 0
    assert not phone._pointer_confirmed


def test_target_changed_sends_zero_clicks(tmp_path):
    # The Home highlight is valid, but another menu item changed too (a red
    # patch over the top-left slot). The landing is found, but the pre-click
    # guard must still intercept the unrelated change.
    def changed(after):
        image = Image.open(io.BytesIO(after)).convert("RGB")
        draw = ImageDraw.Draw(image)
        # Top-left menu slot, in the 500x1080 frame.
        draw.rectangle((110, 610, 150, 650), fill=(200, 30, 30))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=90)
        return out.getvalue()

    phone, device, mirror, context = rig(
        tmp_path, "unhighlighted.jpg", "highlighted.jpg", after_hook=changed)
    near = context.target.center

    with pytest.raises(RuntimeError, match="目标或屏幕已变化"):
        asyncio.run(phone.tap_verified(
            *near, target_frame=mirror.frame, navigation_context=context))

    assert device.commands.count("click") == 0


def _before_click_write(device, hook):
    original = device.send_guarded

    async def wrapped(command, before_write, **kwargs):
        if command == "click":
            hook()
        return await original(command, before_write, **kwargs)

    device.send_guarded = wrapped


def test_pointer_fading_over_home_highlight_between_landing_and_write_sends_one_click(tmp_path):
    # Pointer fade over bright blue Home alters RGB >100 levels between landing
    # measurement (highlighted_oa2.jpg) and write validation (highlighted.jpg).
    phone, device, mirror, context = rig(
        tmp_path, "unhighlighted.jpg", "highlighted.jpg")
    oa2 = (MENU_HIGHLIGHT_FIXTURES / "highlighted_oa2.jpg").read_bytes()
    final = load("highlighted.jpg")

    async def send_with_fade(command):
        device.commands.append(command)
        if command.startswith("move "):
            mirror.frame = oa2
            mirror.updated = time.time()

    device.send = send_with_fade
    _before_click_write(device, lambda: setattr(mirror, "frame", final))
    near = context.target.center

    result = asyncio.run(phone.tap_verified(
        *near, target_frame=mirror.frame, navigation_context=context))

    assert result["clicked"] is True
    assert device.commands.count("click") == 1
    assert phone._menu_highlight_evidence["centre"] == pytest.approx((0.582, 0.839), abs=0.01)


def test_highlight_lost_before_write_sends_zero_clicks(tmp_path):
    phone, device, mirror, context = rig(
        tmp_path, "unhighlighted.jpg", "highlighted.jpg")
    unhighlighted = load("unhighlighted.jpg")
    _before_click_write(device, lambda: setattr(mirror, "frame", unhighlighted))

    with pytest.raises(TargetChangedError, match="目标或屏幕已变化"):
        asyncio.run(phone.tap_verified(
            *context.target.center, target_frame=mirror.frame, navigation_context=context))

    assert device.commands.count("click") == 0


def test_menu_panel_shifted_before_write_sends_zero_clicks(tmp_path):
    phone, device, mirror, context = rig(
        tmp_path, "unhighlighted.jpg", "highlighted.jpg")
    shifted_image = ImageChops.offset(Image.open(io.BytesIO(load("highlighted.jpg"))).convert("RGB"), 18, 0)
    buf = io.BytesIO()
    shifted_image.save(buf, "JPEG", quality=92)
    shifted = buf.getvalue()
    _before_click_write(device, lambda: setattr(mirror, "frame", shifted))

    with pytest.raises(TargetChangedError, match="目标或屏幕已变化"):
        asyncio.run(phone.tap_verified(
            *context.target.center, target_frame=mirror.frame, navigation_context=context))

    assert device.commands.count("click") == 0


def test_pre_move_validate_current_rejects_unsolicited_highlight(tmp_path):
    phone, device, mirror, context = rig(
        tmp_path, "unhighlighted.jpg", "highlighted.jpg")
    mirror.frame = load("highlighted.jpg")
    mirror.updated = time.time()

    with pytest.raises(TargetChangedError, match="目标或屏幕已变化"):
        context.validate_current(mirror)
