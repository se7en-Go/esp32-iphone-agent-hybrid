"""Corroborate host-observed paste effects after an iOS paste permission prompt."""

import io
import math
import re
import unicodedata
from statistics import median

from PIL import Image, ImageDraw

from . import assistive_vision, input_context, ocr, screen_compare

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


def _located_suggestion_bar(before: bytes, after: bytes,
                            region: tuple[float, float, float, float]) -> tuple[float, float] | None:
    """Locate the shared QuickType suggestion strip above the software keyboard."""
    before_bar = input_context.keyboard_suggestion_bar(before)
    after_bar = input_context.keyboard_suggestion_bar(after)
    if (before_bar is None or after_bar is None
            or abs(before_bar[0] - after_bar[0]) > .02
            or abs(before_bar[1] - after_bar[1]) > .02):
        return None
    suggestion_bar = min(before_bar[0], after_bar[0]), max(before_bar[1], after_bar[1])
    return None if region[3] > suggestion_bar[0] else suggestion_bar


def _in_suggestion_bar(item, suggestion_bar: tuple[float, float] | None) -> bool:
    if suggestion_bar is None:
        return False
    bar_top, bar_bottom = suggestion_bar
    _, top, _, bottom = item.box
    return bar_top <= item.center[1] <= bar_bottom and top >= bar_top - .005 and bottom <= bar_bottom + .008


def _mask_normalized_box(draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float],
                         size: tuple[int, int]) -> None:
    width, height = size
    draw.rectangle((round(box[0] * width), round(box[1] * height),
                    round(box[2] * width), round(box[3] * height)), fill=0)


def _outside_visual_changed(before: bytes, after: bytes, region: tuple[float, float, float, float],
                            suggestion_bar: tuple[float, float]) -> bool:
    """Verify no unintended UI outside the field, suggestion strip, clock, pointer, or bubble changed."""
    diff = screen_compare._difference(before, after, 360)
    if diff is None:
        return True
    draw = ImageDraw.Draw(diff)
    _mask_normalized_box(draw, region, diff.size)
    _mask_normalized_box(draw, (0., suggestion_bar[0], 1., suggestion_bar[1]), diff.size)
    for frame in (before, after):
        if assistive_vision.find_panel(frame) is not None:
            return True
        button = assistive_vision.find_button_target(frame)
        if button is not None:
            _, (left, top, right, bottom) = button
            pad = .015
            _mask_normalized_box(
                draw,
                (max(0., left - pad), max(0., top - pad), min(1., right + pad), min(1., bottom + pad)),
                diff.size,
            )
    screen_compare.mask_caret_blink(before, after, diff, ())
    screen_compare.mask_clock_ticks(before, after, diff)
    screen_compare.mask_pointer_highlights(before, after, diff)
    screen_compare._mask_pointer_discs(before, after, diff, max_discs=2)
    screen_compare._mask_translucent_pointer_fade(before, after, diff)
    values = diff.tobytes()
    return sum(values) / len(values) > .8 or any(len(points) >= 6 for points in screen_compare._regions(diff, 24))


def _same_context(before, after, region, *, suggestion_bar=None):
    def anchors(items):
        result = {}
        for item in items:
            left, top, right, bottom = item.box
            outside = right < region[0] or left > region[2] or bottom < region[1] or top > region[3]
            if outside and _in_suggestion_bar(item, suggestion_bar):
                continue
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
        if max(map(abs, first)) < 22:
            # No visible border on this side in the original frame (a
            # borderless field): nothing to compare here.
            continue
        if max(map(abs, second)) < 22:
            return False
        score = sum(x * y for x, y in zip(first, second, strict=True)) / (math.hypot(*first) * math.hypot(*second))
        if score < .98:
            return False
    # A fully borderless field has no outline to compare; the context and
    # line checks still guard the field identity.
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
        # Blank lines (e.g. a leading newline when appending after existing
        # text) have no OCR row, so they cannot be verified visually; the
        # transaction digest already pins the exact intended text. Compare
        # only the visible lines.
        expected = tuple(line for line in (_visible_text(line) for line in observed_text.splitlines()) if line)
        if not expected:
            return False
        original_image, sheet_image, current_image = (_image(frame) for frame in (before, sheet, after))
        if original_image.size != sheet_image.size or original_image.size != current_image.size:
            return False
        if not _is_permission_prompt(await ocr.read_text_async(sheet)):
            return False
        original = await ocr.read_text_async(before)
        current = await ocr.read_text_async(after)
        original_lines = _field_lines(original, region)
        current_lines = _field_lines(current, region)
        # The pasted text must be new and appear after the existing field
        # text: the current lines end with the expected lines, and the lines
        # before them are exactly the lines that were there before the paste.
        # This covers an empty field and appending after existing borderless
        # body text, and still rejects text that was already present.
        appended = (original_lines is not None and current_lines is not None
                    and len(current_lines) >= len(expected)
                    and current_lines[-len(expected):] == expected
                    and current_lines[:len(current_lines) - len(expected)] == original_lines)
        if (_is_permission_prompt(original) or _is_permission_prompt(current)
                or not appended or not _same_field_outline(original_image, current_image, region)):
            return False
        if _same_context(original, current, region):
            return True
        suggestion_bar = _located_suggestion_bar(before, after, region)
        return (suggestion_bar is not None
                and _same_context(original, current, region, suggestion_bar=suggestion_bar)
                and not _outside_visual_changed(before, after, region, suggestion_bar))
    except (ocr.OCRError, OSError, ValueError):
        return False
