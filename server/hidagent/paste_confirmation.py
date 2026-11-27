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


def permission_prompt(boxes):
    """Return the prompt and both OCR buttons from the same system paste sheet."""
    modal = [item for item in boxes if item.conf >= _MIN_CONFIDENCE
             and .13 < item.center[0] < .87 and .27 < item.center[1] < .75]
    allow = [item for item in modal if ocr.normalize_text(item.text) in _ALLOW]
    deny = [item for item in modal if ocr.normalize_text(item.text) in _DENY]
    if len(allow) != 1 or len(deny) != 1:
        return None
    button_top = min(allow[0].box[1], deny[0].box[1])
    if abs(allow[0].center[1] - deny[0].center[1]) > .10:
        return None
    prompt = "".join(ocr.normalize_text(item.text) for item in modal
                     if button_top - .17 < item.center[1] < button_top)
    return (prompt, allow[0], deny[0]) if _PASTE_FROM.search(prompt) else None


def _is_permission_prompt(boxes):
    return permission_prompt(boxes) is not None


def _inside(item, region):
    left, top, right, bottom = item.box
    return region[0] <= left and region[1] <= top and right <= region[2] and bottom <= region[3]


def _filter_stationary_field_icons(
    items,
    before_image: Image.Image,
    after_image: Image.Image,
    region: tuple[float, float, float, float],
    *,
    expected: tuple[str, ...] = (),
):
    """Drop an isolated stationary bottom-right UI icon hallucination (e.g. mic chrome)."""
    if before_image.size != after_image.size or region[2] - region[0] > .70:
        return items
    w, h = before_image.size
    field_items = [item for item in items if _inside(item, region)]
    drop_indices: set[int] = set()
    for idx, item in enumerate(items):
        if not _inside(item, region):
            continue
        left, top, right, bottom = item.box
        text = _visible_text(item.text)
        if not (
            left >= region[2] - .12
            and top >= region[3] - .06
            and 0 < right - left <= .07
            and 0 < bottom - top <= .055
            and 0 < len(text) <= 2
            and not all(ch in _TERMINAL_CJK_PUNCT for ch in text)
            and not (expected and expected[-1].endswith(text))
            and not any(_boxes_overlap(item.box, other.box) for other in field_items if other is not item)
        ):
            continue
        x0 = max(0, min(w, round(left * w)))
        y0 = max(0, min(h, round(top * h)))
        x1 = max(0, min(w, round(right * w)))
        y1 = max(0, min(h, round(bottom * h)))
        if x1 <= x0 or y1 <= y0:
            continue
        b_crop = before_image.crop((x0, y0, x1, y1))
        gray = b_crop.convert("L").tobytes()
        if not gray or max(gray) - min(gray) < 40:
            continue
        b_raw = b_crop.tobytes()
        a_raw = after_image.crop((x0, y0, x1, y1)).tobytes()
        if not b_raw or len(b_raw) != len(a_raw):
            continue
        diffs = [abs(a - b) for a, b in zip(b_raw, a_raw, strict=True)]
        if sum(diffs) / len(diffs) <= 2.5 and max(diffs) <= 18:
            drop_indices.add(idx)
    if len(drop_indices) != 1:
        return items
    return [item for idx, item in enumerate(items) if idx not in drop_indices]


def _field_rows(items, region):
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
    return [sorted(row, key=lambda item: item.center[0]) for row in rows]


def _field_lines(items, region):
    rows = _field_rows(items, region)
    if rows is None:
        return None
    return tuple(_visible_text("".join(item.text for item in row)) for row in rows)


def _has_stationary_corner_icon(before_image: Image.Image, region: tuple[float, float, float, float]) -> bool:
    """True when the bottom-right composer corner already contains high-contrast UI chrome (e.g. mic)."""
    if region[2] - region[0] > .70:
        return False
    w, h = before_image.size
    slot = (
        max(0, min(w, round((region[2] - .11) * w))),
        max(0, min(h, round((region[3] - .045) * h))),
        max(0, min(w, round((region[2] - .01) * w))),
        max(0, min(h, round((region[3] - .005) * h))),
    )
    if slot[2] <= slot[0] or slot[3] <= slot[1]:
        return False
    gray = before_image.crop(slot).convert("L").tobytes()
    return bool(gray) and min(gray) <= 160 and max(gray) - min(gray) >= 60


def _corner_faded_rows(
    rows,
    before_image: Image.Image,
    after_image: Image.Image,
    region: tuple[float, float, float, float],
) -> dict[int, str]:
    """Return lower-right row indices whose rightmost glyph was physically bleached by chrome fade."""
    if (not rows or len(rows) < 2 or before_image.size != after_image.size
            or not _has_stationary_corner_icon(before_image, region)):
        return {}
    before_gray = before_image.convert("L")
    gray = after_image.convert("L")
    w, h = gray.size
    faded: dict[int, str] = {}
    for r_idx in (len(rows) - 2, len(rows) - 1):
        row = rows[r_idx]
        row_text = _visible_text("".join(item.text for item in row))
        if len(row_text) < 4:
            continue
        rx0 = min(item.box[0] for item in row)
        ry0 = min(item.box[1] for item in row)
        rx1 = max(item.box[2] for item in row)
        ry1 = max(item.box[3] for item in row)
        if ry1 < region[3] - .065:
            continue
        x0 = max(0, min(w, round(rx0 * w)))
        y0 = max(0, min(h, round(ry0 * h)))
        x1 = max(0, min(w, round(rx1 * w)))
        y1 = max(0, min(h, round(ry1 * h)))
        if x1 <= x0 or y1 <= y0:
            continue
        cw = max(12, round((x1 - x0) / len(row_text)))
        mid_y = (y0 + y1) // 2
        if mid_y >= y1:
            continue
        if rx1 >= region[2] - .08 and x1 - cw > x0:
            left_min = min(gray.crop((x0, y0, x1 - cw, y1)).tobytes())
            r_bot_min = min(gray.crop((max(x0, x1 - cw), mid_y, x1, y1)).tobytes())
            if left_min <= 35 and r_bot_min >= 95:
                faded[r_idx] = "sub"
                continue
        if region[2] - .13 <= rx1 < region[2] - .07:
            row_min = min(gray.crop((x0, y0, x1, y1)).tobytes())
            cell_x1 = min(w, round(region[2] * w), x1 + cw + 6)
            if row_min <= 35 and cell_x1 >= x1 + 8:
                before_min = min(before_gray.crop((x1, y0, cell_x1, y1)).tobytes())
                after_min = min(gray.crop((x1, y0, cell_x1, y1)).tobytes())
                after_bot_min = min(gray.crop((x1, mid_y, cell_x1, y1)).tobytes())
                if before_min >= 225 and after_min <= 215 and before_min - after_min >= 40 and after_bot_min >= 95:
                    faded[r_idx] = "omit"
    return faded


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
    x0 = max(0, min(width - 1, math.floor(box[0] * width)))
    y0 = max(0, min(height - 1, math.floor(box[1] * height)))
    x1 = max(x0, min(width - 1, math.ceil(box[2] * width) - 1))
    y1 = max(y0, min(height - 1, math.ceil(box[3] * height) - 1))
    draw.rectangle((x0, y0, x1, y1), fill=0)


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
    screen_compare._mask_flat_background_pointer_disc(highlight_before, highlight_after, diff)
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


