"""Screen-content comparisons, separate from pointer/calibration detection."""

import io

from PIL import Image, ImageChops, ImageDraw

from .vision import _discs

STATUS_HEIGHT = 0.06
POINTER_RADIUS = 0.045  # fraction of width; includes JPEG edges and a small highlight


def _difference(before: bytes | None, after: bytes | None, width: int):
    if not before or not after:
        return None
    with Image.open(io.BytesIO(before)) as first, Image.open(io.BytesIO(after)) as second:
        if first.size != second.size:
            return None
        w = min(width, first.width)
        h = max(1, round(first.height * w / first.width))
        a = first.convert("RGB").resize((w, h), Image.Resampling.BILINEAR)
        b = second.convert("RGB").resize((w, h), Image.Resampling.BILINEAR)
    red, green, blue = ImageChops.difference(a, b).split()
    diff = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    draw = ImageDraw.Draw(diff)
    draw.rectangle((0, 0, w, int(STATUS_HEIGHT * h)), fill=0)
    return diff


def _mask_locations(diff: Image.Image, ignore: tuple, hover: tuple) -> None:
    w, h = diff.size
    draw = ImageDraw.Draw(diff)
    for locations, fraction in ((ignore, POINTER_RADIUS), (hover, 0.12)):
        radius = fraction * w
        for x, y in locations:
            cx, cy = x * w, y * h
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=0)


