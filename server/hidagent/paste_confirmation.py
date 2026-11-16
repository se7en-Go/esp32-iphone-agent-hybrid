"""Corroborate host-observed paste effects after an iOS paste permission prompt."""

import io
import math
import re
import unicodedata
from statistics import median

from PIL import Image

from . import ocr

_ALLOW = {"allowpaste", "allow", "允许粘贴", "允许", "允許貼上", "允許"}
_DENY = {"don'tallowpaste", "don’tallowpaste", "don'tallow", "don’tallow",
         "不允许粘贴", "不允许", "不允許貼上", "不允許"}
_PASTE_FROM = re.compile(r"(?:wouldliketo|wantsto)pastefrom.+|想(?:要)?[从從].+(?:粘贴|貼上)")
_MIN_CONFIDENCE = .83


def _visible_text(text):
    # OCR may omit inter-word spaces. The caller verifies the original literal,
    # including whitespace, using the transaction digest independently.
    return "".join(unicodedata.normalize("NFC", text).split())


def _is_permission_prompt(boxes):
    modal = [item for item in boxes if item.conf >= _MIN_CONFIDENCE
             and .13 < item.center[0] < .87 and .27 < item.center[1] < .75]
    allow = [item for item in modal if ocr.normalize_text(item.text) in _ALLOW]
    deny = [item for item in modal if ocr.normalize_text(item.text) in _DENY]
    if len(allow) != 1 or len(deny) != 1:
        return False
    button_top = min(allow[0].box[1], deny[0].box[1])
    if abs(allow[0].center[1] - deny[0].center[1]) > .10:
        return False
    prompt = "".join(ocr.normalize_text(item.text) for item in modal
                     if button_top - .17 < item.center[1] < button_top)
    return _PASTE_FROM.search(prompt) is not None


def _inside(item, region):
    left, top, right, bottom = item.box
    return region[0] <= left and region[1] <= top and right <= region[2] and bottom <= region[3]


def _field_lines(items, region):
    left, top, right, bottom = region
    visible = []
    for item in items:
        if _inside(item, region):
            visible.append(item)
        else:
            x0, y0, x1, y1 = item.box
            if x0 < right and x1 > left and y0 < bottom and y1 > top:
                return None
    rows = []
    for item in sorted(visible, key=lambda item: item.center[1]):
        if item.conf < _MIN_CONFIDENCE:
            return None
        height = item.box[3] - item.box[1]
        if not rows or abs(rows[-1][0].center[1] - item.center[1]) > height / 2:
            rows.append([])
        rows[-1].append(item)
    return tuple(_visible_text("".join(item.text for item in sorted(row, key=lambda item: item.center[0])))
                 for row in rows)


def _same_context(before, after, region):
    def anchors(items):
        result = {}
        for item in items:
            left, top, right, bottom = item.box
            outside = right < region[0] or left > region[2] or bottom < region[1] or top > region[3]
            label = _visible_text(item.text)
            if outside and item.center[1] > .06 and item.conf >= _MIN_CONFIDENCE and len(label) >= 2:
                result.setdefault(label, []).append(item.center)
        return result

    original, current = anchors(before), anchors(after)
    unique = [points[0] for points in original.values() if len(points) == 1]
    if (original.keys() != current.keys() or len(unique) < 3
            or not any(y < region[1] for x, y in unique) or not any(y > region[3] for x, y in unique)):
        return False
    for label, locations in original.items():
        candidates = current[label]
        if len(locations) != len(candidates):
            return False
        for (x, y), (other_x, other_y) in zip(sorted(locations), sorted(candidates), strict=True):
            if abs(x - other_x) > .008 or abs(y - other_y) > .005:
                return False
    return True


def _edge_profile(pixels, horizontal, edge, start, stop, radius):
    result = []
    for fixed in range(edge - radius, edge + radius):
        deltas = []
        for along in range(start, stop):
            first = pixels[along, fixed] if horizontal else pixels[fixed, along]
            second = pixels[along, fixed + 1] if horizontal else pixels[fixed + 1, along]
            deltas.append(tuple(y - x for x, y in zip(first, second, strict=True)))
        result.extend(median(values) for values in zip(*deltas, strict=True))
    return result


def _same_field_outline(before, after, region):
    width, height = before.size
    left, top, right, bottom = (round(value * (width if index % 2 == 0 else height))
                                for index, value in enumerate(region))
    radius = max(3, min(8, round(min(width, height) * .008)))
    inset = 5 * radius
    if (min(left, top, width - right - 1, height - bottom - 1) < radius
            or min(right - left, bottom - top) <= 2 * inset):
        return False

    a, b = before.load(), after.load()
    for horizontal, edge, start, stop in ((True, top, left, right), (True, bottom, left, right),
                                          (False, left, top, bottom), (False, right, top, bottom)):
        first = _edge_profile(a, horizontal, edge, start + inset, stop - inset, radius)
        second = _edge_profile(b, horizontal, edge, start + inset, stop - inset, radius)
        if min(max(map(abs, first)), max(map(abs, second))) < 22:
            return False
        score = sum(x * y for x, y in zip(first, second, strict=True)) / (math.hypot(*first) * math.hypot(*second))
        if score < .98:
            return False
    return True


def _image(frame):
    with Image.open(io.BytesIO(frame)) as image:
        return image.convert("RGB")


async def verify_after_paste_permission(before, sheet, after, region, observed_text) -> bool:
    """Check three already authenticated frames and OCR of the original field."""
    if not before or not sheet or not after or before == after or sheet in (before, after) or region is None:
        return False
    try:
        region = ocr.validate_region(region)
        expected = tuple(_visible_text(line) for line in observed_text.splitlines())
        if not expected or not all(expected):
            return False
        original_image, sheet_image, current_image = (_image(frame) for frame in (before, sheet, after))
        if original_image.size != sheet_image.size or original_image.size != current_image.size:
            return False
        if not _is_permission_prompt(await ocr.read_text_async(sheet)):
            return False
        original = await ocr.read_text_async(before)
        current = await ocr.read_text_async(after)
        original_lines = _field_lines(original, region)
        return (not _is_permission_prompt(original) and not _is_permission_prompt(current)
                and original_lines is not None and original_lines != expected
                and _field_lines(current, region) == expected
                and _same_context(original, current, region)
                and _same_field_outline(original_image, current_image, region))
    except (ocr.OCRError, OSError, ValueError):
        return False
