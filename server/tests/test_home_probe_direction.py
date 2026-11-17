"""Home-only probe ranking cannot create a new candidate or confirm a pointer."""

import asyncio
import io
import math
import time
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from test_assistive_home import frame as home_frame
from test_navigation_vision import navigation_frame
from test_pointer_session import setup_phone

from hidagent.freshness import FrameSnapshot
from hidagent.navigation_context import from_navigation
from hidagent.text_target import PointerLandingError


def menu_frame(*, above=False, below=True, bottom=988):
    image = Image.new("RGB", (500, 1080), "#cccce0")
    draw = ImageDraw.Draw(image)
    left, top, right = 80, 590, 487
    draw.rounded_rectangle((left, top, right, bottom), radius=42, fill="#282628")
    for sx, sy in ((.5, .18), (.2, .45), (.8, .45), (.2, .77), (.8, .77), (.52, .77)):
        x, y = round(left + sx * (right-left)), round(top + sy * (bottom-top))
        draw.ellipse((x-16, y-16, x+16, y+16), fill="white")
    x, y = round(left + .52 * (right-left)), round(top + .77 * (bottom-top))
    for enabled, side in ((above, -1), (below, 1)):
        if enabled:
            for dx in (-16, -8, 0, 8, 16):
                draw.rectangle((x+dx, y+side*49-9, x+dx+3, y+side*49+9), fill="white")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def rig(tmp_path, **kwargs):
    phone, device, mirror = setup_phone(tmp_path)
    mirror.frame = menu_frame(**kwargs)
    phone.span = {"w": 270, "h": 584}
    context = from_navigation("home", FrameSnapshot(mirror.frame, time.time(), None, mirror.generation, True))
    assert context is not None and context.kind == "menu"
    phone._navigation_context = context
    phone._tap_target_frame = context.observation.frame
    return phone, device, mirror, context


def original_choice(phone, near, box):
    context, phone._navigation_context = phone._navigation_context, None
    try:
        return phone._target_escape_probe(near, box)
    finally:
        phone._navigation_context = context


@pytest.mark.parametrize("clear_side", [-1, 1])
def test_home_chooses_clear_opposite_direction_on_the_existing_axis(tmp_path, clear_side):
    phone, device, _, context = rig(tmp_path, above=clear_side > 0, below=clear_side < 0)
    near = (context.target.center[0], context.target.center[1] - clear_side * .001)
    old = original_choice(phone, near, context.target.box)
    assert old[0] == 1 and old[1] * clear_side < 0
    selected = phone._target_escape_probe(near, context.target.box)
    assert selected[0] == old[0] and selected[1] * clear_side > 0
    assert abs(selected[1]) * 1080 / 584 <= .18 * 500
    assert device.commands == []
    assert not phone._pointer_confirmed


@pytest.mark.parametrize("above,below", [(False, False), (True, True)])
def test_ambiguous_clarity_preserves_old_candidate_order(tmp_path, above, below):
    phone, _, _, context = rig(tmp_path, above=above, below=below)
    near = context.target.center
    assert phone._target_escape_probe(near, context.target.box) == original_choice(phone, near, context.target.box)


@pytest.mark.parametrize("scope", ["none", "unsealed", "generation", "frame", "box", "dimensions"])
def test_no_home_authority_or_original_geometry_preserves_old_order(tmp_path, scope):
    phone, _, mirror, context = rig(tmp_path)
    near = (context.target.center[0], context.target.center[1] + .001)
    box = context.target.box
    if scope == "none":
        phone._navigation_context = None
    elif scope == "unsealed":
        phone._navigation_context = SimpleNamespace(**context.__dict__)
    elif scope == "generation":
        mirror.generation += 1
    elif scope == "frame":
        phone._tap_target_frame = menu_frame(below=False)
    elif scope == "box":
        box = (box[0]-.001, box[1], box[2], box[3])
    elif scope == "dimensions":
        output = io.BytesIO()
        Image.new("RGB", (500, 1100), "white").save(output, "PNG")
        mirror.frame = output.getvalue()
    assert phone._target_escape_probe(near, box) == original_choice(phone, near, box)


def test_rank_uses_original_observation_not_current_hover_pixels(tmp_path):
    phone, _, mirror, context = rig(tmp_path)
    near = (context.target.center[0], context.target.center[1] + .001)
    # A later frame has texture on the other side. It cannot replace the original evidence.
    mirror.frame = menu_frame(above=True, below=False)
    assert phone._target_escape_probe(near, context.target.box)[1] < 0


def test_candidate_outside_observed_menu_preserves_old_order(tmp_path):
    phone, _, _, context = rig(tmp_path, bottom=850)
    near = (context.target.center[0], context.target.center[1] + .001)
    radius = math.ceil(.034 * 500)
    old = original_choice(phone, near, context.target.box)
    assert near[1] * 1080 + old[1] * 1080 / 584 + radius > context.regions[0][3] * 1080
    assert phone._target_escape_probe(near, context.target.box) == old


def test_direction_selection_does_not_request_an_extension(tmp_path, monkeypatch):
    phone, _, _, context = rig(tmp_path)

    def forbidden(*args):
        pytest.fail("selection must not extend the existing probe")

    monkeypatch.setattr(phone, "_extend_escape_probe", forbidden)
    near = (context.target.center[0], context.target.center[1] + .001)
    assert phone._target_escape_probe(near, context.target.box)[1] < 0


@pytest.mark.parametrize("action,frame,kind", [
    ("home", home_frame(), "bubble"), ("back", navigation_frame(), "back"),
])
def test_other_detector_issued_controls_keep_geometric_probe(tmp_path, action, frame, kind):
    phone, _, mirror = setup_phone(tmp_path)
    mirror.frame = frame
    context = from_navigation(action, FrameSnapshot(frame, time.time(), None, mirror.generation, True))
    assert context.kind == kind
    phone._navigation_context, phone._tap_target_frame = context, frame
    near, box = context.target.center, context.target.box
    assert phone._target_escape_probe(near, box) == original_choice(phone, near, box)


def test_selected_clear_probe_does_not_authorize_unmeasured_contact(tmp_path, monkeypatch):
    phone, device, mirror, context = rig(tmp_path)
    probes = []

    async def missing(*args, **kwargs):
        return None

    async def localize(near, epoch, *, reference=None, probe=None, before_probe=None):
        before_probe()
        probes.append(probe)
        return None

    monkeypatch.setattr(phone, "_find_pointer", missing)
    monkeypatch.setattr(phone, "_localize_pointer", localize)
    near = (context.target.center[0], context.target.center[1] + .001)
    with pytest.raises(PointerLandingError):
        asyncio.run(phone.tap_verified(*near, target_frame=mirror.frame, navigation_context=context))
    assert len(probes) == 1 and probes[0][0] == 1 and probes[0][1] < 0
    assert "click" not in device.commands and "press" not in device.commands
    assert not phone._pointer_confirmed
