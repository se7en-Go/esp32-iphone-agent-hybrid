"""Recheck an OCR target's visible context immediately before clicking."""

import io

from PIL import Image, ImageChops, ImageDraw

from . import vision
from .caret import mask_caret_blink
from .freshness import require_fresh, snapshot
from .screen_compare import _regions, mask_clock_ticks, mask_pointer_highlights, retain_context_regions


def _image(frame):
    with Image.open(io.BytesIO(frame)) as image:
        return image.convert("RGB")


def _discs(image):
    width, height = image.size
    scale = min(1, 180 / width)
    small = image.resize((round(width * scale), round(height * scale)), Image.Resampling.BOX)
    return [(x / scale, y / scale, diameter / scale)
            for x, y, diameter, color in vision._discs(small, small.width)
            if .025 * small.width <= diameter <= .065 * small.width
            and 90 <= min(color) <= max(color) <= 210 and max(color) - min(color) <= 25]


def _cursor_patch_matches(image, reference, center):
    width, height = image.size
    x, y = center
    cx, cy, radius = x * width, y * height, .036 * width
    box = (max(0, int(cx - radius)), max(0, int(cy - radius)),
           min(width, round(cx + radius + 1)), min(height, round(cy + radius + 1)))
    measured = _image(reference)
    if measured.size != image.size:
        return False
    red, green, blue = ImageChops.difference(measured.crop(box), image.crop(box)).split()
    change = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    return (sum(change.histogram()[56:]) < 3
            and not any(len(region) >= 6 for region in _regions(change, 25)))


class PointerLandingError(RuntimeError):
    """The pointer landing could not be verified before writing a click."""


class TargetChangedError(RuntimeError):
    """The final comparison rejected the action before it could be sent."""


class TextTargetGuard:
    """Ignore recognized cursor discs/outlines, never the entire target rectangle."""

    def __init__(self, screen, before: bytes, target, *, observation=None, navigation_context=None):
        self.screen, self.target = screen, target
        self.generation = (observation if observation is not None else snapshot(screen)).generation
        self.navigation_context = navigation_context
        if navigation_context is not None:
            from .navigation_context import NavigationContext

            if not isinstance(navigation_context, NavigationContext):
                raise ValueError("navigation requires internally observed system controls")
            navigation_context.validate_target(before, self.generation, target.center)
        self.before = _image(before)
        self.discs = _discs(self.before)

    def __call__(self, *, cursor=None, reference=None):
        current = require_fresh(self.screen)
        if current.generation != self.generation:
            raise RuntimeError("目标观察后画面连接已变化，未发送点击；请重新观察。")
        try:
            after = _image(current.frame)
        except (OSError, ValueError) as exc:
            raise RuntimeError("点击前画面无效，未发送点击。") from exc
        if after.size != self.before.size or self._changed(after, cursor=cursor, reference=reference):
            raise TargetChangedError("指针定位期间目标或屏幕已变化，未发送点击；请重新观察。")

    def _changed(self, after, *, cursor=None, reference=None):
        width, height = after.size
        delta = ImageChops.difference(self.before, after)
        red, green, blue = delta.split()
        mask = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        retain_context_regions(mask, self.navigation_context.regions if self.navigation_context is not None else None)
        mask_caret_blink(self.before, after, mask, getattr(self.screen, "recent_frames", ()))
        mask_clock_ticks(self.before, after, mask)
        _, highlights = mask_pointer_highlights(self.before, after, mask)
        mask = mask.point(lambda value: 255 if value > 28 else 0)
        draw = ImageDraw.Draw(mask)
        target_top = getattr(self.target, "box", (0, self.target.center[1]))[1]
        if target_top >= .06:
            draw.rectangle((0, 0, width, round(.06 * height)), fill=0)

        def mask_cursor(point):
            x, y = point
            cx, cy, radius = x * width, y * height, .036 * width
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=0)

        if cursor is not None:
            point, observed = cursor
            # Only suppress the cursor footprint while the write-time crop still
            # matches the frame in which its movement was verified.
            if not _cursor_patch_matches(after, observed, point):
                return True
            mask_cursor(point)
        if reference is not None:
            point, original, unoccluded = reference
            if (_cursor_patch_matches(self.before, original, point)
                    and _cursor_patch_matches(after, unoccluded, point)):
                mask_cursor(point)

        # Only a changed, cursor-sized disc may disappear from the original frame.
        old = []
        for x, y, diameter in self.discs:
            radius = diameter / 2
            patch = mask.crop((int(x - radius), int(y - radius), int(x + radius), int(y + radius)))
            if patch.histogram()[255] >= 6:
                old.append((x, y, diameter))
        if len(old) > 1:
            return True

        tx, ty = self.target.center[0] * width, self.target.center[1] * height
        reach = .08 * width
        bounds = (max(0, int(tx - reach)), max(0, int(ty - reach)),
                  min(width, int(tx + reach)), min(height, int(ty + reach)))
        # Use full-screen width for the detector's cursor-size limits.
        cropped = after.crop(bounds)
        new = [(x + bounds[0], y + bounds[1], diameter)
               for x, y, diameter, color in vision._discs(cropped, width)
               if .025 * width <= diameter <= .065 * width
               and 90 <= min(color) <= max(color) <= 210 and max(color) - min(color) <= 25]
        if len(new) > 1:
            return True
        morphed = (len(highlights) == 1 and abs(highlights[0][0] - tx) <= reach
                   and abs(highlights[0][1] - ty) <= reach)
        if old and not (new or morphed):
            return True
        if old and new and abs(old[0][2] - new[0][2]) > .35 * old[0][2]:
            return True
        for x, y, diameter in old + new:
            radius = diameter / 2 + max(2, width / 180)
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=0)
        return mask.histogram()[255] >= 24
