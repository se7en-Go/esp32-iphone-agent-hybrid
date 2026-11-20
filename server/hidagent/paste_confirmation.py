"""Corroborate host-observed paste effects after an iOS paste permission prompt."""

import asyncio
import hashlib
import io
import math
import re
import unicodedata
from collections import Counter
from statistics import median

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import assistive_vision, input_context, ocr, screen_compare

_ALLOW = {"allowpaste", "allow", "允许粘贴", "允许", "允許貼上", "允許"}
_DENY = {"don'tallowpaste", "don’tallowpaste", "don'tallow", "don’tallow",
         "不允许粘贴", "不允许", "不允許貼上", "不允許"}
_PASTE_FROM = re.compile(r"(?:wouldliketo|wantsto)pastefrom.+|想(?:要)?[从從].+(?:粘贴|貼上)")
_MIN_CONFIDENCE = .83
_VERTICAL_AA = ImageFilter.Kernel((3, 3), (0, 1, 0, 0, 2, 0, 0, 1, 0), scale=4)


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


def _boxes_overlap(first: tuple[float, float, float, float],
                   second: tuple[float, float, float, float]) -> bool:
    return (first[0] < second[2] and first[2] > second[0]
            and first[1] < second[3] and first[3] > second[1])


def _overlaps_box(item, box: tuple[float, float, float, float]) -> bool:
    return _boxes_overlap(item.box, box)


def _mask_normalized_box(draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float],
                         size: tuple[int, int]) -> None:
    width, height = size
    draw.rectangle((round(box[0] * width), round(box[1] * height),
                    round(box[2] * width), round(box[3] * height)), fill=0)


def _diff_has_unexpected_changes(
    before: bytes,
    after: bytes,
    diff: Image.Image,
    region: tuple[float, float, float, float],
    suggestion_bar: tuple[float, float] | None,
    extra=(),
    *,
    mask_before: Image.Image | None = None,
    mask_after: Image.Image | None = None,
    assistive_box: tuple[float, float, float, float] | None = None,
) -> bool:
    """Apply expected masks to `diff` and reject any remaining visual change."""
    draw = ImageDraw.Draw(diff)
    _mask_normalized_box(draw, region, diff.size)
    if suggestion_bar is not None:
        _mask_normalized_box(draw, (0., suggestion_bar[0], 1., suggestion_bar[1]), diff.size)
    for box in extra:
        _mask_normalized_box(draw, box, diff.size)
    if assistive_box is not None:
        _mask_normalized_box(draw, assistive_box, diff.size)
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
    highlight_before = before if mask_before is None else mask_before
    highlight_after = after if mask_after is None else mask_after
    screen_compare.mask_pointer_highlights(highlight_before, highlight_after, diff)
    screen_compare._mask_pointer_discs(before, after, diff, max_discs=2)
    screen_compare._mask_translucent_pointer_fade(highlight_before, highlight_after, diff)
    values = diff.tobytes()
    # Ignore <=4/255 AirPlay/JPEG quantization noise after full-screen sheet dim/undim;
    # sharp small edits still trip _regions(diff, 24) >= 6 and broad low-contrast shifts exceed .8.
    return (sum(value for value in values if value > 4) / len(values) > .8
            or any(len(points) >= 6 for points in screen_compare._regions(diff, 24)))


def _outside_visual_changed(before: bytes, after: bytes, region: tuple[float, float, float, float],
                            suggestion_bar: tuple[float, float] | None, extra=()) -> bool:
    """Verify no unintended UI outside the field, suggestion strip, clock, pointer, or bubble changed."""
    diff = screen_compare._difference(before, after, 360)
    if diff is None:
        return True
    return _diff_has_unexpected_changes(before, after, diff, region, suggestion_bar, extra)


def _context_anchors(items, region, *, suggestion_bar=None, ignored_boxes=()):
    selected = []
    for item in items:
        left, top, right, bottom = item.box
        outside = right < region[0] or left > region[2] or bottom < region[1] or top > region[3]
        if not outside or _in_suggestion_bar(item, suggestion_bar):
            continue
        if any(_overlaps_box(item, box) for box in ignored_boxes):
            continue
        label = _visible_text(item.text)
        if item.center[1] > .06 and item.conf >= _MIN_CONFIDENCE and len(label) >= 2:
            selected.append(item)
    return selected


