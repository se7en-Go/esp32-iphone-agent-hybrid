"""Conservative visible Back targets; unknown navigation styles retain guarded gestures."""

import io
from dataclasses import dataclass
from statistics import median

from PIL import Image


@dataclass(frozen=True)
class BackTarget:
    center: tuple[float, float]
    box: tuple[float, float, float, float]


def _groups(points):
    remaining = set(points)
    while remaining:
        stack, group = [remaining.pop()], []
        while stack:
            x, y = stack.pop()
            group.append((x, y))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    adjacent = x+dx, y+dy
                    if adjacent in remaining:
                        remaining.remove(adjacent)
                        stack.append(adjacent)
        yield group


def _chevron(points, width, height):
    xs, ys = zip(*points, strict=True)
    left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
    w, h = right-left+1, bottom-top+1
    if not (left < .075*width and right < .14*width and .010*width <= w <= .06*width
            and .012*height <= h <= .04*height and .3 <= w/h <= .85
            and .12 <= len(points)/(w*h) <= .65):
        return None
    middle = (top+bottom)/2
    tips = [x for x, y in points if abs(y-middle) <= .15*h]
    upper = [x for x, y in points if y <= top+.22*h]
    lower = [x for x, y in points if y >= bottom-.22*h]
    if (not tips or not upper or not lower or min(median(upper), median(lower))-median(tips) < .3*w
            or abs(median(upper)-median(lower)) > .22*w):
        return None
    # Every arm must follow '<', not a K-shaped glyph with a vertical stem, a
    # filled triangle, circle, forward arrow or a background text fragment.
    errors = [abs((x-left)-(w-1)*abs(y-middle)/((h-1)/2)) for x, y in points]
    if sum(error <= .25*w for error in errors) < .78*len(errors):
        return None
    return left, top, right+1, bottom+1


def find_back_target(frame: bytes) -> BackTarget | None:
    """Recognize one blue iOS nav chevron plus a visible label to its right.

    There is no guessed coordinate or OCR-only fallback. Monochrome/custom
    controls are deliberately unsupported until their visual evidence is tested.
    """
    with Image.open(io.BytesIO(frame)) as original:
        image = original.convert("RGB")
    if image.width > image.height:
        return None
    if image.width > 720:
        image = image.resize((720, round(image.height*720/image.width)), Image.Resampling.BILINEAR)
    width, height = image.size
    pixels = image.load()
    blue = {(x, y) for y in range(round(.045*height), round(.15*height))
            for x in range(round(.38*width))
            if (lambda r, g, b: b >= 150 and g >= 65 and r <= 150 and b-r >= 60 and g-r >= 20)(*pixels[x, y])}
    chevrons = [box for group in _groups(blue) if (box := _chevron(group, width, height)) is not None]
    if len(chevrons) != 1:
        return None
    left, top, right, bottom = chevrons[0]
    arrow_height = bottom-top
    mid = (top+bottom)/2
    label = [(x, y) for x, y in blue if right+.005*width <= x <= min(.38*width, right+.24*width)
             and abs(y-mid) <= .65*arrow_height]
    if len(label) < .30*(right-left)*arrow_height:
        return None
    xs, ys = zip(*label, strict=True)
    if (min(xs)-right > .06*width or max(xs)-min(xs) < .025*width
            or not .55*arrow_height <= max(ys)-min(ys)+1 <= 1.35*arrow_height
            or abs((min(ys)+max(ys))/2-mid) > .20*arrow_height):
        return None
    # Bounds derive from the actual arrow and label strokes. A tiny vertical
    # margin gives the measured landing room while staying in this nav control.
    box = (left/width, (min(top, min(ys))-2)/height,
           (max(right, max(xs)+1)+1)/width, (max(bottom, max(ys)+1)+2)/height)
    return BackTarget(((box[0]+box[2])/2, (box[1]+box[3])/2), box)
