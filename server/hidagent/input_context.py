"""Keep typing in its observed field, including while firmware ACKs are pending."""

import io

from PIL import Image, ImageChops, ImageDraw

from . import screen_compare
from .freshness import require_fresh


def _focused_region(frame):
    """Confirm a bounded field containing one blue insertion caret in the original image.

    An unbordered, hidden or ambiguous field grants no pixel exemption. Never infer
    an input region from the changes caused by a command that has already run.
    """
    with Image.open(io.BytesIO(frame)) as original:
        w = min(360, original.width)
        image = original.convert("RGB").resize((w, max(1, round(original.height * w / original.width))))
    w, h = image.size
    blue = Image.new("L", image.size)
    pixels = image.tobytes()
    blue.putdata([255 if b >= 150 and b - r >= 60 and b - g >= 25 and g >= 50 else 0
                  for r, g, b in zip(pixels[0::3], pixels[1::3], pixels[2::3], strict=True)])
    carets = []
    for points in screen_compare._regions(blue, 0):
        xs, ys = zip(*points, strict=True)
        x0, y0, x1, y1 = min(xs), min(ys), max(xs) + 1, max(ys) + 1
        if (1 <= x1 - x0 <= max(3, .012 * w) and max(6, .008 * h) <= y1 - y0 <= .05 * h
                and y1 - y0 >= 3 * (x1 - x0) and y0 > .06 * h):
            carets.append((x0, y0, x1, y1))
    if len(carets) != 1:
        return None
    x0, y0, x1, y1 = carets[0]
    for sx in (x1 + 3, x0 - 3):
        if not 0 <= sx < w:
            continue
        seed = (sx, (y0 + y1) // 2)
        color = image.getpixel(seed)
        delta = ImageChops.difference(image, Image.new("RGB", image.size, color))
        channels = delta.split()
        mask = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
        mask = mask.point(lambda value: 255 if value <= 12 else 0)
        ImageDraw.floodfill(mask, seed, 128)
        bounds = mask.point(lambda value: 255 if value == 128 else 0).getbbox()
        if bounds is None:
            continue
        left, top, right, bottom = bounds
        if (0 < left < x0 < x1 < right < w and .06 * h < top < y0 < y1 < bottom < h
                and .2 * w <= right - left <= .96 * w and .02 * h <= bottom - top <= .2 * h):
            return left / w, top / h, (right - 1) / w, (bottom - 1) / h
    return None


class InputContextGuard:
    def __init__(self, screen, observation):
        self.screen, self.original = screen, observation
        self.region = _focused_region(observation.frame)

    def __call__(self, *, stage="context"):
        current = require_fresh(self.screen)
        region = self.region if stage in {"accept", "chunk"} else None
        if (current.generation != self.original.generation
                or screen_compare.observation_changed(self.original.frame, current.frame, input_region=region)):
            raise RuntimeError("screen changed since the original observation; observe again before acting")
        return current