def _stationary_anchors_match(before_items, after_items):
    def group(items):
        grouped = {}
        for item in items:
            grouped.setdefault(_visible_text(item.text), []).append(item.center)
        return grouped

    original, current = group(before_items), group(after_items)
    if original.keys() != current.keys():
        return None
    for label, locations in original.items():
        candidates = current[label]
        if len(locations) != len(candidates):
            return None
        for (x, y), (other_x, other_y) in zip(sorted(locations), sorted(candidates), strict=True):
            if abs(x - other_x) > .008 or abs(y - other_y) > .005:
                return None
    return [points[0] for points in original.values() if len(points) == 1]


def _same_context(before, after, region, *, suggestion_bar=None, ignored_boxes=()):
    original = _context_anchors(before, region, suggestion_bar=suggestion_bar, ignored_boxes=ignored_boxes)
    current = _context_anchors(after, region, suggestion_bar=suggestion_bar, ignored_boxes=ignored_boxes)
    unique = _stationary_anchors_match(original, current)
    return (unique is not None and len(unique) >= 3
            and any(y < region[1] for _, y in unique) and any(y > region[3] for _, y in unique))


def _match_shifted_anchors(before_items, after_items, *, dy=0., tol_x=.008, tol_y=.005):
    """Pair same-label anchors where `after` sits at `(before.x, before.y - dy)`."""
    candidates = []
    for b_idx, before_item in enumerate(before_items):
        label = _visible_text(before_item.text)
        bx, by = before_item.center
        for a_idx, after_item in enumerate(after_items):
            if _visible_text(after_item.text) != label:
                continue
            dx = abs(bx - after_item.center[0])
            dy_err = abs((by - dy) - after_item.center[1])
            if dx <= tol_x and dy_err <= tol_y:
                candidates.append((dx * dx + dy_err * dy_err, before_item.center[1], before_item.center[0],
                                   after_item.center[1], after_item.center[0], b_idx, a_idx))
    candidates.sort()
    used_before, used_after, pairs = set(), set(), []
    for *_, b_idx, a_idx in candidates:
        if b_idx in used_before or a_idx in used_after:
            continue
        used_before.add(b_idx)
        used_after.add(a_idx)
        pairs.append((before_items[b_idx], after_items[a_idx]))
    unmatched_before = [item for idx, item in enumerate(before_items) if idx not in used_before]
    unmatched_after = [item for idx, item in enumerate(after_items) if idx not in used_after]
    return pairs, unmatched_before, unmatched_after


def _header_viewport_bounds(before, after, region, current_region,
                            *, ignored_boxes=()) -> tuple[float, float] | None:
    """Locate the vertical split band between a fixed top header and the scrolled viewport."""
    header_cap = min(.20, current_region[1] - .12)
    if header_cap <= .07:
        return None
    top_before = [item for item in _context_anchors(before, region, ignored_boxes=ignored_boxes)
                  if item.center[1] < header_cap]
    top_after = [item for item in _context_anchors(after, current_region, ignored_boxes=ignored_boxes)
                 if item.center[1] < header_cap]
    matched, unmatched_before, unmatched_after = _match_shifted_anchors(top_before, top_after)
    first_moved = min((item.center[1] for item in (*unmatched_before, *unmatched_after)), default=header_cap)
    y_hi = min(header_cap, first_moved)
    header_pairs = [(b, a) for b, a in matched if b.center[1] < y_hi]
    counts = Counter(_visible_text(b.text) for b, _ in header_pairs)
    if not any(count == 1 for count in counts.values()):
        return None
    y_lo = max(max(b.box[3], a.box[3]) for b, a in header_pairs) + .002
    return (y_lo, y_hi) if y_lo < y_hi else None