def _context_anchors(items, region, *, suggestion_bar=None, ignored_boxes=(), min_y: float = .06):
    selected = []
    for item in items:
        left, top, right, bottom = item.box
        outside = right < region[0] or left > region[2] or bottom < region[1] or top > region[3]
        if not outside or _in_suggestion_bar(item, suggestion_bar):
            continue
        if any(_overlaps_box(item, box) for box in ignored_boxes):
            continue
        label = _visible_text(item.text)
        if item.center[1] > min_y and item.conf >= _MIN_CONFIDENCE and len(label) >= 2:
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


def _same_context(before, after, region, *, suggestion_bar=None, ignored_boxes=(), min_y: float = .06):
    original = _context_anchors(
        before, region, suggestion_bar=suggestion_bar, ignored_boxes=ignored_boxes, min_y=min_y)
    current = _context_anchors(
        after, region, suggestion_bar=suggestion_bar, ignored_boxes=ignored_boxes, min_y=min_y)
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
                            *, ignored_boxes=(), min_y: float = .06) -> tuple[float, float] | None:
    """Locate the vertical split band between a fixed top header and the scrolled viewport."""
    header_cap = min(.20, current_region[1] - .12)
    if header_cap <= .07:
        return None
    top_before = [item for item in _context_anchors(before, region, ignored_boxes=ignored_boxes, min_y=min_y)
                  if item.center[1] < header_cap]
    top_after = [item for item in _context_anchors(after, current_region, ignored_boxes=ignored_boxes, min_y=min_y)
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


def _rgb_difference(first: Image.Image, second: Image.Image) -> Image.Image:
    red, green, blue = ImageChops.difference(first, second).split()
    return ImageChops.lighter(ImageChops.lighter(red, green), blue)


def _scroll_split(before: Image.Image, shifted: Image.Image, after: Image.Image,
                  y_lo: int, y_hi: int, fixed_visible: Image.Image, moved_visible: Image.Image) -> int:
    """Find one native-pixel boundary between the fixed and shifted references."""
    width = before.width
    band = (0, y_lo, width, y_hi)
    actual = after.crop(band)
    fixed = ImageChops.multiply(_rgb_difference(before.crop(band), actual), fixed_visible.crop(band)).tobytes()
    moved = ImageChops.multiply(_rgb_difference(shifted.crop(band), actual), moved_visible.crop(band)).tobytes()
    cost, best_cost, best_y = 0, 0, y_lo
    for offset in range(0, len(fixed), width):
        cost += sum(v for v in fixed[offset:offset + width] if v > 4)
        cost -= sum(v for v in moved[offset:offset + width] if v > 4)
        if cost < best_cost:
            best_cost, best_y = cost, y_lo + offset // width + 1
    return best_y


def _fractional_scroll_row(fixed: Image.Image, moved: Image.Image, actual: Image.Image,
                           visible: Image.Image, hard: Image.Image):
    """Fit one coverage fraction shared by the full-width source boundary row."""
    visibility = visible.tobytes()
    numerator, denominator = 0, 0
    channels = zip(fixed.tobytes(), moved.tobytes(), actual.tobytes(), strict=True)
    for index, (original, shifted, observed) in enumerate(channels):
        if visibility[index // 3]:
            delta = shifted - original
            numerator += delta * (observed - original)
            denominator += delta * delta
    if not 0 < numerator < denominator:
        return None

    blended = Image.blend(fixed, moved, numerator / denominator)
    hard_error = ImageChops.multiply(_rgb_difference(hard, actual), visible)
    blend_error = ImageChops.multiply(_rgb_difference(blended, actual), visible)
    improvement = sum(hard_error.tobytes()) - sum(blend_error.tobytes())
    if improvement <= 0:
        return None
    return improvement, blended


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
    y_lo = max(round(.06 * oh), math.ceil(split_bounds[0] * oh))
    y_hi = min(round((current_region[1] - .10) * oh), math.floor(split_bounds[1] * oh))
    y_bot_px = round(current_region[1] * h)
    y_hi_px = math.ceil(y_hi * h / oh)
    if not (y_lo < y_hi and y_hi_px < y_bot_px):
        return None

    after_360 = current_image.resize((w, h), Image.Resampling.BILINEAR)
    after_360_aa = after_360.filter(_VERTICAL_AA)

    def shifted_image(d_orig: float) -> Image.Image:
        return original_image.transform((ow, oh), Image.Transform.AFFINE, (1, 0, 0, 0, 1, d_orig),
                                        resample=Image.Resampling.BICUBIC)

    def mask_scrolled_assistive(diff: Image.Image, delta: float, *, top: float = 0.) -> None:
        if assistive_box is None:
            return
        left, y0, right, y1 = assistive_box
        draw = ImageDraw.Draw(diff)
        for box_top, box_bottom in ((y0, y1), (y0 - delta, y1 - delta)):
            if box_bottom > top:
                _mask_normalized_box(draw, (left, max(top, box_top), right, box_bottom), diff.size)

    def eval_shift(d_orig: float):
        # Resample at native resolution before 360p downsampling and apply symmetric 1D vertical
        # anti-aliasing inside the viewport band to absorb fractional-pixel row phase residuals.
        a_360 = shifted_image(d_orig).resize((w, h), Image.Resampling.BILINEAR)
        sd = _rgb_difference(a_360.filter(_VERTICAL_AA), after_360_aa)
        mask_scrolled_assistive(sd, d_orig / oh)
        hist = sd.crop((0, y_hi_px, w, y_bot_px)).histogram()
        outliers = sum(hist[25:])
        energy = sum(level * count for level, count in enumerate(hist[5:], 5))
        return (outliers, energy, abs(d_orig - growth_delta * oh)), d_orig, sd

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
    _, best_d, sd = best

    shifted = shifted_image(best_d)
    fixed_visible = Image.new("L", (ow, oh), 255)
    if assistive_box is not None:
        _mask_normalized_box(ImageDraw.Draw(fixed_visible), assistive_box, fixed_visible.size)
    moved_visible = fixed_visible.copy()
    mask_scrolled_assistive(moved_visible, best_d / oh)
    split = _scroll_split(original_image, shifted, current_image, y_lo, y_hi, fixed_visible, moved_visible)
    best_y = round(split * h / oh)
    expected = original_image.copy()
    expected.paste(shifted.crop((0, split, ow, round(y_bot_px * oh / h))), (0, split))

    clipped_row = None
    for row in range(max(y_lo, split - 1), min(y_hi, split + 1)):
        band = (0, row, ow, row + 1)
        fit = _fractional_scroll_row(original_image.crop(band), shifted.crop(band), current_image.crop(band),
                                     moved_visible.crop(band), expected.crop(band))
        if fit is not None and (clipped_row is None or fit[0] > clipped_row[1]):
            clipped_row = row, *fit
    if clipped_row is not None:
        row, _, blended = clipped_row
        expected.paste(blended, (0, row))
    comp_before_360 = expected.resize((w, h), Image.Resampling.BILINEAR)

    comp = diff0.copy()
    comp.paste(sd.crop((0, best_y, w, y_bot_px)), (0, best_y))
    # Bilinear resizing followed by vertical AA can sample two rows across the
    # split. Compare those rows against a reference clipped before resizing so
    # the same split is visible in both images. Skip extra smoothing here to
    # preserve evidence of edits on either side of the boundary.
    status_h = screen_compare.status_height_for_size((w, h))
    seam_top = max(int(status_h * h) + 1, best_y - 2)
    seam_bottom = min(y_bot_px, best_y + 2)
    seam = (0, seam_top, w, seam_bottom)
    seam_diff = _rgb_difference(comp_before_360, after_360)
    mask_scrolled_assistive(seam_diff, best_d / oh, top=best_y / h)
    comp.paste(seam_diff.crop(seam), seam[:2])
    if _diff_has_unexpected_changes(before, after, comp, current_region, suggestion_bar, extra,
                                    mask_before=comp_before_360, mask_after=after_360,
                                    assistive_box=assistive_box):
        return None
    return split / oh, best_d / oh


def _ignored_across_scroll(item, ignored_boxes, dy: float) -> bool:
    left, top, right, bottom = item.box
    shifted = (left, top + dy, right, bottom + dy)
    return any(_boxes_overlap(item.box, box) or _boxes_overlap(shifted, box) for box in ignored_boxes)


def _same_context_scrolled(before, after, region, current_region, *, split_y: float, scroll_delta: float,
                           suggestion_bar=None, ignored_boxes=(), min_y: float = .06) -> bool:
    """Verify stationary header/lower anchors and rigidly scrolled chat-viewport anchors."""
    orig_anchors = _context_anchors(before, region, suggestion_bar=suggestion_bar, min_y=min_y)
    curr_anchors = _context_anchors(after, current_region, suggestion_bar=suggestion_bar, min_y=min_y)

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

    before_vp = [i for i in orig_anchors if split_y <= i.center[1] <= region[1]]
    after_vp = [i for i in curr_anchors if split_y <= i.center[1] <= current_region[1]]
    tol_x = tol_y = .008
    occluders = [(left + tol_x, top, right - tol_x, bottom)
                 for left, top, right, bottom in ignored_boxes if right - left > 2 * tol_x]

    def visible_and_grazing(items, dy):
        if not ignored_boxes:
            return items, []
        visible, grazing = [], []
        for item in items:
            if not _ignored_across_scroll(item, ignored_boxes, dy):
                visible.append(item)
            elif not _ignored_across_scroll(item, occluders, dy):
                grazing.append(item)
        return visible, grazing

    # Match clear anchors first so near-button duplicates cannot replace them
    # in the displacement check. Only interior button overlap excuses a line.
    visible_before, grazing_before = visible_and_grazing(before_vp, -scroll_delta)
    visible_after, grazing_after = visible_and_grazing(after_vp, scroll_delta)
    matched_vp, unmatched_before, unmatched_after = _match_shifted_anchors(
        visible_before, visible_after, dy=scroll_delta, tol_x=tol_x, tol_y=tol_y)
    visible_shifts = [b.center[1] - a.center[1] for b, a in matched_vp]
    if len(visible_shifts) >= 2 and abs(median(visible_shifts) - scroll_delta) > .006:
        return False

    # The same line's OCR box can graze the button in only one frame. It still
    # needs a same-label match within the scroll tolerances.
    if grazing_before or grazing_after:
        _, unmatched_before, unmatched_after = _match_shifted_anchors(
            unmatched_before + grazing_before, unmatched_after + grazing_after,
            dy=scroll_delta, tol_x=tol_x, tol_y=tol_y)

    top_margin, bot_margin = .010, .012
    unexpected_before = any(
        i.box[1] - scroll_delta >= split_y + top_margin
        and i.box[3] <= region[1] - bot_margin
        and i.box[3] - scroll_delta <= current_region[1] - bot_margin
        for i in unmatched_before
    )
    unexpected_after = any(
        i.box[1] >= split_y + top_margin
        and i.box[3] <= current_region[1] - bot_margin
        and i.box[3] + scroll_delta <= region[1] - bot_margin
        for i in unmatched_after
    )
    return not unexpected_before and not unexpected_after


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
_CHAT_SEND_KEYS = frozenset({"发送", "發送", "send"})


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


def _return_key_box(original, current, suggestion_bar, *, keys=_RETURN_KEYS, max_bottom: float = .94):
    """The keyboard's return/send key, whose tint changes once the field is non-empty."""
    def find(items):
        found = [item for item in items if item.conf >= _MIN_CONFIDENCE
                 and ocr.normalize_text(item.text) in keys
                 and .72 <= item.box[0] < item.box[2] <= 1
                 and max(suggestion_bar[1] + .14, .82) <= item.box[1] < item.box[3] <= max_bottom]
        return found[0] if len(found) == 1 else None

    first, second = find(original), find(current)
    if (first is None or second is None
            or ocr.normalize_text(first.text) != ocr.normalize_text(second.text)
            or abs(first.center[0] - second.center[0]) > .008 or abs(first.center[1] - second.center[1]) > .005):
        return None
    x, y = (first.center[0] + second.center[0]) / 2, (first.center[1] + second.center[1]) / 2
    return (max(.74, x - .13), max(suggestion_bar[1] + .12, y - .034), min(1., x + .12), min(max_bottom, y + .032))


def _white_keycap_bands(raw: bytes, width: int, height: int, y_min: int, y_max: int) -> list[tuple[int, int]]:
    """Locate the three horizontal QWERTY keycap rows inside `[y_min, y_max)`."""
    stride = width * 3
    samples = len(range(0, width, 2))
    min_row_h = max(10, round(.025 * height))
    bands: list[tuple[int, int]] = []
    cur: list[int] | None = None
    for y in range(y_min, y_max):
        row_off = y * stride
        white = 0
        for x in range(0, width, 2):
            off = row_off + x * 3
            r, g, b = raw[off], raw[off + 1], raw[off + 2]
            if min(r, g, b) >= 240 and max(r, g, b) - min(r, g, b) <= 15:
                white += 1
        if white / samples >= .36:
            if cur is None:
                cur = [y, y]
            else:
                cur[1] = y
        elif cur is not None:
            if cur[1] - cur[0] >= min_row_h:
                bands.append((cur[0], cur[1] + 1))
            cur = None
    if cur is not None and cur[1] - cur[0] >= min_row_h:
        bands.append((cur[0], cur[1] + 1))
    return bands


def _row_keycap_spans(raw: bytes, width: int, y0: int, y1: int) -> list[tuple[int, int]]:
    """Locate white keycap horizontal spans across `(y0, y1)` using top/bottom borders."""
    stride = width * 3
    sample_ys = (y0 + 1, y0 + 2, y1 - 3, y1 - 2)
    min_w = round(.055 * width)
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for x in range(width):
        bright = 0
        for yy in sample_ys:
            off = yy * stride + x * 3
            r, g, b = raw[off], raw[off + 1], raw[off + 2]
            if min(r, g, b) >= 230 and max(r, g, b) - min(r, g, b) <= 20:
                bright += 1
        if bright >= 3:
            if start is None:
                start = x
        elif start is not None:
            if x - start >= min_w:
                spans.append((start, x))
            start = None
    if start is not None and width - start >= min_w:
        spans.append((start, width))
    return spans


def _crop_pixel_stats(img: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, float, float, float] | None:
    """Return `(max_chroma, white_frac, black_frac, grey_frac)` over integer pixel crop `box`."""
    crop_bytes = img.crop(box).tobytes()
    total = len(crop_bytes) // 3
    if total <= 0:
        return None
    max_chroma = white = black = grey = 0
    for idx in range(0, len(crop_bytes), 3):
        r, g, b = crop_bytes[idx], crop_bytes[idx + 1], crop_bytes[idx + 2]
        hi, lo = max(r, g, b), min(r, g, b)
        chroma = hi - lo
        if chroma > max_chroma:
            max_chroma = chroma
        if lo >= 225:
            white += 1
        elif hi <= 45:
            black += 1
        elif 450 <= r + g + b <= 615:
            grey += 1
    return max_chroma, white / total, black / total, grey / total


def _keyboard_shift_off_boxes(
    before_image: Image.Image,
    after_image: Image.Image,
    original,
    current,
    suggestion_bar: tuple[float, float] | None,
    key_box: tuple[float, float, float, float] | None,
) -> tuple[tuple[float, float, float, float], ...] | None:
    """Return tight Shift + 26-letter keycap boxes iff English Shift transitioned ON -> OFF."""
    if suggestion_bar is None or key_box is None or before_image.size != after_image.size:
        return None
    w, h = before_image.size
    y_min = round(suggestion_bar[1] * h)
    y_max = round(key_box[1] * h)
    if y_max - y_min < round(.14 * h):
        return None

    b_raw, a_raw = before_image.tobytes(), after_image.tobytes()
    b_bands = _white_keycap_bands(b_raw, w, h, y_min, y_max)
    a_bands = _white_keycap_bands(a_raw, w, h, y_min, y_max)
    if len(b_bands) != 3 or len(a_bands) != 3:
        return None

    rows: list[tuple[int, int]] = []
    for (by0, by1), (ay0, ay1) in zip(b_bands, a_bands, strict=True):
        if abs(by0 - ay0) > 4 or abs(by1 - ay1) > 4:
            return None
        row_h = (by1 - by0) / h
        if not (.032 <= row_h <= .075):
            return None
        rows.append((min(by0, ay0), max(by1, ay1)))
    p1, p2 = (rows[1][0] - rows[0][0]) / h, (rows[2][0] - rows[1][0]) / h
    if abs(p1 - p2) > .012 or not (.045 <= p1 <= .095):
        return None

    r0_b, r0_a = _row_keycap_spans(b_raw, w, *rows[0]), _row_keycap_spans(a_raw, w, *rows[0])
    r1_b, r1_a = _row_keycap_spans(b_raw, w, *rows[1]), _row_keycap_spans(a_raw, w, *rows[1])
    r2_b, r2_a = _row_keycap_spans(b_raw, w, *rows[2]), _row_keycap_spans(a_raw, w, *rows[2])
    if (len(r0_b), len(r0_a), len(r1_b), len(r1_a)) != (10, 10, 9, 9):
        return None
    if len(r2_b) != 8 or len(r2_a) != 7:
        return None
    shift_x0, shift_x1 = r2_b[0]
    if shift_x0 > round(.03 * w) or shift_x1 > round(.15 * w):
        return None

    letter_boxes_px: list[tuple[int, int, int, int]] = []
    for row_idx, (bs, as_) in enumerate(((r0_b, r0_a), (r1_b, r1_a), (r2_b[1:], r2_a))):
        y0, y1 = rows[row_idx]
        for (bx0, bx1), (ax0, ax1) in zip(bs, as_, strict=True):
            if abs(bx0 - ax0) > 4 or abs(bx1 - ax1) > 4:
                return None
            letter_boxes_px.append((min(bx0, ax0), y0, max(bx1, ax1), y1))

    sy0, sy1 = rows[2]
    sb = _crop_pixel_stats(before_image, (shift_x0 + 2, sy0 + 2, shift_x1 - 2, sy1 - 2))
    sa = _crop_pixel_stats(after_image, (shift_x0 + 2, sy0 + 2, shift_x1 - 2, sy1 - 2))
    if sb is None or sa is None or sb[0] > 30 or sa[0] > 30:
        return None
    if not (sb[1] >= .45 and sb[2] >= .08 and sa[3] >= .60 and sa[1] <= .05):
        return None

    for x0, y0, x1, y1 in letter_boxes_px:
        for img in (before_image, after_image):
            stats = _crop_pixel_stats(img, (x0 + 1, y0 + 1, x1 - 1, y1 - 1))
            if stats is None or stats[0] > 32 or stats[1] < .45:
                return None

    kb_top_norm, kb_bot_norm = rows[0][0] / h, rows[2][1] / h
    flips = 0
    for b_item in original:
        bt = b_item.text.strip()
        if len(bt) != 1 or not ("A" <= bt <= "Z") or b_item.conf < .70:
            continue
        if not (kb_top_norm <= b_item.center[1] <= kb_bot_norm):
            continue
        if any(a_item.text.strip() == bt.lower() and a_item.conf >= .65
               and abs(a_item.center[0] - b_item.center[0]) <= .02
               and abs(a_item.center[1] - b_item.center[1]) <= .015
               for a_item in current):
            flips += 1
    if flips < 3:
        return None

    pad_x, pad_y = 2 / w, 2 / h
    boxes = [
        (max(0., shift_x0 / w - pad_x), max(0., sy0 / h - pad_y),
         min(1., shift_x1 / w + pad_x), min(1., sy1 / h + pad_y))
    ]
    for x0, y0, x1, y1 in letter_boxes_px:
        boxes.append((max(0., x0 / w - pad_x), max(0., y0 / h - pad_y),
                      min(1., x1 / w + pad_x), min(1., y1 / h + pad_y)))
    return tuple(boxes)


def _return_key_enabled(before: Image.Image, after: Image.Image, key_box) -> bool:
    """Confirm the return/send key transitioned from neutral grey to active iOS blue."""
    w, h = before.size
    x0 = max(0, min(w, round(key_box[0] * w)))
    y0 = max(0, min(h, round(key_box[1] * h)))
    x1 = max(0, min(w, round(key_box[2] * w)))
    y1 = max(0, min(h, round(key_box[3] * h)))
    if x1 <= x0 or y1 <= y0:
        return False
    b_raw = before.crop((x0, y0, x1, y1)).tobytes()
    a_raw = after.crop((x0, y0, x1, y1)).tobytes()
    n = len(b_raw) // 3
    if n <= 0 or len(a_raw) != len(b_raw):
        return False
    blue_before = grey_to_blue = 0
    for idx in range(0, len(b_raw), 3):
        r0, g0, b0 = b_raw[idx], b_raw[idx + 1], b_raw[idx + 2]
        r1, b1 = a_raw[idx], a_raw[idx + 2]
        if b0 >= 150 and b0 - r0 >= 50:
            blue_before += 1
        if max(r0, g0, b0) - min(r0, g0, b0) <= 30 and b1 >= 150 and b1 - r1 >= 50:
            grey_to_blue += 1
    return blue_before / n <= .05 and grey_to_blue / n >= .30


_PAIR_CACHE: tuple[object, tuple, bool] | None = None


def _consume_wrapped_lines(added: tuple[str, ...], lines: tuple[str, ...]) -> int | None:
    """Match logical `lines` against soft-wrapped visual OCR `added`, returning consumed index or None."""
    idx = 0
    for line in lines:
        if idx >= len(added):
            return None
        joined = added[idx]
        idx += 1
        while len(joined) < len(line) and idx < len(added) and line.startswith(joined):
            joined += added[idx]
            idx += 1
        if joined != line:
            return None
    return idx


def _matches_wrapped_lines(added: tuple[str, ...], expected: tuple[str, ...]) -> bool:
    """Match visual OCR rows to logical lines, allowing display-level soft wrapping.

    One logical line may wrap across consecutive visual rows whose concatenation
    equals that exact logical line. Distinct logical lines (separated by a real
    newline) must still start on separate visual rows, and any missing, extra,
    altered, or duplicated text is rejected.
    """
    return _consume_wrapped_lines(added, expected) == len(added)


_TERMINAL_CJK_PUNCT = frozenset("。！？…")
_OCR_THIN_CJK_PUNCT = frozenset("「」『』《》〈〉“”‘’（）【】、，；：。！？…·")
_WIDTH_EQUIV_PUNCT = {
    "，": ",",
    ",": "，",
    "；": ";",
    ";": "；",
    "：": ":",
    ":": "：",
    "！": "!",
    "!": "！",
    "？": "?",
    "?": "？",
    "（": "(",
    "(": "（",
    "）": ")",
    ")": "）",
}
_CORNER_BRACKET_OCR_EQUIV = {
    "[": frozenset("「『"),
    "]": frozenset("」』"),
}
_NEGATION_CJK = frozenset("不没无非勿别否未禁停")
_MIN_TAIL_PUNCT_LEN = 15
_MIN_TAIL_LAST_LINE = 6
_MAX_OCR_PUNCT_OMISSIONS = 2
_PROTECTED_PRECEDING = re.compile(r"[0-9A-Za-z@#%$¥￥/\\?=&+_~^|<>\[\]{}.-]")


def _is_safe_fade_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" and ch not in _NEGATION_CJK


def _adjacent_to_number_or_currency(line: str, pos: int) -> bool:
    for adj in (pos - 1, pos + 1):
        if 0 <= adj < len(line):
            ch = line[adj]
            if ch.isdigit() or ch in "¥￥$":
                return True
    return False


def _chars_match_chat(obs_ch: str, exp_ch: str) -> bool:
    return (
        obs_ch == exp_ch
        or _WIDTH_EQUIV_PUNCT.get(obs_ch) == exp_ch
        or exp_ch in _CORNER_BRACKET_OCR_EQUIV.get(obs_ch, ())
    )


def _matches_chat_composer_lines(
    added: tuple[str, ...],
    expected: tuple[str, ...],
    *,
    faded_rows: dict[int, str] | None = None,
) -> tuple[str | None, str]:
    """Match chat-composer OCR rows, allowing thin CJK punctuation gaps and proven corner fade.

    Preserves logical newlines, digits, prices, URLs, negations, and exact word
    characters; allows only bounded thin CJK punctuation omissions/width swaps
    and physically-bleached bottom-right CJK character substitution/omission.
    """
    if _matches_wrapped_lines(added, expected):
        return "full", ""
    if not added or not expected or len(added) < len(expected):
        return None, ""
    total_added = sum(len(row) for row in added)
    total_expected = sum(len(line) for line in expected)
    if total_expected < _MIN_TAIL_PUNCT_LEN or total_added > total_expected:
        return None, ""
    max_missing = _MAX_OCR_PUNCT_OMISSIONS + (1 if faded_rows else 0)
    if total_expected - total_added > max_missing:
        return None, ""

    row_idx = 0
    omitted_punct: list[tuple[int, int, str]] = []
    width_diffs = 0
    fade_diffs: list[str] = []

    def _can_omit_punct(l_idx: int, l_text: str, p: int) -> bool:
        if l_text[p] not in _OCR_THIN_CJK_PUNCT or len(omitted_punct) >= _MAX_OCR_PUNCT_OMISSIONS:
            return False
        at_final_tail = l_idx == len(expected) - 1 and all(ch in _OCR_THIN_CJK_PUNCT for ch in l_text[p:])
        at_line_terminal_tail = all(ch in _TERMINAL_CJK_PUNCT for ch in l_text[p:])
        if at_final_tail and len(l_text) < _MIN_TAIL_LAST_LINE:
            return False
        if (at_final_tail or at_line_terminal_tail) and p > 0 and _PROTECTED_PRECEDING.search(l_text[p - 1]):
            return False
        return True

    for line_idx, line in enumerate(expected):
        pos = 0
        consumed_rows = 0
        while pos < len(line):
            if row_idx >= len(added):
                return None, ""
            row = added[row_idx]
            k = 0
            while k < len(row):
                if pos < len(line) and _chars_match_chat(row[k], line[pos]):
                    if row[k] != line[pos]:
                        width_diffs += 1
                    k += 1
                    pos += 1
                elif pos < len(line) and _can_omit_punct(line_idx, line, pos):
                    omitted_punct.append((line_idx, pos, line[pos]))
                    pos += 1
                elif (k == len(row) - 1 and len(row) >= 4 and pos < len(line) and not fade_diffs
                      and (faded_rows or {}).get(row_idx) == "sub"
                      and _is_safe_fade_cjk(line[pos]) and _is_safe_fade_cjk(row[k])
                      and not _adjacent_to_number_or_currency(line, pos)):
                    fade_diffs.append(f"{line[pos]}->{row[k]}")
                    k += 1
                    pos += 1
                else:
                    return None, ""

            if (pos < len(line) and not fade_diffs and len(row) >= 4
                    and (faded_rows or {}).get(row_idx) == "omit"
                    and _is_safe_fade_cjk(line[pos])
                    and not _adjacent_to_number_or_currency(line, pos)
                    and ((row_idx + 1 >= len(added) and all(ch in _OCR_THIN_CJK_PUNCT for ch in line[pos + 1:]))
                         or (row_idx + 1 < len(added) and not _chars_match_chat(added[row_idx + 1][0], line[pos])))):
                fade_diffs.append(f"{line[pos]}->")
                pos += 1

            while pos < len(line) and _can_omit_punct(line_idx, line, pos):
                if row_idx + 1 >= len(added):
                    omitted_punct.append((line_idx, pos, line[pos]))
                    pos += 1
                elif not _chars_match_chat(added[row_idx + 1][0], line[pos]):
                    omitted_punct.append((line_idx, pos, line[pos]))
                    pos += 1
                else:
                    break

            row_idx += 1
            consumed_rows += 1
        if consumed_rows == 0:
            return None, ""

    if row_idx != len(added):
        return None, ""

    punct_str = "".join(ch for _, _, ch in omitted_punct)
    if fade_diffs:
        detail = f"{fade_diffs[0]};punct={punct_str}" if punct_str else fade_diffs[0]
        return "corner_fade", detail
    if omitted_punct:
        tail_start = len(expected[-1]) - len(omitted_punct)
        if (width_diffs == 0
                and all(l_idx == len(expected) - 1 and ch in _TERMINAL_CJK_PUNCT for l_idx, _, ch in omitted_punct)
                and omitted_punct[0][1] == tail_start):
            return "tail_punct", punct_str
        return "ocr_punct", punct_str
    if width_diffs:
        return "ocr_punct", "width"
    return "full", ""


def _notes_enabled_share_box(before, after, original, current, region):
    """Recognize the disabled-to-enabled share arrow in an empty Apple Notes editor."""
    width, height = before.size
    for items in (original, current):
        titles = [item for item in items if item.conf >= _MIN_CONFIDENCE
                  and ocr.normalize_text(item.text) in {"icloud全部备忘录", "icloud全部備忘錄", "allicloud"}
                  and .08 < item.center[0] < .4 and .04 < item.center[1] < region[1]]
        done = [item for item in items if item.conf >= _MIN_CONFIDENCE
                and ocr.normalize_text(item.text) in {"完成", "done"}
                and .82 < item.center[0] < .98 and .04 < item.center[1] < region[1]]
        if len(titles) != 1 or len(done) != 1 or abs(titles[0].center[1] - done[0].center[1]) > .012:
            return None
    cy = round(done[0].center[1] * height)
    crop = (round(.54 * width), cy - round(.055 * width), round(.725 * width), cy + round(.055 * width))
    if crop[1] < 0 or crop[3] >= region[1] * height:
        return None

    grey = Image.new("L", before.crop(crop).size)
    gold = Image.new("L", grey.size)
    for y in range(grey.height):
        for x in range(grey.width):
            position = crop[0] + x, crop[1] + y
            old = before.getpixel(position)
            r, g, b = after.getpixel(position)
            if min(old) >= 180 and max(old) <= 241 and max(old) - min(old) <= 10:
                grey.putpixel((x, y), 255)
            if r - b > 34 and g - b > 25 and -8 <= r - g < 100:
                gold.putpixel((x, y), 255)
    bounds, active = grey.getbbox(), gold.getbbox()
    if not bounds or not active:
        return None
    x0, y0, x1, y1 = bounds
    w, h = x1 - x0, y1 - y0
    if not (.036 * width <= w <= .067 * width and 1.1 * w <= h <= 1.45 * w):
        return None
    # Brighter active strokes can include an extra antialiased pixel on
    # either edge. The control's center must stay at the same position.
    if (abs((x0 + x1) - (active[0] + active[2])) > 2 * max(1, .0025 * width)
            or abs((y0 + y1) - (active[1] + active[3])) > 2 * max(1, .0025 * width)):
        return None
    # Match the open-top tray, central shaft and arrowhead in both states.
    glyph = Image.new("L", grey.size)
    draw = ImageDraw.Draw(glyph)
    for line in (((.27, .16), (.5, .02), (.73, .16)), ((.5, .02), (.5, .60)),
                 ((.36, .28), (.09, .28), (.04, .35), (.04, .88), (.10, .96),
                  (.90, .96), (.96, .88), (.96, .35), (.90, .28), (.67, .28))):
        draw.line([(round(x0 + a * (w - 1)), round(y0 + b * (h - 1))) for a, b in line], fill=255)
    tolerance = 2 * max(1, round(.14 * w)) + 1
    band = glyph.filter(ImageFilter.MaxFilter(tolerance))
    for ink in (grey, gold):
        if (ImageChops.subtract(ink, band).getbbox()
                or ImageChops.subtract(glyph, ink.filter(ImageFilter.MaxFilter(tolerance))).getbbox()):
            return None
    # Do not excuse edits in the rectangle or inside the tray: only neutral
    # grey on white may become yellow on white along the proven strokes.
    # The enabled glyph has a slightly larger antialiased footprint than
    # the disabled one. Validate the union, including its pale JPEG fringe;
    # never exempt an unchecked toolbar rectangle.
    fringe = gold.filter(ImageFilter.MaxFilter(9))
    left, top = min(x0, active[0]) - 3, min(y0, active[1]) - 3
    right, bottom = max(x1, active[2]) + 3, max(y1, active[3]) + 3
    for y in range(top, bottom):
        for x in range(left, right):
            if not (0 <= x < grey.width and 0 <= y < grey.height):
                return None
            position = crop[0] + x, crop[1] + y
            old = before.getpixel(position)
            new = after.getpixel(position)
            noise = 28 if min(old) >= 229 and min(new) >= 223 else 12
            if max(abs(a - b) for a, b in zip(old, new, strict=True)) <= noise:
                continue
            if (fringe.getpixel((x, y)) and min(old) >= 245 and max(old) - min(old) <= 12
                    and min(new[:2]) >= 240 and new[2] >= 200
                    and min(new[:2]) - new[2] >= 12 and abs(new[0] - new[1]) <= 16):
                continue
            if (not band.getpixel((x, y)) or min(old) < 175 or max(old) - min(old) > 15
                    or new[0] < 145 or new[1] < 135 or new[0] < new[1] - 16 or new[1] < new[2] - 16):
                return None
    return ((crop[0] + left) / width, (crop[1] + top) / height,
            (crop[0] + right) / width, (crop[1] + bottom) / height)


def _notes_outside_visual_changed(before, after, original, current, region, suggestion_bar, extra):
    """Keep all context checks; discount only proven Done-label JPEG blue chroma.

    The recorded unchanged gold Done strokes differed by up to 46 in blue,
    creating a seven-pixel residual. Do not mask its rectangle: red/green,
    dark strokes, larger changes and every other region remain checked.
    Called only after the Notes share transition and context have matched.
    """
    diff = screen_compare._difference(before, after, 360)
    if diff is None:
        return True
    labels = [[item for item in items if ocr.normalize_text(item.text) in {"完成", "done"}
               and item.conf >= _MIN_CONFIDENCE and .82 < item.center[0] < .98
               and .04 < item.center[1] < region[1]] for items in (original, current)]
    if all(len(items) == 1 for items in labels) and labels[0][0].box == labels[1][0].box:
        box = labels[0][0].box
        first = _image(before).resize(diff.size, Image.Resampling.BILINEAR)
        second = _image(after).resize(diff.size, Image.Resampling.BILINEAR)
        width, height = diff.size
        for y in range(max(0, math.floor(box[1] * height)), min(height, math.ceil(box[3] * height))):
            for x in range(max(0, math.floor(box[0] * width)), min(width, math.ceil(box[2] * width))):
                old, new = first.getpixel((x, y)), second.getpixel((x, y))
                dr, dg, db = (abs(a - b) for a, b in zip(old, new, strict=True))
                if (dr <= 12 and dg <= 12 and db <= 48
                        and all(r >= 175 and g >= 145 and b <= min(r, g) + 12 for r, g, b in (old, new))):
                    diff.putpixel((x, y), max(dr, dg))
    return _diff_has_unexpected_changes(before, after, diff, region, suggestion_bar, extra)


def _verify_paste_transition(before, after, original, current, region, expected,
                             *, notes_only=False, require_visual=False) -> str | None:
    """Verify before/after paste state and return the evidence label, or None if rejected."""
    original_image, current_image = _image(before), _image(after)
    if original_image.size != current_image.size:
        return None
    if _is_permission_prompt(original) or _is_permission_prompt(current):
        return None
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
    if require_visual and original_lines != ():
        return None
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
        return None
    if grown is None:
        if not _same_field_outline(original_image, current_image, region):
            return None
        if not notes_only and not require_visual and _same_context(original, current, region):
            return "direct_paste_verified"
    elif len(current_lines) < 2:
        return None  # only multi-line text grows the field
    if notes_only and (grown is not None or original_lines != () or suggestion_bar is None):
        return None
    if not require_visual and suggestion_bar is None and (grown is not None or assistive_box is None):
        return None
    if require_visual and grown is not None and suggestion_bar is None:
        return None
    compact = screen_compare.is_compact_portrait_size(original_image.size)
    anchor_min_y = .038 if compact else .06
    return_max_bottom = .995 if compact else .94
    extra = []
    if grown is not None:
        extra.extend(grown[1])
    key_box = None
    if original_lines == () and suggestion_bar is not None:
        key_box = _return_key_box(original, current, suggestion_bar, max_bottom=return_max_bottom)
        if key_box is not None:
            extra.append(key_box)
            shift_boxes = _keyboard_shift_off_boxes(
                original_image, current_image, original, current, suggestion_bar, key_box)
            if shift_boxes is not None:
                extra.extend(shift_boxes)
    ignored = (assistive_box,) if assistive_box is not None else ()
    if _same_context(original, current, current_region,
                     suggestion_bar=suggestion_bar, ignored_boxes=ignored, min_y=anchor_min_y):
        if not notes_only and not _outside_visual_changed(before, after, current_region, suggestion_bar, extra):
            if (not require_visual or grown is not None
                    or (key_box is not None and _return_key_enabled(original_image, current_image, key_box))):
                return "direct_paste_verified"
        if grown is None and original_lines == () and suggestion_bar is not None:
            share = _notes_enabled_share_box(original_image, current_image, original, current, region)
            if share and not _notes_outside_visual_changed(
                    before, after, original, current, region, suggestion_bar, [*extra, share]):
                return "notes_share_enabled"
    if grown is None:
        return None
    split_bounds = _header_viewport_bounds(
        original, current, region, current_region, ignored_boxes=ignored, min_y=anchor_min_y)
    if split_bounds is None:
        return None
    scrolled = _scrolled_viewport_match(
        before, after, original_image, current_image, current_region, suggestion_bar, extra,
        growth_delta=grown[2], split_bounds=split_bounds, assistive_box=assistive_box,
    )
    if scrolled is None:
        return None
    split_y, scroll_delta = scrolled
    if _same_context_scrolled(
        original, current, region, current_region,
        split_y=split_y, scroll_delta=scroll_delta, suggestion_bar=suggestion_bar,
        ignored_boxes=ignored, min_y=anchor_min_y,
    ):
        return "direct_paste_verified"
    return None


def _verify_before_after_sync(before, after, original, current, region, expected, *, notes_only=False) -> bool:
    return _verify_paste_transition(
        before, after, original, current, region, expected, notes_only=notes_only, require_visual=False,
    ) is not None


def _chat_composer_layout(before: bytes, after: bytes, region: tuple[float, float, float, float]):
    """Return the QuickType strip if `region` is a lower chat composer, else None."""
    if region[1] < .35:
        return None
    suggestion_bar = _located_suggestion_bar(before, after, region)
    if suggestion_bar is None or not 0 <= suggestion_bar[0] - region[3] <= .04:
        return None
    return suggestion_bar


def _verify_chat_structural_sync(
    before: bytes,
    after: bytes,
    original,
    current,
    region: tuple[float, float, float, float],
    expected: tuple[str, ...],
    *,
    require_visual: bool = True,
) -> tuple[str, tuple[float, float]] | None:
    """Verify a chat-composer paste while decoupling only the live chat history stream.

    Keeps full verification of: conversation header bar (OCR + pixels), no
    permission modal or AssistiveTouch menu panel, input-field outline/growth,
    exact verbatim pasted text, return/send key transition, lower keyboard
    anchors, and keyboard pixels.
    """
    original_image, current_image = _image(before), _image(after)
    if original_image.size != current_image.size:
        return None
    if _is_permission_prompt(original) or _is_permission_prompt(current):
        return None
    if assistive_vision.find_panel(before) is not None or assistive_vision.find_panel(after) is not None:
        return None
    compact = screen_compare.is_compact_portrait_size(original_image.size)
    anchor_min_y = .038 if compact else .06
    return_max_bottom = .995 if compact else .94
    suggestion_bar = _chat_composer_layout(before, after, region)
    if suggestion_bar is None:
        return None
    key_box = _return_key_box(original, current, suggestion_bar, max_bottom=return_max_bottom)
    if key_box is None:
        return None
    assistive_box = screen_compare.stationary_assistive_button_box(before, after, exclude=(region,))
    grown = _grown_field(original_image, current_image, region, suggestion_bar, assistive_box=assistive_box)
    current_region = grown[0] if grown else region
    if assistive_box is not None and _boxes_overlap(assistive_box, current_region):
        assistive_box = None
        if grown is not None:
            grown = _grown_field(original_image, current_image, region, suggestion_bar, assistive_box=None)
            current_region = grown[0] if grown else region
    original_lines = _field_lines(original, region)
    curr_field = (
        _filter_stationary_field_icons(
            current, original_image, current_image, current_region, expected=expected,
        )
        if original_lines == () else current
    )
    curr_rows = _field_rows(curr_field, current_region)
    current_lines = (
        None if curr_rows is None
        else tuple(_visible_text("".join(item.text for item in row)) for row in curr_rows)
    )
    if require_visual and original_lines != ():
        return None
    faded_rows = (
        _corner_faded_rows(curr_rows, original_image, current_image, current_region)
        if (curr_rows is not None and original_lines == ()) else {}
    )
    match_mode = None
    if (original_lines is not None and current_lines is not None
            and len(current_lines) >= len(original_lines)
            and current_lines[:len(original_lines)] == original_lines):
        match_mode, _ = _matches_chat_composer_lines(
            current_lines[len(original_lines):], expected, faded_rows=faded_rows,
        )
    if match_mode is None:
        return None
    if match_mode in {"tail_punct", "corner_fade", "ocr_punct"}:
        send_box = _return_key_box(original, current, suggestion_bar, keys=_CHAT_SEND_KEYS)
        if (original_lines != () or send_box is None
                or not _return_key_enabled(original_image, current_image, send_box)):
            return None
    if grown is None:
        if not _same_field_outline(original_image, current_image, region):
            return None
        if require_visual and not _return_key_enabled(original_image, current_image, key_box):
            return None
    elif len(current_lines) < 2:
        return None
    ignored = (assistive_box,) if assistive_box is not None else ()
    orig_anchors = _context_anchors(
        original, region, suggestion_bar=suggestion_bar, ignored_boxes=ignored, min_y=anchor_min_y)
    curr_anchors = _context_anchors(
        current, current_region, suggestion_bar=suggestion_bar, ignored_boxes=ignored, min_y=anchor_min_y)
    orig_header = [i for i in orig_anchors if i.center[1] <= .105]
    curr_header = [i for i in curr_anchors if i.center[1] <= .105]
    header_unique = _stationary_anchors_match(orig_header, curr_header)
    if header_unique is None or not header_unique:
        return None
    orig_lower = [i for i in orig_anchors if i.center[1] > region[3]]
    curr_lower = [i for i in curr_anchors if i.center[1] > region[3]]
    lower_unique = _stationary_anchors_match(orig_lower, curr_lower)
    if lower_unique is None or not lower_unique or len(header_unique) + len(lower_unique) < 3:
        return None
    header_bottom = max(max(i.box[3] for i in orig_header), max(i.box[3] for i in curr_header)) + .004
    composer_top = min(region[1], current_region[1])
    if not header_bottom < composer_top - .10:
        return None
    diff = screen_compare._difference(before, after, 360)
    if diff is None:
        return None
    shift_boxes = (
        _keyboard_shift_off_boxes(original_image, current_image, original, current, suggestion_bar, key_box)
        if original_lines == () else None
    )
    viewport = (header_bottom, composer_top)
    extra = [
        *(grown[1] if grown is not None else ()),
        key_box,
        *(shift_boxes or ()),
        (0., viewport[0], 1., viewport[1]),
    ]
    if _diff_has_unexpected_changes(before, after, diff, current_region, suggestion_bar, extra,
                                    assistive_box=assistive_box):
        return None
    return "direct_paste_verified", viewport


async def verify_chat_structural_paste(before, after, region, observed_text, *,
                                       require_visual: bool = True) -> tuple[str, tuple[float, float]] | None:
    """Confirm a chat paste structurally, tolerating live activity only in the chat viewport."""
    if not before or not after or before == after or region is None:
        return None
    try:
        region = ocr.validate_region(region)
        expected = _expected_lines(observed_text)
        if not expected or _chat_composer_layout(before, after, region) is None:
            return None
        read_fn = ocr.read_text_async
        original = await read_fn(before)
        current = await read_fn(after)
        return await asyncio.to_thread(
            _verify_chat_structural_sync, before, after, original, current, region, expected,
            require_visual=require_visual,
        )
    except (ocr.OCRError, OSError, ValueError):
        return None


def chat_viewport_only_changed(before: bytes, after: bytes, viewport: tuple[float, float], *,
                               caret_frames=(), caret_scope=None) -> bool:
    """True when header, pasted draft field, and keyboard pixels stayed intact across `before` -> `after`."""
    if not before or not after:
        return False
    if assistive_vision.find_panel(before) is not None or assistive_vision.find_panel(after) is not None:
        return False
    diff = screen_compare._difference(before, after, 360)
    if diff is None:
        return False
    draw = ImageDraw.Draw(diff)
    _mask_normalized_box(draw, (0., viewport[0], 1., viewport[1]), diff.size)
    exclude = (caret_scope,) if caret_scope is not None else ()
    assistive_box = screen_compare.stationary_assistive_button_box(before, after, exclude=exclude)
    if assistive_box is not None:
        _mask_normalized_box(draw, assistive_box, diff.size)
    for frame in (before, after):
        button = assistive_vision.find_button_target(frame)
        if button is not None:
            _, (left, top, right, bottom) = button
            pad = .015
            _mask_normalized_box(
                draw,
                (max(0., left - pad), max(0., top - pad), min(1., right + pad), min(1., bottom + pad)),
                diff.size,
            )
    screen_compare.mask_caret_blink(before, after, diff, caret_frames, scope=caret_scope)
    screen_compare.mask_clock_ticks(before, after, diff)
    screen_compare.mask_pointer_highlights(before, after, diff)
    screen_compare._mask_pointer_discs(before, after, diff, max_discs=2)
    screen_compare._mask_translucent_pointer_fade(before, after, diff)
    screen_compare._mask_flat_background_pointer_disc(before, after, diff)
    values = diff.tobytes()
    return not (sum(value for value in values if value > 4) / len(values) > .8
                or any(len(points) >= 6 for points in screen_compare._regions(diff, 24)))


async def _verify_before_after_uncached(read_fn, before, after, region, expected, *, notes_only) -> bool:
    original = await read_fn(before)
    current = await read_fn(after)
    return await asyncio.to_thread(_verify_before_after_sync, before, after, original, current, region, expected,
                                   notes_only=notes_only)


async def _verify_before_after(before, after, region, expected, *, notes_only=False) -> bool:
    global _PAIR_CACHE
    read_fn = ocr.read_text_async
    key = (hashlib.sha256(before).digest(), hashlib.sha256(after).digest(), region, expected, notes_only)
    cached = _PAIR_CACHE
    if cached is not None and cached[0] is read_fn and cached[1] == key:
        return cached[2]
    verified = await _verify_before_after_uncached(read_fn, before, after, region, expected, notes_only=notes_only)
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


async def verify_direct_paste(before, after, region, observed_text) -> str | None:
    """Confirm a direct sheet-less paste via OCR plus strict pixel-level change verification."""
    if not before or not after or before == after or region is None:
        return None
    try:
        region = ocr.validate_region(region)
        expected = _expected_lines(observed_text)
        if not expected:
            return None
        read_fn = ocr.read_text_async
        original = await read_fn(before)
        current = await read_fn(after)
        direct = await asyncio.to_thread(
            _verify_paste_transition, before, after, original, current, region, expected,
            notes_only=False, require_visual=True,
        )
        if direct is not None:
            return direct
        structural = await asyncio.to_thread(
            _verify_chat_structural_sync, before, after, original, current, region, expected,
            require_visual=True,
        )
        return structural[0] if structural is not None else None
    except (ocr.OCRError, OSError, ValueError):
        return None


async def verify_before_after_for_permission(before, after, region, observed_text, *, notes_only=False) -> bool:
    """Verify before/after paste state alone before scanning permission-sheet candidates."""
    if not before or not after or before == after or region is None:
        return False
    try:
        region = ocr.validate_region(region)
        expected = _expected_lines(observed_text)
        if not expected:
            return False
        return await _verify_before_after(before, after, region, expected, notes_only=notes_only)
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
