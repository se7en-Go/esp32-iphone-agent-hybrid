"""Recheck an OCR target's visible context immediately before clicking."""

import io
import math

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
            and 40 <= min(color) <= max(color) <= 210 and max(color) - min(color) <= 25]


def _cursor_patch_matches(image, reference, center):
    width, height = image.size
    x, y = center
    cx, cy, radius = x * width, y * height, .036 * width
    box = (max(0, int(cx - radius)), max(0, int(cy - radius)),
           min(width, round(cx + radius + 1)), min(height, round(cy + radius + 1)))
    measured = reference if isinstance(reference, Image.Image) else _image(reference)
    if measured.size != image.size:
        return False
    red, green, blue = ImageChops.difference(measured.crop(box), image.crop(box)).split()
    change = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    return (sum(change.histogram()[56:]) < 3
            and not any(len(region) >= 6 for region in _regions(change, 25)))


def _home_slot_bounds(panel, width, height):
    from .assistive_vision import MENU_ICON_REGIONS

    pl, pt, pr, pb = panel
    hl, ht, hr, hb = MENU_ICON_REGIONS[-1]
    hb = min(0.96, hb + 0.025)
    return (
        (pl + hl * (pr - pl)) * width,
        (pt + ht * (pb - pt)) * height,
        (pl + hr * (pr - pl)) * width,
        (pt + hb * (pb - pt)) * height,
    )


class PointerLandingError(RuntimeError):
    """The pointer landing could not be verified before writing a click."""


class TargetChangedError(RuntimeError):
    """The final comparison rejected the action before it could be sent."""