def _scrolled_viewport_match(
    before: bytes,
    after: bytes,
    original_image: Image.Image,
    current_image: Image.Image,
    current_region: tuple[float, float, float, float],
    suggestion_bar: tuple[float, float] | None,
    extra,
    *,
    growth_delta: float,
    split_bounds: tuple[float, float],
    assistive_box: tuple[float, float, float, float] | None,
) -> tuple[float, float] | None:
    """Verify the chat viewport scrolled rigidly upward by `growth_delta` while header & keyboard stayed put."""
    if assistive_vision.find_panel(before) is not None or assistive_vision.find_panel(after) is not None:
        return None
    diff0 = screen_compare._difference(before, after, 360)
    if diff0 is None:
        return None
    w, h = diff0.size
    ow, oh = original_image.size
    y_lo_px = max(round(.06 * h), round(split_bounds[0] * h))
    y_bot_px = round(current_region[1] * h)
    y_hi_px = min(y_bot_px - round(.10 * h), round(split_bounds[1] * h))
    if not (y_lo_px < y_hi_px < y_bot_px):
        return None

    before_360 = original_image.resize((w, h), Image.Resampling.BILINEAR)
    after_360 = current_image.resize((w, h), Image.Resampling.BILINEAR)
    after_360_aa = after_360.filter(_VERTICAL_AA)

    def shifted_pair(d_orig: float):
        # Resample at native resolution before 360p downsampling and apply symmetric 1D vertical
        # anti-aliasing inside the viewport band to absorb fractional-pixel row phase residuals.
        a_full = original_image.transform((ow, oh), Image.Transform.AFFINE, (1, 0, 0, 0, 1, d_orig),
                                          resample=Image.Resampling.BICUBIC)
        a_360 = a_full.resize((w, h), Image.Resampling.BILINEAR)
        red, green, blue = ImageChops.difference(a_360.filter(_VERTICAL_AA), after_360_aa).split()
        sd = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        if assistive_box is not None:
            sdraw = ImageDraw.Draw(sd)
            _mask_normalized_box(sdraw, assistive_box, sd.size)
            _mask_normalized_box(
                sdraw,
                (assistive_box[0], max(0., assistive_box[1] - d_orig / oh),
                 assistive_box[2], max(0., assistive_box[3] - d_orig / oh)),
                sd.size,
            )
        return a_360, sd

    def eval_shift(d_orig: float):
        a_360, sd = shifted_pair(d_orig)
        hist = sd.crop((0, y_hi_px, w, y_bot_px)).histogram()
        outliers = sum(hist[25:])
        energy = sum(level * count for level, count in enumerate(hist[5:], 5))
        return (outliers, energy, abs(d_orig - growth_delta * oh)), d_orig, a_360, sd

    delta_px = growth_delta * oh
    max_err = max(4., .006 * oh)
    low = max(1., delta_px - max_err)
    high = max(low, delta_px + max_err)
    coarse_steps = sorted({low, high, *(d for k in range(-math.floor(max_err), math.floor(max_err) + 1)
                                         if low <= (d := delta_px + k) <= high)})
    best = min((eval_shift(d) for d in coarse_steps), key=lambda item: item[0])
    best_coarse = best[1]
    fine_steps = [min(high, max(low, best_coarse + k * .1)) for k in range(-9, 10) if k != 0]
    best = min((best, *(eval_shift(d) for d in fine_steps)), key=lambda item: item[0])
    _, best_d, a_360, sd = best

    d0_bytes = diff0.tobytes()
    sd_bytes = sd.tobytes()
    row0 = [sum(v for v in d0_bytes[y * w:(y + 1) * w] if v > 4) for y in range(y_lo_px, y_hi_px)]
    rows = [sum(v for v in sd_bytes[y * w:(y + 1) * w] if v > 4) for y in range(y_lo_px, y_hi_px)]
    pref, suf = 0, sum(rows)
    best_cost, best_y = None, y_lo_px
    for i in range(len(row0) + 1):
        cost = pref + suf
        if best_cost is None or cost < best_cost:
            best_cost, best_y = cost, y_lo_px + i
        if i < len(row0):
            pref += row0[i]
            suf -= rows[i]

    comp = diff0.copy()
    comp.paste(sd.crop((0, best_y, w, y_bot_px)), (0, best_y))
    comp_before_360 = before_360.copy()
    comp_before_360.paste(a_360.crop((0, best_y, w, y_bot_px)), (0, best_y))
    if _diff_has_unexpected_changes(before, after, comp, current_region, suggestion_bar, extra,
                                    mask_before=comp_before_360, mask_after=after_360,
                                    assistive_box=assistive_box):
        return None
    return best_y / h, best_d / oh


def _ignored_across_scroll(item, ignored_boxes, dy: float) -> bool:
    left, top, right, bottom = item.box
    shifted = (left, top + dy, right, bottom + dy)
    return any(_boxes_overlap(item.box, box) or _boxes_overlap(shifted, box) for box in ignored_boxes)


