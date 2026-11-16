"""Real Home-probe sample: dark-grey pointer, return frames with no pointer.

Fixtures are the device sample (notes-home-probe-redacted.zip, 500x1080):
- pointer-frame-00.png: baseline, pointer on the Home icon (occluded)
- pointer-frame-20.png: outward leg, dark-grey pointer disc at the escape spot
- pointer-frame-30.png / -35.png: return leg, no pointer anywhere

The probe events record near (0.58125, 0.83108), vertical shift -0.0411
(first escape) then -0.0668 (extension). The return frames have no pointer
at all, yet the old occluded branch accepted the return from absence of
evidence and sent a click. These tests pin the refusal: the dark disc must
be seen outward, and zero clicks may be sent when nothing returns.
"""

import asyncio
import io
import time
from pathlib import Path

import pytest
from fake_clock import ManualClock
from PIL import Image
from test_verified_tap import LaggingMirror, calibrated_phone

from hidagent import vision
from hidagent.freshness import FrameSnapshot
from hidagent.navigation_context import from_navigation
from hidagent.text_target import PointerLandingError

FIXTURES = Path(__file__).parent / "fixtures" / "home_probe"
NEAR = (0.58125, 0.8310810810810811)
ESCAPE = (303 / 500, 853 / 1080)  # actual dark disc in the outward frame
DARK = (46, 44, 47)  # real iOS pointer colour in the sample


def load(name):
    return (FIXTURES / name).read_bytes()


def pointer_discs(frame):
    """Pointer-sized grey discs with the widened (dark-inclusive) range."""
    with Image.open(io.BytesIO(frame)) as image:
        rgb = image.convert("RGB")
        width = rgb.size[0]
        return [(x / width, y / rgb.size[1], diameter / width, color)
                for x, y, diameter, color in vision._discs(rgb, width)
                if .025 <= diameter / width <= .065
                and 40 <= min(color) <= max(color) <= 210
                and max(color) - min(color) <= 25]


def test_dark_pointer_disc_found_in_outward_frame():
    frame = load("pointer-frame-20.png")
    with Image.open(io.BytesIO(frame)) as image:
        rgb = image.convert("RGB")
        discs = vision._discs(rgb, 500)
    near_escape = [d for d in discs
                   if .025 <= d[2] / 500 <= .065
                   and 40 <= min(d[3]) <= max(d[3]) <= 210 and max(d[3]) - min(d[3]) <= 25
                   and abs(d[0] / 500 - ESCAPE[0]) <= .02 and abs(d[1] / 1080 - ESCAPE[1]) <= .02]
    assert len(near_escape) == 1
    x, y, diameter, color = near_escape[0]
    assert .045 <= diameter / 500 <= .055
    assert max(abs(a - b) for a, b in zip(color, DARK, strict=True)) <= 8
    # The old 90-210 range never saw this pointer at all.
    old = [d for d in discs
           if .025 <= d[2] / 500 <= .065
           and 90 <= min(d[3]) <= max(d[3]) <= 210 and max(d[3]) - min(d[3]) <= 25
           and abs(d[0] / 500 - ESCAPE[0]) <= .02 and abs(d[1] / 1080 - ESCAPE[1]) <= .02]
    assert not old


def test_no_pointer_disc_in_return_frames():
    for name in ("pointer-frame-30.png", "pointer-frame-35.png"):
        assert pointer_discs(load(name)) == []


class SampleMirror(LaggingMirror):
    """Serve the real frames by move total: outward leg shows the disc, the
    return leg shows the empty frames the device actually produced."""
    generation = 1

    def __init__(self, clock, base, outward, returned, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock
        self.base = base
        self.outward = outward
        self.returned = returned
        self.frame = base
        self._total = (0, 0)
        self._moves = 0

    async def send(self, command):
        self.commands.append(command)
        self._moves += 1
        dx, dy = (int(v) for v in command.split()[1:3])
        if self._moves == 1:
            frame = self.base
            self._total = (0, 0)
        else:
            tx, ty = self._total[0] + dx, self._total[1] + dy
            self._total = (tx, ty)
            frame = self.outward if (tx, ty) != (0, 0) else self.returned
        self.clock.call_later(.15, self.publish, frame)
        await asyncio.sleep(.2)

    def publish(self, image):
        self.frame, self.updated = image, time.time()


def test_sample_return_sends_zero_clicks(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)
    base, outward, returned = (load(f"pointer-frame-{n:02d}.png") for n in (0, 20, 30))
    mirror = SampleMirror(clock, base, outward, returned)
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone._pace = {"w": 500, "h": 1080}, 4
    context = from_navigation("home", FrameSnapshot(base, time.time(), None, mirror.generation, True))
    assert context is not None and context.kind == "menu"
    near = context.target.center
    assert abs(near[0] - NEAR[0]) <= .015 and abs(near[1] - NEAR[1]) <= .015
    phone._navigation_context = context
    phone._tap_target_frame = context.observation.frame
    probe = phone._target_escape_probe(near, context.target.box)
    assert probe is not None and probe[0] == 1 and probe[1] < 0
    with pytest.raises(PointerLandingError):
        asyncio.run(phone.tap_verified(
            *near, target_box=context.target.box, navigation_context=context))
    assert "click" not in mirror.commands
    assert "press" not in mirror.commands
    # The first command is the one-way positioning move to the target; every
    # probe leg after it must be undone, so the pointer ends where it started.
    total_x = total_y = 0
    for command in mirror.commands[1:]:
        dx, dy = (int(v) for v in command.split()[1:3])
        total_x += dx
        total_y += dy
    assert (total_x, total_y) == (0, 0)