class TextTargetGuard:
    """Ignore recognized cursor discs/outlines, never the entire target rectangle."""

    def __init__(self, screen, before: bytes, target, *, observation=None, navigation_context=None,
                 caret_scope=None):
        self.screen, self.target = screen, target
        self.generation = (observation if observation is not None else snapshot(screen)).generation
        self.navigation_context = navigation_context
        self.caret_scope = caret_scope
        if navigation_context is not None:
            from .navigation_context import NavigationContext

            if not isinstance(navigation_context, NavigationContext):
                raise ValueError("navigation requires internally observed system controls")
            navigation_context.validate_target(before, self.generation, target.center)
        self.before = _image(before)
        self._before_frame = before
        self.discs = _discs(self.before)

    def __call__(self, *, cursor=None, reference=None, home_highlight: bool = False):
        current = require_fresh(self.screen)
        if current.generation != self.generation:
            raise RuntimeError("目标观察后画面连接已变化，未发送点击；请重新观察。")
        try:
            after = _image(current.frame)
        except (OSError, ValueError) as exc:
            raise RuntimeError("点击前画面无效，未发送点击。") from exc
        if after.size != self.before.size or self._changed(
                after, cursor=cursor, reference=reference, after_frame=current.frame,
                home_highlight=home_highlight):
            raise TargetChangedError("指针定位期间目标或屏幕已变化，未发送点击；请重新观察。")

    def _changed(self, after, *, cursor=None, reference=None, after_frame=None, home_highlight: bool = False):
        width, height = after.size
        delta = ImageChops.difference(self.before, after)
        red, green, blue = delta.split()
        mask = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        retain_context_regions(mask, self.navigation_context.regions if self.navigation_context is not None else None)
        mask_caret_blink(self.before, after, mask, getattr(self.screen, "recent_frames", ()),
                         scope=self.caret_scope)
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
            measured = _image(observed)
            # Only suppress the cursor footprint while the write-time crop still
            # matches the frame in which its movement was verified.
            if not _cursor_patch_matches(after, measured, point):
                return True
            mask_cursor(point)
            context = self.navigation_context
            if context is not None and context.kind == "bubble" and mask.getbbox() is None:
                left, top, right, bottom = context.target.box
                x, y = point
                if left < x < right and top < y < bottom:
                    # The scoped anchor has no remaining change. A stationary
                    # round control near the measured cursor needs no masking.
                    return False
        if reference is not None:
            point, original, unoccluded = reference
            if (_cursor_patch_matches(self.before, original, point)
                    and _cursor_patch_matches(after, unoccluded, point)):
                mask_cursor(point)

        home_panel = None
        if home_highlight:
            home_panel = self._valid_home_highlight(after_frame)
            if home_panel is None:
                return True

        # Only a changed, cursor-sized disc may disappear from the original frame.
        old = []
        for x, y, diameter in self.discs:
            radius = diameter / 2
            patch = mask.crop((int(x - radius), int(y - radius), int(x + radius), int(y + radius)))
            if patch.histogram()[255] >= 6:
                old.append((x, y, diameter))
        if home_panel is not None:
            slot_left, slot_top, slot_right, slot_bottom = _home_slot_bounds(home_panel, width, height)
            old_outside = [
                (x, y, d) for x, y, d in old
                if not (slot_left <= x <= slot_right and slot_top <= y <= slot_bottom)]
            if len(old_outside) > 1:
                return True
        else:
            old_outside = old
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
               and 40 <= min(color) <= max(color) <= 210 and max(color) - min(color) <= 25]
        if len(new) > 1:
            return True
        faded_arrival = (self._arrived_faded_cursor(after, bounds, tx, ty, reach, cursor)
                         if old_outside and not new else None)
        morphed = (len(highlights) == 1 and abs(highlights[0][0] - tx) <= reach
                   and abs(highlights[0][1] - ty) <= reach)
        if old_outside and not (new or morphed or faded_arrival is not None or home_panel is not None):
            return True
        if old_outside and new and abs(old_outside[0][2] - new[0][2]) > .35 * old_outside[0][2]:
            return True
        if (old_outside and faded_arrival is not None
                and abs(old_outside[0][2] - faded_arrival[2]) > .45 * old_outside[0][2]):
            return True
        for x, y, diameter in old + new:
            radius = diameter / 2 + max(2, width / 180)
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=0)
        if cursor is not None and 24 <= mask.histogram()[255] < 48:
            self._mask_home_icon_edge_noise(after, mask, cursor[0], measured, delta)
        if home_panel is not None:
            margin = max(2, round(0.01 * width))
            draw.rectangle(
                (max(0, round(slot_left) - margin), max(0, round(slot_top) - margin),
                 min(width, round(slot_right) + margin), min(height, round(slot_bottom) + margin)),
                fill=0)
        return mask.histogram()[255] >= 24

    def _valid_home_highlight(self, after_frame):
        """Return the anchored menu panel iff this move produced a valid Home highlight.

        Only authorized when this tap's positioning move established a Home
        highlight landing (`home_highlight=True`). Pre-move checks and non-highlight
        moves leave `home_highlight=False` so unsolicited highlights still trip.
        """
        context = self.navigation_context
        if context is None or context.action != "home" or context.kind != "menu":
            return None
        if after_frame is None or self._before_frame is None or len(context.regions) != 1:
            return None
        from .assistive_vision import menu_home_highlight

        panel = context.regions[0]
        try:
            before_hl = menu_home_highlight(self._before_frame, panel)
            after_hl = menu_home_highlight(after_frame, panel)
        except (OSError, ValueError):
            return None
        if before_hl.highlighted or not after_hl.highlighted or after_hl.centre is None:
            return None
        left, top, right, bottom = context.target.box
        if not (left <= after_hl.centre[0] <= right and top <= after_hl.centre[1] <= bottom):
            return None
        return panel

    def _mask_home_icon_edge_noise(self, after, mask, point, measured, delta):
        """A measured Home icon may retain a few stationary JPEG edge specks.

        Keep the original target, label and panel. Neither a blue highlight nor
        a fresh measurement alone excuses a changed icon or a later edit.
        """
        context = self.navigation_context
        if context is None or context.action != "home" or context.kind != "menu":
            return
        left, top, right, bottom = context.target.box
        if not (left < point[0] < right and top < point[1] < bottom):
            return
        width, height = after.size
        margin = math.ceil(.012 * width)
        bounds = (math.floor(left*width)-margin, math.floor(top*height)-margin,
                  math.ceil(right*width)+margin, math.ceil(bottom*height)+margin)
        panel = tuple(value*(width if index % 2 == 0 else height)
                      for index, value in enumerate(context.regions[0]))
        if not (0 <= panel[0] <= bounds[0] < bounds[2] <= panel[2] <= width
                and 0 <= panel[1] <= bounds[1] < bounds[3] <= panel[3] <= height):
            return
        # The original measured cursor crop was checked above. Also retain its
        # surrounding halo: a new edit outside that circle disables all relief.
        stationary = ImageChops.difference(measured, after)
        cx, cy, radius = point[0]*width, point[1]*height, .036*width
        ImageDraw.Draw(stationary).ellipse((cx-radius, cy-radius, cx+radius, cy+radius), fill=0)
        if max(high for _, high in stationary.crop(bounds).getextrema()) > 6:
            return
        before_luma, after_luma = self.before.convert("L"), after.convert("L")
        candidates = []
        # Inspect whole components, never fragments cropped at the icon edge.
        for component in _regions(mask, 0):
            if len(component) > 6 or any(not (bounds[0] <= x < bounds[2] and bounds[1] <= y < bounds[3])
                                         for x, y in component):
                continue
            if any(max(delta.getpixel(pixel)) > 40 for pixel in component):
                continue
            if any(high-low < 40 for image in (before_luma, after_luma) for x, y in component
                   for low, high in [image.crop((x-1, y-1, x+2, y+2)).getextrema()]):
                continue
            candidates.extend(component)
        if 0 < len(candidates) <= 24:
            for pixel in candidates:
                mask.putpixel(pixel, 0)

    def _arrived_faded_cursor(self, after, bounds, tx, ty, reach, cursor):
        """Confirm a pre-move disc arrived over glyphs at the verified cursor, without masking."""
        if cursor is None:
            return None
        from .screen_compare import _faded_pointer_disc

        width, height = after.size
        px, py = cursor[0][0] * width, cursor[0][1] * height
        if abs(px - tx) > reach or abs(py - ty) > reach:
            return None
        red, green, blue = ImageChops.difference(self.before.crop(bounds), after.crop(bounds)).split()
        cdelta = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        matches = []
        for pts in _regions(cdelta, 10):
            if len(pts) < (.022 * width) ** 2:
                continue
            full_pts = [(x + bounds[0], y + bounds[1]) for x, y in pts]
            if _faded_pointer_disc(after, self.before, full_pts, width):
                xs, ys = zip(*full_pts, strict=True)
                cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
                diam = ((max(xs) - min(xs) + 1) + (max(ys) - min(ys) + 1)) / 2
                matches.append((cx, cy, diam))
        if len(matches) != 1:
            return None
        cx, cy, diam = matches[0]
        if math.hypot(cx - px, cy - py) > .02 * width:
            return None
        return cx, cy, diam