def _same_context_scrolled(before, after, region, current_region, *, split_y: float, scroll_delta: float,
                           suggestion_bar=None, ignored_boxes=()) -> bool:
    """Verify stationary header/lower anchors and rigidly scrolled chat-viewport anchors."""
    orig_anchors = _context_anchors(before, region, suggestion_bar=suggestion_bar)
    curr_anchors = _context_anchors(after, current_region, suggestion_bar=suggestion_bar)

    orig_header = [i for i in orig_anchors if i.center[1] < split_y
                   and not any(_overlaps_box(i, box) for box in ignored_boxes)]
    curr_header = [i for i in curr_anchors if i.center[1] < split_y
                   and not any(_overlaps_box(i, box) for box in ignored_boxes)]
    header_unique = _stationary_anchors_match(orig_header, curr_header)
    if header_unique is None or not header_unique:
        return False

    orig_lower = [i for i in orig_anchors if i.center[1] > region[3]
                  and not any(_overlaps_box(i, box) for box in ignored_boxes)]
    curr_lower = [i for i in curr_anchors if i.center[1] > region[3]
                  and not any(_overlaps_box(i, box) for box in ignored_boxes)]
    lower_unique = _stationary_anchors_match(orig_lower, curr_lower)
    if lower_unique is None or not lower_unique or len(header_unique) + len(lower_unique) < 3:
        return False

    before_vp = [i for i in orig_anchors if split_y <= i.center[1] <= region[1]
                 and not _ignored_across_scroll(i, ignored_boxes, -scroll_delta)]
    after_vp = [i for i in curr_anchors if split_y <= i.center[1] <= current_region[1]
                and not _ignored_across_scroll(i, ignored_boxes, scroll_delta)]
    matched_vp, unmatched_before, unmatched_after = _match_shifted_anchors(
        before_vp, after_vp, dy=scroll_delta, tol_x=.008, tol_y=.008)
    if len(matched_vp) >= 2 and abs(median(b.center[1] - a.center[1] for b, a in matched_vp) - scroll_delta) > .006:
        return False
    top_margin, bot_margin = .010, .012
    strict_before = {
        id(i) for i in before_vp
        if (i.box[1] - scroll_delta >= split_y + top_margin
            and i.box[3] <= region[1] - bot_margin
            and i.box[3] - scroll_delta <= current_region[1] - bot_margin)
    }
    strict_after = {
        id(i) for i in after_vp
        if (i.box[1] >= split_y + top_margin
            and i.box[3] <= current_region[1] - bot_margin
            and i.box[3] + scroll_delta <= region[1] - bot_margin)
    }
    return (not any(id(i) in strict_before for i in unmatched_before)
            and not any(id(i) in strict_after for i in unmatched_after))


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


_RETURN_KEYS = {"发送", "發送", "send", "return", "换行", "換行", "前往", "go", "搜索", "search", "完成", "done"}


