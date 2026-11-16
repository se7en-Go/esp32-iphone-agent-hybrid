"""Occluded Home-icon return: a correctly returned pointer is a crescent, never a pair.

The Home icon is smaller than the pointer disc, so the pointer sitting on it
cannot pair a full disc in the return frame. The occluded branch may accept
the return only on the outward leg + no-other-change evidence, Home-menu only.
The hovered icon turns blue with a rounded body larger than the glyph, which
is why the normal pairing finds no overlay to pair with in the return frame.
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


def move_total(commands):
    total_x = total_y = 0
    for command in commands:
        dx, dy = (int(v) for v in command.split()[1:3])
        total_x += dx
        total_y += dy
    return total_x, total_y


def test_occluded_return_accepts_home_icon_landing(tmp_path, monkeypatch):
    base = menu_frame()
    phone, mirror, context, probe = rig(tmp_path, monkeypatch, returned=None)
    mirror.returned_frame = with_occluded_pointer(base, context.target.center)
    result = asyncio.run(phone._localize_pointer(
        context.target.center, 0, reference=(base, None), probe=probe))
    assert result == pytest.approx(context.target.center, abs=.003)
    assert phone._pointer_pair_verified
    # Outward, maybe one extension, then the exact inverse: never a blind click.
    assert move_total(mirror.commands) == (0, 0)
    assert len(mirror.commands) in (2, 3)


def test_occluded_return_confirms_through_point_verified(tmp_path, monkeypatch):
    base = menu_frame()
    phone, mirror, context, _probe = rig(tmp_path, monkeypatch, returned=None, point_first=True)
    mirror.returned_frame = with_occluded_pointer(base, context.target.center)

    async def missing(*args, **kwargs):
        return None

    monkeypatch.setattr(phone, "_find_pointer", missing)
    found = asyncio.run(phone.point_verified(*context.target.center, target_box=context.target.box))
    assert found == pytest.approx(context.target.center, abs=.003)
    assert phone._pointer_confirmed
    assert "click" not in mirror.commands


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


def test_occluded_return_needs_home_menu_context(tmp_path, monkeypatch):
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
