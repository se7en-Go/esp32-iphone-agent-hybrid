import io
import time
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from hidagent.text_target import TextTargetGuard


def home_screen(tick, *, replaced=False):
    image = Image.new("RGB", (720, 1560), "#4d4db8")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((555, 496, 670, 611), radius=24, fill="#171819")
    draw.ellipse((566, 507, 659, 600), fill="#f8f8f8")
    draw.line((613, 554, 602, 536), fill="#171819", width=5)
    draw.line((613, 554, 628, 573), fill="#171819", width=4)
    draw.line((613, 554, *tick), fill="#ed6c23", width=3)
    draw.ellipse((608, 549, 618, 559), fill="#ed6c23")
    draw.rounded_rectangle((362, 852, 478, 968), radius=24,
                           fill="#ffda37" if replaced else "#aaaeb0")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=85)
    return out.getvalue()


def test_clock_ticking_does_not_cancel_unrelated_click():
    before = home_screen((645, 537))
    after = home_screen((633, 525))
    screen = SimpleNamespace(live=True, frame=after, updated=time.time())
    guard = TextTargetGuard(screen, before, SimpleNamespace(center=(.58, .58)))

    guard()


def test_clock_ticking_does_not_hide_replaced_target():
    before = home_screen((645, 537))
    after = home_screen((633, 525), replaced=True)
    screen = SimpleNamespace(live=True, frame=after, updated=time.time())
    guard = TextTargetGuard(screen, before, SimpleNamespace(center=(.58, .58)))

    with pytest.raises(RuntimeError, match="目标或屏幕已变化"):
        guard()