def _field_span(image, left, right, bottom):
    """Vertical extent of a white input field whose bottom edge is near `bottom`.

    Rows count as field when enough of the row is page-white; glyph rows of
    pasted text keep part of the row white. Returns (top, bottom) or None.
    """
    w, h = image.size
    x0, x1 = max(0, round((left + .012) * w)), min(w, round((right - .012) * w))
    if x1 - x0 < round(.20 * w):
        return None
    xs = range(x0, x1, max(1, (x1 - x0) // 64))
    minimum = round(.025 * h)
    start, stop = min(h - 2, round((bottom + .010) * h)), max(round(.08 * h), round((bottom - .22) * h))
    rows = []
    for y in range(start, stop - 1, -1):
        if sum(sum(image.getpixel((x, y))) / 3 >= 251 for x in xs) / len(xs) >= .15:
            rows.append(y)
        elif len(rows) >= minimum:
            break
        else:
            rows.clear()
    if len(rows) < minimum:
        return None
    return min(rows) / h, (max(rows) + 1) / h


def _grown_field(original_image, current_image, region, suggestion_bar, *, assistive_box=None):
    """The same chat input field after it grew upward for multi-line text.

    Its bottom edge must stay put just above the located suggestion strip, its
    original top must match the observed field, and it may only grow upward by
    a bounded amount. Returns (current_region, extra_boxes, growth_delta) or None.
    """
    if suggestion_bar is None or not 0 <= suggestion_bar[0] - region[3] <= .04:
        return None
    before = _field_span(original_image, region[0], region[2], region[3])
    after = _field_span(current_image, region[0], region[2], region[3])
    if (before is None or after is None or abs(before[1] - after[1]) > .006
            or not region[1] - .012 <= before[0] <= region[1] + .020
            or not .015 <= before[0] - after[0] <= .16):
        return None
    current = (region[0], min(region[1], max(.06, after[0] - .006)), region[2], region[3])
    # The field's expand control appears left of the grown field, above the old top.
    expand = (.02, max(.06, after[0] - .008), region[0], before[0])
    # Right of the field the input bar's own background now covers the page. It
    # must be a flat bar colour; any content there is still a change. Skip only
    # a stationary AssistiveTouch floating button independently proven by both frames.
    band = (min(1., region[2] + .01), expand[1], 1., before[0])
    w, h = current_image.size
    bx0, by0, bx1, by1 = round(band[0] * w), round(band[1] * h), w, round(band[3] * h)
    total_band = max(0, bx1 - bx0) * max(0, by1 - by0)
    hole = None
    if assistive_box is not None and _boxes_overlap(band, assistive_box):
        hole = (round(assistive_box[0] * w), round(assistive_box[1] * h),
                round(assistive_box[2] * w), round(assistive_box[3] * h))
    pixels = [
        sum(current_image.getpixel((x, y))) / 3
        for y in range(by0, by1)
        for x in range(bx0, bx1)
        if not (hole and hole[0] <= x < hole[2] and hole[1] <= y < hole[3])
    ]
    if not pixels or max(pixels) - min(pixels) > 24:
        return None
    if hole is not None and len(pixels) < max(200, round(.20 * total_band)):
        return None
    return current, (expand, (region[2], band[1], 1., band[3])), before[0] - after[0]


def _return_key_box(original, current, suggestion_bar):
    """The keyboard's return/send key, whose tint changes once the field is non-empty."""
    def find(items):
        found = [item for item in items if item.conf >= _MIN_CONFIDENCE
                 and ocr.normalize_text(item.text) in _RETURN_KEYS
                 and .72 <= item.box[0] < item.box[2] <= 1
                 and max(suggestion_bar[1] + .14, .82) <= item.box[1] < item.box[3] <= .94]
        return found[0] if len(found) == 1 else None

    first, second = find(original), find(current)
    if (first is None or second is None
            or ocr.normalize_text(first.text) != ocr.normalize_text(second.text)
            or abs(first.center[0] - second.center[0]) > .008 or abs(first.center[1] - second.center[1]) > .005):
        return None
    x, y = (first.center[0] + second.center[0]) / 2, (first.center[1] + second.center[1]) / 2
    return (max(.74, x - .13), max(suggestion_bar[1] + .12, y - .034), min(1., x + .12), min(.94, y + .032))


_PAIR_CACHE: tuple[object, tuple, bool] | None = None


def _matches_wrapped_lines(added: tuple[str, ...], expected: tuple[str, ...]) -> bool:
    """Match visual OCR rows to logical lines, allowing display-level soft wrapping.

    One logical line may wrap across consecutive visual rows whose concatenation
    equals that exact logical line. Distinct logical lines (separated by a real
    newline) must still start on separate visual rows, and any missing, extra,
    altered, or duplicated text is rejected.
    """
    idx = 0
    for line in expected:
        if idx >= len(added):
            return False
        joined = added[idx]
        idx += 1
        while len(joined) < len(line) and idx < len(added) and line.startswith(joined):
            joined += added[idx]
            idx += 1
        if joined != line:
            return False
    return idx == len(added)


def _verify_before_after_sync(before, after, original, current, region, expected) -> bool:
    original_image, current_image = _image(before), _image(after)
    if original_image.size != current_image.size:
        return False
    if _is_permission_prompt(original) or _is_permission_prompt(current):
        return False
    suggestion_bar = _located_suggestion_bar(before, after, region)
    assistive_box = screen_compare.stationary_assistive_button_box(before, after, exclude=(region,))
    grown = _grown_field(original_image, current_image, region, suggestion_bar, assistive_box=assistive_box)
    current_region = grown[0] if grown else region
    if assistive_box is not None and _boxes_overlap(assistive_box, current_region):
        assistive_box = None
        if grown is not None:
            grown = _grown_field(original_image, current_image, region, suggestion_bar, assistive_box=None)
            current_region = grown[0] if grown else region
    original_lines = _field_lines(original, region)
    current_lines = _field_lines(current, current_region)
    # The pasted text must be new and appear after the existing field
    # text: the current lines end with the expected lines, and the lines
    # before them are exactly the lines that were there before the paste.
    # This covers an empty field and appending after existing borderless
    # body text, and still rejects text that was already present.
    appended = (original_lines is not None and current_lines is not None
                and len(current_lines) >= len(original_lines)
                and current_lines[:len(original_lines)] == original_lines
                and _matches_wrapped_lines(current_lines[len(original_lines):], expected))
    if not appended:
        return False
    if grown is None:
        if not _same_field_outline(original_image, current_image, region):
            return False
        if _same_context(original, current, region):
            return True
    elif len(current_lines) < 2:
        return False  # only multi-line text grows the field
    if suggestion_bar is None and (grown is not None or assistive_box is None):
        return False
    extra = []
    if grown is not None:
        extra.extend(grown[1])
    if original_lines == () and suggestion_bar is not None:
        key_box = _return_key_box(original, current, suggestion_bar)
        if key_box is not None:
            extra.append(key_box)
    ignored = (assistive_box,) if assistive_box is not None else ()
    if (_same_context(original, current, current_region,
                      suggestion_bar=suggestion_bar, ignored_boxes=ignored)
            and not _outside_visual_changed(before, after, current_region, suggestion_bar, extra)):
        return True
    if grown is None:
        return False
    split_bounds = _header_viewport_bounds(original, current, region, current_region, ignored_boxes=ignored)
    if split_bounds is None:
        return False
    scrolled = _scrolled_viewport_match(
        before, after, original_image, current_image, current_region, suggestion_bar, extra,
        growth_delta=grown[2], split_bounds=split_bounds, assistive_box=assistive_box,
    )
    if scrolled is None:
        return False
    split_y, scroll_delta = scrolled
    return _same_context_scrolled(
        original, current, region, current_region,
        split_y=split_y, scroll_delta=scroll_delta, suggestion_bar=suggestion_bar, ignored_boxes=ignored,
    )


async def _verify_before_after_uncached(read_fn, before, after, region, expected) -> bool:
    original = await read_fn(before)
    current = await read_fn(after)
    return await asyncio.to_thread(_verify_before_after_sync, before, after, original, current, region, expected)


async def _verify_before_after(before, after, region, expected) -> bool:
    global _PAIR_CACHE
    read_fn = ocr.read_text_async
    key = (hashlib.sha256(before).digest(), hashlib.sha256(after).digest(), region, expected)
    cached = _PAIR_CACHE
    if cached is not None and cached[0] is read_fn and cached[1] == key:
        return cached[2]
    verified = await _verify_before_after_uncached(read_fn, before, after, region, expected)
    _PAIR_CACHE = (read_fn, key, verified)
    return verified


def _expected_lines(observed_text: str) -> tuple[str, ...]:
    # Blank lines (e.g. a leading newline when appending after existing
    # text) have no OCR row, so they cannot be verified visually; the
    # transaction digest already pins the exact intended text. Compare
    # only the visible lines.
    return tuple(line for line in (_visible_text(line) for line in observed_text.splitlines()) if line)


def _same_frame_sizes(*frames: bytes) -> bool:
    sizes = set()
    for frame in frames:
        with Image.open(io.BytesIO(frame)) as image:
            sizes.add(image.size)
    return len(sizes) == 1


async def verify_before_after_for_permission(before, after, region, observed_text) -> bool:
    """Verify before/after paste state alone before scanning permission-sheet candidates."""
    if not before or not after or before == after or region is None:
        return False
    try:
        region = ocr.validate_region(region)
        expected = _expected_lines(observed_text)
        if not expected:
            return False
        return await _verify_before_after(before, after, region, expected)
    except (ocr.OCRError, OSError, ValueError):
        return False


async def verify_after_paste_permission(before, sheet, after, region, observed_text) -> bool:
    """Check three already authenticated frames and OCR of the original field."""
    if not before or not sheet or not after or before == after or sheet in (before, after) or region is None:
        return False
    try:
        region = ocr.validate_region(region)
        expected = _expected_lines(observed_text)
        if not expected:
            return False
        if not await asyncio.to_thread(_same_frame_sizes, before, sheet, after):
            return False
        if not await _verify_before_after(before, after, region, expected):
            return False
        return _is_permission_prompt(await ocr.read_text_async(sheet))
    except (ocr.OCRError, OSError, ValueError):
        return False


_default_verify_after_paste_permission = verify_after_paste_permission
