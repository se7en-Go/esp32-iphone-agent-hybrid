"""Narrow insertion-caret evidence; never ignore an unknown field's contents."""

import io
import statistics
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw


def _image(frame):
    if isinstance(frame, bytes):
        with Image.open(io.BytesIO(frame)) as source:
            frame = source.convert("RGB")
    else:
        frame = frame.convert("RGB")
    return frame


def _components(mask):
    width = mask.width
    hot = {(i % width, i // width) for i, value in enumerate(mask.tobytes()) if value}
    while hot:
        stack, points = [hot.pop()], []
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


@dataclass(frozen=True)
class FieldCaret:
    box: tuple[int, int, int, int]
    field: tuple[int, int, int, int]
    background_x: int


def _caret_components(image, white, regions):
    for bounds in regions or ((0, 0, image.width, image.height),):
        patch = image.crop(bounds)
        pixels = patch.tobytes()
        mask = Image.new("L", patch.size)
        mask.putdata([255 if (not white and b >= 150 and b - r >= 60 and b - g >= 25 and g >= 50)
                      or (white and min(r, g, b) >= 215 and max(r, g, b) - min(r, g, b) <= 65)
                      else 0 for r, g, b in zip(pixels[0::3], pixels[1::3], pixels[2::3], strict=True)])
        for points in _components(mask):
            # Cropping a white page must never manufacture a thin caret shape.
            if regions and any(x in (0, patch.width - 1) or y in (0, patch.height - 1) for x, y in points):
                continue
            yield [(x + bounds[0], y + bounds[1]) for x, y in points]


def field_carets(image, *, allow_white=False, _white=False, regions=None):
    """Locate thin colored carets enclosed by a visible, bounded field background."""
    if allow_white:
        # Keep color masks separate: a blue caret inside a white field must not
        # merge with that field when white Spotlight support is enabled.
        return list(dict.fromkeys(field_carets(image, regions=regions)
                                  + field_carets(image, _white=True, regions=regions)))
    w, h = image.size
    found = []
    for points in _caret_components(image, _white, regions):
        xs, ys = zip(*points, strict=True)
        x0, y0, x1, y1 = min(xs), min(ys), max(xs) + 1, max(ys) + 1
        bw, bh = x1 - x0, y1 - y0
        if _white and .012 * w < bw <= .06 * w and max(6, .008 * h) <= bh <= .05 * h:
            # JPEG chroma can join a few adjacent placeholder pixels to a white
            # caret. Require its nearly full-height core: a shorter adjacent
            # letter stroke is not another core. Never expand into that text.
            columns = {x for x in xs if xs.count(x) >= .95 * bh}
            if columns and max(columns) - min(columns) + 1 == len(columns):
                x0, x1 = min(columns), max(columns) + 1
                points = [(x, y) for x, y in points if x0 <= x < x1]
                bw = x1 - x0
        if not (1 <= bw <= max(3, .012 * w) and max(6, .008 * h) <= bh <= .05 * h
                and bh >= 3 * bw and len(points) >= .65 * bw * bh and y0 > .06 * h):
            continue
        margin = max(3, round(.012 * w))
        for sx in (x0 - margin, x1 + margin):
            if not 0 <= sx < w:
                continue
            seed = sx, (y0 + y1) // 2
            delta = ImageChops.difference(image, Image.new("RGB", image.size, image.getpixel(seed)))
            r, g, b = delta.split()
            background = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v <= 12 else 0)
            ImageDraw.floodfill(background, seed, 128)
            bounds = background.point(lambda v: 255 if v == 128 else 0).getbbox()
            if bounds is None:
                continue
            left, top, right, bottom = bounds
            if (0 < left < x0 < x1 < right < w and .06 * h < top < y0 < y1 < bottom < h
                    and .2 * w <= right - left <= .96 * w and .02 * h <= bottom - top <= .2 * h):
                found.append(FieldCaret((x0, y0, x1, y1), bounds, sx))
                break
    return found


def _field_region(caret, size):
    left, top, right, bottom = caret.field
    w, h = size
    return left / w, top / h, (right - 1) / w, (bottom - 1) / h


def focused_region(frame, *, allow_white=False, accept_field=None):
    """Select a unique caret; optional same-frame evidence may exclude fields."""
    image = _image(frame)
    carets = field_carets(image, allow_white=allow_white)
    if accept_field is not None:
        carets = [caret for caret in carets if accept_field(_field_region(caret, image.size))]
    if len(carets) != 1:
        return None
    return _field_region(carets[0], image.size)


def _phase_errors(sample, off, on):
    delta = [high - low for low, high in zip(off, on, strict=True)]
    norm = sum(value * value for value in delta)
    if norm == 0:
        return None
    alpha = sum((value - low) * change for value, low, change in zip(sample, off, delta, strict=True)) / norm
    if not -.08 <= alpha <= 1.08:
        return None
    alpha = max(0, min(1, alpha))
    errors = [abs(value - (low + alpha * change))
              for value, low, change in zip(sample, off, delta, strict=True)]
    return [max(errors[i:i + 3]) for i in range(0, len(errors), 3)]


def _phase_matches(sample, off, on):
    """A single opacity must explain the entire caret, including its ends and edges."""
    pixels = _phase_errors(sample, off, on)
    if pixels is None:
        return False
    return sum(value > 20 for value in pixels) < 6 and sum(value > 55 for value in pixels) < 3


def _bounded_field(image, box):
    """A tighter color component must have visible edges on all four sides."""
    x0, y0, x1, y1 = box
    w, h = image.size
    margin = max(3, round(.012 * w))
    for sx in (x0 - margin, x1 + margin):
        if not 0 <= sx < w:
            continue
        seed = sx, (y0 + y1) // 2
        channels = ImageChops.difference(image, Image.new("RGB", image.size, image.getpixel(seed))).split()
        background = ImageChops.lighter(ImageChops.lighter(*channels[:2]), channels[2])
        background = background.point(lambda value: 255 if value <= 8 else 0)
        ImageDraw.floodfill(background, seed, 128)
        bounds = background.point(lambda value: 255 if value == 128 else 0).getbbox()
        if bounds is None:
            continue
        left, top, right, bottom = bounds
        if not (0 < left < x0 < x1 < right < w and .06 * h < top < y0 < y1 < bottom < h
                and .2 * w <= right - left <= .96 * w and .02 * h <= bottom - top <= .2 * h):
            continue
        cx, cy = (left + right) // 2, (top + bottom) // 2
        edges = (((left + 3, cy), (max(0, left - 3), cy)),
                 ((right - 4, cy), (min(w - 1, right + 2), cy)),
                 ((cx, top + 3), (cx, max(0, top - 3))),
                 ((cx, bottom - 4), (cx, min(h - 1, bottom + 2))))
        if all(max(abs(a - b) for a, b in zip(image.getpixel(inside), image.getpixel(outside), strict=True)) > 8
               for inside, outside in edges):
            return FieldCaret(box, bounds, sx)
    return None


def _editor_caret(image, box):
    """Local verification area for an amber caret on a white editor canvas."""
    x0, y0, x1, y1 = box
    w, h = image.size
    pad_x, pad_y = round(.06 * w), round(.02 * h)
    if not (pad_x < x0 < x1 < w - pad_x
            and .06 * h + pad_y < y0 < y1 < h - pad_y):
        return None
    background_x = x1 + max(3, round(.012 * w))
    light = [min(image.getpixel((background_x, y))) >= 232 for y in range(y0, y1)]
    if sum(light) >= .95 * len(light):
        return FieldCaret(box, (x0 - pad_x, y0 - pad_y, x1 + pad_x, y1 + pad_y), background_x)
    return None


def _field_difference(image, reference, caret, patch):
    left, top, _, _ = caret.field
    channels = ImageChops.difference(image.crop(caret.field), reference.crop(caret.field)).split()
    diff = ImageChops.lighter(ImageChops.lighter(*channels[:2]), channels[2])
    # Half-open patch: leave every neighboring underline/text pixel compared.
    ImageDraw.Draw(diff).rectangle((patch[0] - left, patch[1] - top,
                                    patch[2] - 1 - left, patch[3] - 1 - top), fill=0)
    return sum(diff.histogram()[25:])


def _same_field(image, reference, caret, patch):
    return _field_difference(image, reference, caret, patch) < 6


def _green_scanout_matches(sample, off, on, width):
    """Allow smooth sRGB/4:2:0 scanout fade along a proven green caret, never a mark or tick."""
    stride = width * 3
    alphas, errors = [], []
    for r0 in range(0, len(sample), stride):
        s_row, lo_row, hi_row = sample[r0:r0 + stride], off[r0:r0 + stride], on[r0:r0 + stride]
        delta = [h - lo for lo, h in zip(lo_row, hi_row, strict=True)]
        norm = sum(d * d for d in delta)
        if norm < 300:
            row_errors = [max(abs(s_row[k + c] - lo_row[k + c]) for c in range(3)) for k in range(0, stride, 3)]
        else:
            alpha = sum((s - lo) * d for s, lo, d in zip(s_row, lo_row, delta, strict=True)) / norm
            if not -.10 <= alpha <= 1.10:
                return False
            alpha = max(0., min(1., alpha))
            alphas.append(alpha)
            row_errors = [max(abs(s_row[k + c] - (lo_row[k + c] + alpha * delta[k + c])) for c in range(3))
                          for k in range(0, stride, 3)]
        if any(err > 54 for err in row_errors):
            return False
        errors.extend(row_errors)
    if not errors or statistics.median(errors) > 12:
        return False
    return not alphas or (max(alphas) - min(alphas) <= .65
                          and all(abs(b - a) <= .22 for a, b in zip(alphas, alphas[1:], strict=False)))


def _current_template_matches(image, reference, caret, patch, low, high, *, green=False):
    field_diff = _field_difference(image, reference, caret, patch)
    sample = image.crop(patch).tobytes()
    errors = _phase_errors(sample, low, high)
    if errors is not None and (field_diff + sum(v > 20 for v in errors) < 6 and sum(v > 55 for v in errors) < 3):
        return True
    return green and field_diff < 6 and _green_scanout_matches(sample, low, high, patch[2] - patch[0])


def _amber_tone(r, g, b):
    # Lemon yellow (device Notes caret ~186,175,5; R-G 11) is still yellow:
    # R>=G>>B. The r-g sign separates yellow from green-tinted colours;
    # a 14-level gap wrongly excluded it.
    return r >= 165 and r - g >= 0 and g - b >= 48


def _green_tone(r, g, b):
    # WeChat's insertion caret: full phase ~(9,199,101), fading ~(155,226,184).
    return g >= 145 and g - r >= 45 and g - b >= 30


def _scoped_caret(image, box, scope):
    """A thin line inside a caller-verified input field, on that field's light background.

    Used only for a green caret, and only when the caller names the field (`scope`,
    pixels). The comparison area is the padded caret neighbourhood clipped to that
    field, so nearby glyphs, the field's own text and anything outside stay compared.
    """
    x0, y0, x1, y1 = box
    left, top, right, bottom = scope
    if not (left <= x0 < x1 <= right and top <= y0 < y1 <= bottom):
        return None
    w, h = image.size
    pad_x, pad_y = round(.06 * w), round(.02 * h)
    margin = max(3, round(.012 * w))
    for background_x in (x1 + margin, x0 - margin):
        if not left <= background_x < right:
            continue
        light = [min(image.getpixel((background_x, y))) >= 232 for y in range(y0, y1)]
        if sum(light) < .95 * len(light):
            continue
        field = (max(left, x0 - pad_x), max(top, y0 - pad_y), min(right, x1 + pad_x), min(bottom, y1 + pad_y))
        if field[0] < x0 - 1 and x1 + 2 < field[2] and field[1] < y0 - 1 and y1 + 1 < field[3]:
            return FieldCaret(box, field, background_x)
    return None


def _template_cycle(caret, images, frames, current, *, allow_white=True, amber=False, green=False):
    """Use complete periodic templates, including unchanged preedit underline."""
    x0, y0, x1, y1 = caret.box
    tone = _amber_tone if amber else _green_tone if green else None
    patch = x0 - 1, y0 - 1, x1 + 2, y1 + 1
    # A complete on-phase must remain a colored vertical line. The top three
    # quarters of an off-phase must reveal the field; its foot may intersect a
    # stable underline. That foot is retained verbatim in both templates.
    rows = range(y0 + 2, y1 - 2)
    upper = range(y0 + 2, y0 + round(.75 * (y1 - y0)))
    tried = set()
    for x in range(x0, x1):
        on, off = [], []
        for i, image in enumerate(images):
            colors = [image.getpixel((x, y)) for y in rows]
            if tone is not None:
                on_pixels = sum(tone(*color) for color in colors)
            else:
                blue = sum(b >= 150 and b - r >= 60 and b - g >= 25 and g >= 50 for r, g, b in colors)
                white = sum(min(c) >= 215 and max(c) - min(c) <= 65 for c in colors) if allow_white else 0
                on_pixels = max(blue, white)
            contrasts = [max(abs(a - b) for a, b in zip(image.getpixel((x, y)),
                         image.getpixel((caret.background_x, y)), strict=True)) for y in upper]
            if on_pixels >= .85 * len(colors) and sum(v >= 60 for v in contrasts) >= .85 * len(contrasts):
                on.append(i)
            if statistics.median(contrasts) <= 12 and sum(v <= 20 for v in contrasts) >= .85 * len(contrasts):
                # An amber or green line on a light field must disappear completely.
                # Its endpoints cannot become part of a new learned off phase.
                if tone is not None:
                    clearance = max(3, round(.006 * image.height))
                    pixels = image.crop((x0 - 1, y0 - clearance, x1 + 1, y1 + clearance)).tobytes()
                    colors = zip(pixels[::3], pixels[1::3], pixels[2::3], strict=True)
                    if any(tone(*color) for color in colors):
                        continue
                off.append(i)
        if not on or not off:
            continue
        # Use actual complete phase pairs, not extrema merged across different
        # JPEGs. A compression outlier cannot veto another fully proved cycle.
        for off_index in off:
            for on_index in on:
                pair = images[off_index].crop(patch).tobytes(), images[on_index].crop(patch).tobytes()
                if pair in tried:
                    continue
                tried.add(pair)
                if _matching_cycle(caret, patch, images, frames, current, off_index, on_index, x, pair,
                                   green=green):
                    return True
    return False


def _stable_underline(low, high, caret):
    """A preedit underline's horizontal footprint must not become a blink phase."""
    x0, y0, x1, y1 = caret.box
    width, height = x1 - x0, y1 - y0
    rows = []
    for y in range(y0 + round(.7 * height), min(high.height, y1 + 2)):
        spans = []
        for image in (low, high):
            spans.append([x for x in range(max(0, x0 - max(12, 3 * width)), min(image.width, x1 + width))
                          if (lambda r, g, b: b >= 150 and b - r >= 60 and b - g >= 25 and g >= 50)(
                              *image.getpixel((x, y)))])
        if all(sum(x < x0 for x in points) >= max(3, width) for points in spans):
            rows.append((sum(map(len, spans)), spans))
    if not rows:
        return True  # Ordinary insertion caret without a preedit underline.
    if len(rows) < 2:
        return False
    # The two fullest horizontal rows avoid JPEG fringes at the line's edges.
    for _, (off, on) in sorted(rows, key=lambda item: item[0], reverse=True)[:2]:
        if abs(min(off) - min(on)) > 1 or abs(max(off) - max(on)) > 1 or len(set(off) ^ set(on)) > 2:
            return False
    return True


def _opposite_caret_pixels(low, high, caret, patch, core_x):
    x0, y0, x1, y1 = caret.box
    upper = range(y0 + 2, y0 + round(.6 * (y1 - y0)))
    directions = [statistics.median(high.getpixel((core_x, y))[channel]
                                   - low.getpixel((core_x, y))[channel] for y in upper) for channel in (0, 1)]
    first, second = low.crop(patch).tobytes(), high.crop(patch).tobytes()
    wrong = 0
    for i in range(0, len(first), 3):
        wrong += any((direction < -20 and second[i + c] - first[i + c] > 40)
                     or (direction > 20 and second[i + c] - first[i + c] < -40)
                     for c, direction in enumerate(directions))
    return wrong


def _matching_cycle(caret, patch, images, frames, current, off_index, on_index, core_x, pair, *, green=False):
    low, high = pair
    low_image = images[off_index]
    if (not _stable_underline(low_image, images[on_index], caret)
            or _field_difference(images[on_index], low_image, caret, patch)
            + _opposite_caret_pixels(low_image, images[on_index], caret, patch, core_x) >= 6):
        return False
    if not all(_current_template_matches(image, low_image, caret, patch, low, high, green=green) for image in current):
        return False
    delta = [b - a for a, b in zip(low, high, strict=True)]
    norm = sum(v * v for v in delta)
    if not norm:
        return False
    states = []
    for image, frame in zip(images, frames, strict=True):
        sample = image.crop(patch).tobytes()
        if not _same_field(image, low_image, caret, patch):
            states.clear()  # Never stitch a blink across a text/field edit.
            continue
        if not _phase_matches(sample, low, high):
            continue
        alpha = sum((v - a) * d for v, a, d in zip(sample, low, delta, strict=True)) / norm
        state = 0 if alpha <= .1 else 1 if alpha >= .9 else None
        if state is not None and (not states or states[-1][0] != state):
            states.append((state, frame.arrived))
        if len(states) >= 3:
            a, b, c = states[-3:]
            if (a[0] == c[0] and a[0] != b[0] and .12 <= b[1] - a[1] <= 1.8
                    and .12 <= c[1] - b[1] <= 1.8):
                return True
    return False


def _temporal_carets(first, second, frames, regions, *, images=None, allow_white=True, allow_editor=False,
                     green_scope=None):
    images = images if images is not None else [_image(frame.frame) for frame in frames]
    if any(image.size != first.size for image in images):
        return []
    found = []
    for bounds in regions:
        ref = first.crop(bounds)
        border = [(x, y) for x in range(ref.width) for y in (0, ref.height - 1)] + [
            (x, y) for y in range(1, ref.height - 1) for x in (0, ref.width - 1)]
        patches = []
        for image in images:
            patch = image.crop(bounds)
            border_diffs = [
                max(abs(a - b) for a, b in zip(patch.getpixel(p), ref.getpixel(p), strict=True))
                for p in border
            ]
            if sum(value > 24 for value in border_diffs) <= 3 and max(border_diffs, default=0) <= 40:
                patches.append(patch)
        if len(patches) < 3:
            continue
        low, high = patches[0], patches[0]
        for patch in patches[1:]:
            low, high = ImageChops.darker(low, patch), ImageChops.lighter(high, patch)
        channels = ImageChops.difference(low, high).split()
        dynamic = ImageChops.lighter(ImageChops.lighter(*channels[:2]), channels[2])
        for points in _components(dynamic.point(lambda value: 255 if value > 40 else 0)):
            xs, ys = zip(*points, strict=True)
            x0, y0, x1, y1 = min(xs), min(ys), max(xs) + 1, max(ys) + 1
            bw, bh = x1 - x0, y1 - y0
            if not (0 < x0 < x1 < dynamic.width and 0 < y0 < y1 < dynamic.height
                    and 1 <= bw <= max(3, .012 * first.width)
                    and max(6, .008 * first.height) <= bh <= .05 * first.height and bh >= 3 * bw):
                continue
            box = x0 + bounds[0], y0 + bounds[1], x1 + bounds[0], y1 + bounds[1]
            dense = len(points) >= .65 * bw * bh
            caret = _bounded_field(first, box) if dense else None
            if caret and _template_cycle(caret, images, frames, (first, second), allow_white=allow_white):
                found.append(caret)
                continue
            if green_scope is not None and (dense or max(xs.count(x) for x in set(xs)) >= .95 * bh):
                scoped = _scoped_caret(first, box, green_scope)
                if scoped and _template_cycle(scoped, images, frames, (first, second), green=True):
                    found.append(scoped)
                    continue
            if allow_editor and (dense or max(xs.count(x) for x in set(xs)) >= .95 * bh):
                # JPEG fringes can widen the component with little extra area.
                # The complete box still has to match the learned phases.
                editor = _editor_caret(first, box)
                if editor and _template_cycle(editor, images, frames, (first, second), amber=True):
                    found.append(editor)
    return list(dict.fromkeys(found))


def _recent_evidence(frames):
    frames = tuple(frames)[-32:]
    if (len(frames) < 3 or len({getattr(frame, "generation", None) for frame in frames}) != 1
            or any(a.arrived >= b.arrived for a, b in zip(frames, frames[1:], strict=False))):
        return ()
    newest = frames[-1].arrived
    return tuple(frame for frame in frames if 0 <= newest - frame.arrived <= 4)


def temporal_focused_region(frame, frames, *, allow_white=False, accept_field=None):
    """Read-only field evidence from a same-text complete blink at the current frame.

    Callers still own freshness/session checks. This does not alter input scope or
    permit the current text to differ from the observed historical templates.
    """
    frames = _recent_evidence(frames)
    if len(frames) < 3:
        return None
    current = _image(frame)
    images = [_image(item.frame) for item in frames]
    if any(image.size != current.size for image in images):
        return None
    low, high = images[0], images[0]
    for image in images[1:]:
        low, high = ImageChops.darker(low, image), ImageChops.lighter(high, image)
    channels = ImageChops.difference(low, high).split()
    dynamic = ImageChops.lighter(ImageChops.lighter(*channels[:2]), channels[2]).point(
        lambda value: 255 if value > 40 else 0)
    if not 6 <= dynamic.histogram()[255] <= .02 * current.width * current.height:
        return None
    regions = []
    padding = max(3, round(.012 * current.width))
    for points in _components(dynamic):
        xs, ys = zip(*points, strict=True)
        width, height = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
        if (width <= max(3, .012 * current.width) and height >= 3 * width
                and max(6, .008 * current.height) <= height <= .05 * current.height):
            regions.append((max(0, min(xs) - padding), max(0, min(ys) - padding),
                            min(current.width, max(xs) + 1 + padding),
                            min(current.height, max(ys) + 1 + padding)))
    if not 1 <= len(regions) <= 4:
        return None
    found = _temporal_carets(current, current, frames, regions, images=images, allow_white=allow_white)
    if accept_field is not None:
        found = [caret for caret in found if accept_field(_field_region(caret, current.size))]
    if len(found) != 1:
        return None
    return _field_region(found[0], current.size)


def _periodic(caret, bright, frames, current):
    x0, y0, x1, y1 = caret.box
    field = caret.field
    left, top, _, _ = field
    # Keep history's field text/background identical, excluding only this thin line.
    original = bright.crop(field)
    extent = (x0 - 1 - left, y0 - 1 - top, x1 + 1 - left, y1 - top)
    patch = x0 - 1, y0 - 1, x1 + 2, y1 + 1  # Same inclusive extent as the eventual mask.
    observed = []
    for frame in frames:
        image = _image(frame.frame)
        if image.size != bright.size:
            continue
        r, g, b = ImageChops.difference(original, image.crop(field)).split()
        difference = ImageChops.lighter(ImageChops.lighter(r, g), b)
        ImageDraw.Draw(difference).rectangle(extent, fill=0)
        if sum(difference.histogram()[25:]) >= 6:
            continue
        # The off phase must actually reveal the same background beside the line.
        x, ys = (x0 + x1 - 1) // 2, range(y0 + 2, y1 - 2)
        contrasts = [max(abs(a - b) for a, b in zip(image.getpixel((x, y)),
                      image.getpixel((caret.background_x, y)), strict=True)) for y in ys]
        contrast = statistics.median(contrasts)
        state = (0 if max(contrasts) <= 20 else 1
                 if sum(value >= 60 for value in contrasts) >= .85 * len(contrasts) else None)
        observed.append((state, frame.arrived, image.crop(patch).tobytes(), contrast))
    off_samples = [item for item in observed if item[0] == 0]
    if not off_samples:
        return False
    off, on = min(off_samples, key=lambda item: item[3])[2], bright.crop(patch).tobytes()
    # Old blink history is not permission to erase a new character in the same
    # rectangle. Both current patches must have the same full spatial pattern.
    if not all(_phase_matches(image.crop(patch).tobytes(), off, on) for image in current):
        return False
    states = []
    for state, arrived, sample, _ in observed:
        if (state is not None and _phase_matches(sample, off, on)
                and (not states or states[-1][0] != state)):
            states.append((state, arrived))
    return any(a[0] == c[0] and a[0] != b[0]
               and .12 <= b[1] - a[1] <= 1.8 and .12 <= c[1] - b[1] <= 1.8
               for a, b, c in zip(states, states[1:], states[2:], strict=False))


def mask_caret_blink(before, after, diff, frames=(), scope=None):
    """Exempt a reversible thin caret only; surrounding text is always compared.

    `scope` (normalized [left, top, right, bottom]) is a caller-verified input field.
    Only inside it may a green caret (WeChat) be recognised; without it, green
    lines are always compared as content.
    """
    if len(frames) < 3:
        return
    hot = diff.point(lambda value: 255 if value > 24 else 0)
    count = hot.histogram()[255]
    if not 6 <= count <= .02 * diff.width * diff.height:
        return
    changed_lines = []
    for points in _components(hot):
        xs, ys = zip(*points, strict=True)
        width, height = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
        if (width <= max(4, .018 * diff.width) and height >= 3 * width
                and max(6, .008 * diff.height) <= height <= .055 * diff.height):
            changed_lines.append((min(xs), min(ys), max(xs) + 1, max(ys) + 1))
    if not 1 <= len(changed_lines) <= 4:
        return
    frames = _recent_evidence(frames)
    if len(frames) < 3:
        return
    first, second = (_image(frame) for frame in (before, after))
    if first.size != second.size:
        return
    if any(_image(frame.frame).size != first.size for frame in frames):
        return
    padding = max(3, round(.012 * first.width))
    pad_y = max(padding, round(.03 * first.height))
    regions = []
    for bounds in changed_lines:
        native = [round(value * first.size[i % 2] / diff.size[i % 2]) for i, value in enumerate(bounds)]
        regions.append((max(0, native[0] - padding), max(0, native[1] - pad_y),
                        min(first.width, native[2] + padding), min(first.height, native[3] + pad_y)))
    candidates = []
    for bright in (first, second):
        for caret in field_carets(bright, allow_white=True, regions=regions):
            x0, y0, x1, y1 = caret.box
            extent = (x0 - 1, y0 - 1, x1 + 1, y1)
            scaled = tuple(round(v * diff.size[i % 2] / bright.size[i % 2]) for i, v in enumerate(extent))
            if sum(diff.crop(scaled).histogram()[25:]) < 6:
                continue
            if _periodic(caret, bright, frames, (first, second)):
                candidates.append(scaled)
    if not candidates:
        # Editor evidence can exempt the thin blink from a comparison. It does
        # not provide a bounded field to callers that are trying to locate focus.
        green_scope = None
        if scope is not None:
            w, h = first.size
            green_scope = (round(scope[0] * w), round(scope[1] * h), round(scope[2] * w), round(scope[3] * h))
        for caret in _temporal_carets(first, second, frames, regions, allow_editor=True, green_scope=green_scope):
            x0, y0, x1, y1 = caret.box
            candidates.append(tuple(round(v * diff.size[i % 2] / first.size[i % 2])
                                    for i, v in enumerate((x0 - 1, y0 - 1, x1 + 1, y1))))
    # More than one animated line is ambiguous; do not infer several input foci.
    unique = set(candidates)
    if len(unique) == 1:
        ImageDraw.Draw(diff).rectangle(unique.pop(), fill=0)