def _regions(diff: Image.Image, threshold: int):
    w, _ = diff.size
    hot = {(i % w, i // w) for i, value in enumerate(diff.tobytes()) if value > threshold}
    while hot:
        stack = [hot.pop()]
        points = []
        while stack:
            x, y = stack.pop()
            points.append((x, y))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbor = x + dx, y + dy
                    if neighbor in hot:
                        hot.remove(neighbor)
                        stack.append(neighbor)
        yield points


def _mask_pointer_discs(before: bytes, after: bytes, diff: Image.Image) -> None:
    """Mask visible gray discs in delayed move frames, without hiding other small edits."""
    w, h = diff.size
    boxes = []
    for points in _regions(diff, 25):
        if len(points) < 6:
            continue
        xs, ys = zip(*points, strict=True)
        x0, y0, x1, y1 = min(xs), min(ys), max(xs) + 1, max(ys) + 1
        if x1 - x0 <= 0.1 * w and y1 - y0 <= 0.1 * w:
            margin = round(0.04 * w)
            boxes.append((max(0, x0 - margin), max(0, y0 - margin),
                          min(w, x1 + margin), min(h, y1 + margin)))
    if not boxes or len(boxes) > 12:
        return
    draw = ImageDraw.Draw(diff)
    for jpeg in (before, after):
        with Image.open(io.BytesIO(jpeg)) as original:
            image = original.convert("RGB").resize((w, h), Image.Resampling.BILINEAR)
        for box in boxes:
            for x, y, diameter, color in _discs(image.crop(box), w):
                if not (0.03 * w <= diameter <= 0.06 * w and max(color) - min(color) <= 20
                        and 100 <= sum(color) / 3 <= 200):
                    continue
                x, y = x + box[0], y + box[1]
                radius = diameter / 2 + 1
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=0)


def _mask_pointer_highlights(before: bytes, after: bytes, diff: Image.Image,
                             unmasked: Image.Image | None = None) -> None:
    """Ignore neutral, hollow, rounded hover outlines, including an unknown old hover.

    Mask only the outline and its JPEG edge; content inside the control remains visible.
    Solid patches, colored outlines and changes to a control's label are not highlights.
    """
    w, _ = diff.size
    candidates = []
    for points in _regions(unmasked if unmasked is not None else diff, 24):
        xs, ys = zip(*points, strict=True)
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        if not (0.08 * w <= min(bw, bh) <= max(bw, bh) <= 0.26 * w
                and 0.65 <= bw / bh <= 1.55 and 0.04 <= len(points) / (bw * bh) <= 0.35):
            continue
        if any((abs(x - (x0 + x1) / 2) < bw * .25
                and abs(y - (y0 + y1) / 2) < bh * .25)
               or (min(x - x0, x1 - x) < bw * .07 and min(y - y0, y1 - y) < bh * .07)
               for x, y in points):
            continue  # unchanged center and rounded corners
        if min(sum(x < x0 + bw * .15 for x, _ in points),
               sum(x > x1 - bw * .15 for x, _ in points),
               sum(y < y0 + bh * .15 for _, y in points),
               sum(y > y1 - bh * .15 for _, y in points)) < min(bw, bh) * .4:
            continue
        candidates.append((points, ((x0 + x1) / 2, (y0 + y1) / 2)))
    if not candidates or len(candidates) > 2:
        return
    draw = ImageDraw.Draw(diff)
    for jpeg in (before, after):
        with Image.open(io.BytesIO(jpeg)) as original:
            image = original.convert("RGB").resize(diff.size, Image.Resampling.BILINEAR)
        for points, (cx, cy) in candidates:
            colors = [image.getpixel(point) for point in points]
            levels = sorted(sum(color) / 3 for color in colors)
            if (sum(max(color) - min(color) <= 18 for color in colors) < .95 * len(colors)
                    or not 110 <= levels[len(levels) // 2] <= 225
                    or levels[-len(levels) // 10] - levels[len(levels) // 10] > 40):
                continue
            contrast = [0, 0]
            offset = max(2, round(w * .012))
            for (x, y), color in zip(points, colors, strict=True):
                dx, dy = x - cx, y - cy
                scale = max(abs(dx), abs(dy))
                for side, direction in enumerate((-1, 1)):
                    nx, ny = round(x + direction * offset * dx / scale), round(y + direction * offset * dy / scale)
                    if 0 <= nx < image.width and 0 <= ny < image.height:
                        contrast[side] += abs(sum(image.getpixel((nx, ny))) - sum(color)) / 3 >= 12
            if min(contrast) < len(points) * .65:
                continue  # gray background alone is not a visible outline
            for x, y in points:
                draw.rectangle((x - 1, y - 1, x + 1, y + 1), fill=0)


def observation_changed(before: bytes | None, after: bytes | None) -> bool:
    """Reject a different screen while tolerating clock and up to two pointer-sized changes.

    Recognized hover outlines and small compact discs may fade or move. Other larger
    regions, several edits, scrolling and broad low-contrast changes invalidate it.
    """
    if before and before == after:
        return False
    diff = _difference(before, after, 180)
    if diff is None:
        return True
    values = diff.tobytes()
    if sum(values) / len(values) > 3:
        return True  # broad transitions need no outline search
    _mask_pointer_highlights(before, after, diff)
    values = diff.tobytes()
    if sum(values) / len(values) > 0.8:
        return True
    w, _ = diff.size
    regions = 0
    for points in _regions(diff, 24):
        if len(points) < 3:
            continue  # isolated compression noise
        xs, ys = zip(*points, strict=True)
        if max(xs) - min(xs) + 1 > 0.08 * w or max(ys) - min(ys) + 1 > 0.08 * w:
            return True
        regions += 1
        if regions > 2:
            return True
    return False


def effect_changed(before: bytes | None, after: bytes | None, ignore: tuple = (), *, hover: tuple = ()) -> bool:
    """See even a small content change, excluding status bar and known pointer locations.

    `hover` also masks larger iOS button highlights around tap targets. A response wholly
    beneath those masks is inconclusive. Untracked hover outlines are recognized in either
    frame. Visual change is not proof of the intended effect.
    """
    if before and before == after:
        return False
    unmasked = _difference(before, after, 360)
    if unmasked is None:
        return True
    diff = unmasked.copy()
    _mask_locations(diff, ignore, hover)
    values = diff.tobytes()
    if sum(values) / len(values) > 3:  # a screen transition cannot be just a pair of small discs
        return True
    # A known target can overlap an unknown old hover; recognize the complete outline.
    _mask_pointer_highlights(before, after, diff, unmasked)
    _mask_pointer_discs(before, after, diff)
    values = diff.tobytes()
    return sum(values) / len(values) > 0.8 or any(len(region) >= 6 for region in _regions(diff, 25))
