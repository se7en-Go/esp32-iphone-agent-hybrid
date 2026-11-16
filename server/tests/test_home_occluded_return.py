"""Occluded Home-icon return must refuse without a pairable pointer.

The Home icon is smaller than the pointer disc, so the pointer sitting on it
cannot pair a full disc in the return frame. There used to be an occluded
branch that accepted the return on outward-leg + no-other-change evidence;
it is removed: a return frame with no pointer at all passed it, so the
requested coordinates became the measurement and a click was sent. Now the
return must pair a disc displaced by the return move, or the landing stays
unconfirmed and tap_verified sends zero clicks.
"""

import asyncio
import io
import time
from types import SimpleNamespace

import pytest
from fake_clock import ManualClock
from PIL import Image, ImageDraw
from test_home_probe_direction import menu_frame
from test_verified_tap import LaggingMirror, calibrated_phone

from hidagent.freshness import FrameSnapshot
from hidagent.navigation_context import from_navigation

DISC = (150, 150, 150)
DISC_R = 13  # 26px diameter = 5.2% of the 500px fixture width
HIGHLIGHT = (60, 130, 210)  # hovered Home turns blue; body larger than the glyph
HIGHLIGHT_R = 20  # 40px diameter = 8% of width: too large for the overlay detector


def with_pointer(base, center):
    image = Image.open(io.BytesIO(base)).convert("RGB")
    draw = ImageDraw.Draw(image)
    x, y = round(center[0] * 500), round(center[1] * 1080)
    draw.ellipse((x - DISC_R, y - DISC_R, x + DISC_R, y + DISC_R), fill=DISC)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90)
    return out.getvalue()


def with_occluded_pointer(base, center):
    """Pointer on the icon, fully hidden under the blue hover body."""
    image = Image.open(io.BytesIO(base)).convert("RGB")
    draw = ImageDraw.Draw(image)
    x, y = round(center[0] * 500), round(center[1] * 1080)
    draw.ellipse((x - DISC_R, y - DISC_R, x + DISC_R, y + DISC_R), fill=DISC)
    draw.ellipse((x - HIGHLIGHT_R, y - HIGHLIGHT_R, x + HIGHLIGHT_R, y + HIGHLIGHT_R),
                 fill=HIGHLIGHT)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90)
    return out.getvalue()


def with_block(base, center, size=24):
    image = Image.open(io.BytesIO(base)).convert("RGB")
    draw = ImageDraw.Draw(image)
    x, y = round(center[0] * 500), round(center[1] * 1080)
    draw.rectangle((x, y, x + size, y + size), fill="white")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90)
    return out.getvalue()


class ScriptedMirror(LaggingMirror):
    generation = 1

    def __init__(self, clock, base, near, *, returned, stuck=False, point_first=False, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock
        self.base = base
        self.near = near
        self.returned_frame = returned
        self.stuck = stuck
        self.point_first = point_first
        self.frame = base
        self._moves = 0
        self._total = (0, 0)
        self._outward = None

    async def send(self, command):
        self.commands.append(command)
        self._moves += 1
        dx, dy = (int(v) for v in command.split()[1:3])
        if self.point_first and self._moves == 1:
            # The positioning move to the target: no pointer evidence yet.
            frame = self.base
            self._total = (0, 0)
        else:
            tx, ty = self._total[0] + dx, self._total[1] + dy
            self._total = (tx, ty)
            if (tx, ty) == (0, 0):
                frame = self._outward if self.stuck else self.returned_frame
            else:
                escape = (self.near[0] + tx / 500, self.near[1] + ty / 1080)
                self._outward = with_pointer(self.base, escape)
                frame = self._outward
        self.clock.call_later(.15, self.publish, frame)
        await asyncio.sleep(.2)

    def publish(self, image):
        self.frame, self.updated = image, time.time()


def rig(tmp_path, monkeypatch, *, returned, stuck=False, point_first=False):
    clock = ManualClock()
    clock.install(monkeypatch)
    base = menu_frame()
    mirror = ScriptedMirror(clock, base, (0, 0), returned=returned, stuck=stuck,
                            point_first=point_first)
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone._pace = {"w": 500, "h": 1080}, 4
    context = from_navigation("home", FrameSnapshot(base, time.time(), None, mirror.generation, True))
    assert context is not None and context.kind == "menu"
    phone._navigation_context = context
    phone._tap_target_frame = context.observation.frame
    mirror.near = context.target.center
    probe = phone._target_escape_probe(context.target.center, context.target.box)
    assert probe is not None
    return phone, mirror, context, probe


def test_occluded_return_refuses_pointer_left_at_escape_spot(tmp_path, monkeypatch):
    base = menu_frame()
    phone, mirror, context, probe = rig(tmp_path, monkeypatch, returned=None, stuck=True)
    result = asyncio.run(phone._localize_pointer(
        context.target.center, 0, reference=(base, None), probe=probe))
    assert result is None
    assert not phone._pointer_pair_verified


def test_occluded_return_refuses_unrelated_change_outside_icon(tmp_path, monkeypatch):
    # Inside the menu panel but outside the icon box: the navigation context
    # deliberately ignores changes outside the panel, so the block must sit
    # in the panel's flat area to count as an unrelated change.
    base = menu_frame()
    phone, mirror, context, probe = rig(tmp_path, monkeypatch, returned=None)
    mirror.returned_frame = with_block(
        with_occluded_pointer(base, context.target.center), (.35, .6))
    result = asyncio.run(phone._localize_pointer(
        context.target.center, 0, reference=(base, None), probe=probe))
    assert result is None
    assert not phone._pointer_pair_verified


def test_occluded_return_refuses_static_disc_at_escape_spot(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)
    base = menu_frame()
    mirror = ScriptedMirror(clock, base, (0, 0), returned=None)
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone._pace = {"w": 500, "h": 1080}, 4
    context = from_navigation("home", FrameSnapshot(base, time.time(), None, mirror.generation, True))
    phone._navigation_context = context
    phone._tap_target_frame = context.observation.frame
    near = context.target.center
    probe = phone._target_escape_probe(near, context.target.box)
    assert probe is not None
    dx, dy = (0, probe[1]) if probe[0] == 1 else (probe[1], 0)
    escape = (near[0] + dx / 500, near[1] + dy / 1080)
    static = with_pointer(base, escape)
    mirror.base = static
    mirror.frame = static
    mirror.near = near
    mirror.returned_frame = with_occluded_pointer(static, near)
    result = asyncio.run(phone._localize_pointer(near, 0, reference=(static, None), probe=probe))
    assert result is None
    assert not phone._pointer_pair_verified


def test_occluded_pointer_in_return_frame_is_not_pairable(tmp_path, monkeypatch):
    # The grey disc hidden under the blue hover body is not a pointer-sized
    # disc in the return frame, so no displacement can be paired to it.
    base = menu_frame()
    phone, mirror, context, probe = rig(tmp_path, monkeypatch, returned=None)
    mirror.returned_frame = with_occluded_pointer(base, context.target.center)
    phone._navigation_context = None
    result = asyncio.run(phone._localize_pointer(
        context.target.center, 0, reference=(base, None), probe=probe))
    assert result is None
    phone._navigation_context = SimpleNamespace(
        action="back", kind="back",
        observation=FrameSnapshot(base, time.time(), None, 1, True),
        regions=((0., 0., 1., 1.),))
    result = asyncio.run(phone._localize_pointer(
        context.target.center, 0, reference=(base, None), probe=probe))
    assert result is None
    assert not phone._pointer_pair_verified
