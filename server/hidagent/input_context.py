"""Keep typing in its observed field, including while firmware ACKs are pending."""

import io
import logging
from functools import lru_cache

from PIL import Image

from . import screen_compare
from .caret import focused_region as _focused_region
from .caret import temporal_focused_region
from .freshness import require_fresh

logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def keyboard_suggestion_bar(frame: bytes) -> tuple[float, float] | None:
    """Return the (top, bottom) of the iOS keyboard suggestion bar, or None.

    The keyboard is light grey (brightness 150-245); the white note is 255
    and the dark AssistiveTouch menu is ~40, so that band separates the
    keyboard from both. The suggestion bar is the top strip of the keyboard,
    one key-height tall. The keyboard top is the first row band, scanning
    down from mid-screen, whose grey share exceeds 0.40 with the region
    below it sustained grey to the bottom; the 0.40 top threshold keeps the
    dark menu (grey 0.15-0.23) from faking a keyboard top. A dark-mode
    keyboard has no light-grey keys, so this returns None there and the
    guard simply does not fire.
    """
    with Image.open(io.BytesIO(frame)) as image:
        rgb = image.convert("RGB")
    width, height = rgb.size
    sw, sh = 120, max(1, round(120 * height / width))
    raw = rgb.resize((sw, sh), Image.Resampling.BILINEAR).tobytes()
    # Read channel sums once from the contiguous byte buffer (3 * luma) so bare
    # tap guards stay ~2 ms even when both detectors run on slow CI runners.
    sums = [r + g + b for r, g, b in zip(raw[0::3], raw[1::3], raw[2::3], strict=True)]
    found = _white_page_suggestion_bar(sums, sw, sh)
    return found if found is not None else _light_page_suggestion_bar(sums, sw, sh)


def _white_page_suggestion_bar(sums, sw, sh):
    band_h = max(1, round(0.02 * sh))
    bands = []
    for y0 in range(0, sh, band_h):
        chunk = sums[y0 * sw:min(y0 + band_h, sh) * sw]
        bands.append(sum(450 <= value <= 735 for value in chunk) / len(chunk) if chunk else 0)
    for index in range(len(bands) // 2, len(bands)):
        if bands[index] <= 0.40:
            continue
        below = bands[index:]
        if sum(below) / len(below) <= 0.15:
            continue
        # A real keyboard is a bounded region at the bottom with different
        # content above it. A uniform grey background (the legacy test
        # scene, or a plain grey app) must not read as a keyboard: require
        # a sharp top edge and non-grey content above it.
        if index == 0 or bands[index] - bands[index - 1] <= 0.30:
            continue
        above = bands[:index]
        if above and sum(above) / len(above) >= 0.50:
            continue
        top = index * band_h / sh
        return (top, min(1.0, top + 0.06))
    return None


def _light_page_suggestion_bar(sums, sw, sh):
    """Keyboard under a light-grey page (WeChat chat, ~238 brightness).

    The white-page detector counts that page as keyboard grey (150-245), so the
    keyboard never shows a top edge. Here the keyboard band is 150-232 and must
    be clearly darker than the page above it; dark menus, uniform grey scenes
    and screens without a keyboard still find nothing. The top is located to a
    single row so the same keyboard reads the same strip in every frame.
    """
    rows, lumas = [], []
    for y in range(sh):
        row = sums[y * sw:(y + 1) * sw]
        rows.append(sum(450 <= value <= 696 for value in row) / sw)
        lumas.append(sum(row) / (3 * sw))
    run = max(3, round(.018 * sh))  # the strip starts with a solid, full-width keyboard band
    page = max(3, round(.10 * sh))
    for y in range(max(page, sh // 2 - round(.05 * sh)), min(sh - run, round(.72 * sh))):
        if min(rows[y:y + run]) < .90:
            continue
        # Text lines in a grown input field are only partly grey; a keyboard
        # is grey across the full width, darker than the page above it, and
        # continues to the bottom of the screen.
        above = lumas[y - run:y]
        if sum(above) / run - sum(lumas[y:y + run]) / run < 12:
            continue
        if sum(rows[y:]) / (sh - y) <= .15 or sum(rows[:y]) / y >= .50:
            continue
        top = y / sh
        return (top, min(1.0, top + 0.06))
    return None


def point_in_suggestion_bar(frame: bytes, x: float, y: float) -> bool:
    """Whether the point (x, y) falls inside the keyboard suggestion bar."""
    bar = keyboard_suggestion_bar(frame)
    return bar is not None and bar[0] <= y <= bar[1]


class InputContextGuard:
    def __init__(self, screen, observation, *, allow_white=False, accept_field=None,
                 allow_assistive_opacity: bool = False):
        self.screen, self.original = screen, observation
        self.allow_assistive_opacity = bool(allow_assistive_opacity)
        self.region = _focused_region(observation.frame, allow_white=allow_white, accept_field=accept_field)
        # This guard may be created after the first chunk was acknowledged.
        # Only history from the original observation can establish its field;
        # newly typed text or a later connection must never supply that proof.
        frames = tuple(frame for frame in getattr(screen, "recent_frames", ())
                       if frame.generation == observation.generation
                       and 0 <= observation.updated - frame.updated <= 4)
        if self.region is None:
            self.region = temporal_focused_region(observation.frame, frames, allow_white=allow_white,
                                                 accept_field=accept_field)
        if self.region is None and allow_white:
            # Spotlight may be observed during the caret's dark phase. Only a
            # proven blink on otherwise identical pixels can recover its field.
            for recent in reversed(frames[-12:]):
                candidate = _focused_region(recent.frame, allow_white=True, accept_field=accept_field)
                if candidate is not None and not screen_compare.observation_changed(
                        recent.frame, observation.frame, caret_frames=frames):
                    self.region = candidate
                    break

    def __call__(self, *, stage="context"):
        current = require_fresh(self.screen)
        region = self.region if stage in {"accept", "chunk"} else None
        if current.generation != self.original.generation:
            raise RuntimeError("screen changed since the original observation; observe again before acting")
        residuals: list = []
        if screen_compare.observation_changed(
            self.original.frame, current.frame, input_region=region,
            caret_frames=getattr(self.screen, "recent_frames", ()), caret_scope=self.region,
            allow_assistive_opacity=self.allow_assistive_opacity, residual_out=residuals,
        ):
            logger.info("input context guard rejected (stage=%s): residuals=%s", stage, residuals)
            raise RuntimeError("screen changed since the original observation; observe again before acting")
        return current
