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
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from test_pointer_session import setup_phone

from hidagent.freshness import FrameSnapshot
from hidagent.navigation_context import from_navigation
from hidagent.text_target import PointerLandingError

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "menu_highlight_click"


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
