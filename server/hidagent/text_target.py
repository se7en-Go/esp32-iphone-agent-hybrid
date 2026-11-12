"""Recheck an OCR target's visible context immediately before clicking."""

import io

from PIL import Image, ImageChops, ImageDraw

from . import vision


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


class TextTargetGuard:
    """Ignore recognized cursor discs, never the entire OCR target rectangle."""

    def __init__(self, screen, before: bytes, target):
        self.screen, self.target = screen, target
        self.before = _image(before)
        self.discs = _discs(self.before)

    def __call__(self):
        if not self.screen.live or not self.screen.frame:
            raise RuntimeError("点击前屏幕已断开，未发送点击。")
        try:
            after = _image(self.screen.frame)
        except (OSError, ValueError) as exc:
            raise RuntimeError("点击前画面无效，未发送点击。") from exc
        if after.size != self.before.size or self._changed(after):
            raise RuntimeError("指针定位期间目标或屏幕已变化，未发送点击；请重新观察。")

    def _changed(self, after):
        width, height = after.size
        delta = ImageChops.difference(self.before, after)
        red, green, blue = delta.split()
        mask = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        mask = mask.point(lambda value: 255 if value > 28 else 0)
        draw = ImageDraw.Draw(mask)
        target_top = getattr(self.target, "box", (0, self.target.center[1]))[1]
        if target_top >= .06:
            draw.rectangle((0, 0, width, round(.06 * height)), fill=0)

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
        if old and (not new or abs(old[0][2] - new[0][2]) > .35 * old[0][2]):
            return True
        for x, y, diameter in old + new:
            radius = diameter / 2 + max(2, width / 180)
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=0)
        return mask.histogram()[255] >= 24
