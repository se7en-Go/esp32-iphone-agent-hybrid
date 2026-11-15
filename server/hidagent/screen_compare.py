"""Screen-content comparisons, separate from pointer/calibration detection."""

import io
import math
from statistics import median

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .caret import mask_caret_blink
from .vision import _discs

STATUS_HEIGHT = 0.06
POINTER_RADIUS = 0.045  # fraction of width; includes JPEG edges and a small highlight


def _difference(before: bytes | None, after: bytes | None, width: int, *, ignore_status=True):
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
    if ignore_status:
        ImageDraw.Draw(diff).rectangle((0, 0, w, int(STATUS_HEIGHT * h)), fill=0)
    return diff


def _mask_locations(diff: Image.Image, ignore: tuple, hover: tuple) -> None:
    w, h = diff.size
    draw = ImageDraw.Draw(diff)
    for locations, fraction in ((ignore, POINTER_RADIUS), (hover, 0.12)):
        radius = fraction * w
        for x, y in locations:
            cx, cy = x * w, y * h
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=0)


def retain_context_regions(diff: Image.Image, regions) -> None:
    """Keep the anchors of an internally recognized system navigation control.

    Regions must come from NavigationContext, never from an external tap box.
    The full-screen default remains unchanged for ordinary actions.
    """
    if regions is None:
        return
    retained = Image.new("L", diff.size)
    width, height = diff.size
    for left, top, right, bottom in regions:
        box = (max(0, round(left * width)), max(0, round(top * height)),
               min(width, round(right * width)), min(height, round(bottom * height)))
        retained.paste(diff.crop(box), box)
    diff.paste(retained)


def _regions(diff: Image.Image, threshold: int):
    w, _ = diff.size
    return _point_regions({(i % w, i // w) for i, value in enumerate(diff.tobytes()) if value > threshold})


def _point_regions(hot):
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


def _rgb_at_size(frame: bytes | Image.Image, size: tuple[int, int]) -> Image.Image:
    if isinstance(frame, bytes):
        with Image.open(io.BytesIO(frame)) as original:
            frame = original.convert("RGB")
    elif frame.mode != "RGB":
        frame = frame.convert("RGB")
    return frame if frame.size == size else frame.resize(size, Image.Resampling.BILINEAR)


def _dim_overlay_disc(background, current, points, width):
    """A low-contrast candidate needs a complete gray disc on unchanged flat dark pixels."""
    xs, ys = zip(*points, strict=True)
    left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
    w, h = right - left + 1, bottom - top + 1
    if not (.03 * width <= min(w, h) <= max(w, h) <= .067 * width
            and .85 <= w / h <= 1.18 and .65 <= len(points) / (w * h) <= .88):
        return None
    cx, cy, radius = (left + right) / 2, (top + bottom) / 2, (w + h) / 4
    # Area alone also admits a thick '+' with a round-looking central core.
    # Check the entire silhouette against the observed radius, allowing only
    # one native pixel of edge quantization and small JPEG boundary holes.
    footprint = set(points)
    ideal = {(x, y) for y in range(top, bottom + 1) for x in range(left, right + 1)
             if math.hypot(x - cx, y - cy) <= radius}
    if (any(math.hypot(x - cx, y - cy) > radius + 1 for x, y in footprint)
            or len(footprint ^ ideal) > .12 * len(ideal)):
        return None
    if not (1.35 * radius < cx < current.width - 1.35 * radius
            and 1.35 * radius < cy < current.height - 1.35 * radius):
        return None
    core = [(x, y) for y in range(top, bottom + 1) for x in range(left, right + 1)
            if math.hypot(x - cx, y - cy) <= .65 * radius]
    ring = [(round(cx + 1.30 * radius * math.cos(i * math.tau / 24)),
             round(cy + 1.30 * radius * math.sin(i * math.tau / 24))) for i in range(24)]
    a, b = background.load(), current.load()

    def flat(colors, original_colors=None):
        color = tuple(median(channel) for channel in zip(*colors, strict=True))
        if not (24 <= min(color) <= max(color) <= 90 and max(color) - min(color) <= 12):
            return None

        def distance(first, second):
            return max(abs(p - q) for p, q in zip(first, second, strict=True))

        outliers = [i for i, pixel in enumerate(colors) if distance(pixel, color) > 6]
        if len(outliers) <= .05 * len(colors):
            return color
        if original_colors is not None and len(outliers) == 2 and all(
                distance(original_colors[i], color) >= 4 and distance(original_colors[i], colors[i]) <= 4
                for i in outliers):
            # Two stationary ring specks do not interrupt the pointer silhouette.
            # The unchanged center, contour and paired return are still required.
            return color
        return None

    inside, original = flat([b[p] for p in core]), flat([a[p] for p in core])
    outside = flat([b[p] for p in ring], [a[p] for p in ring])
    if inside is None or original is None or outside is None:
        return None
    if (not 8 <= sum(inside) / 3 - sum(outside) / 3 <= 26
            or max(abs(p - q) for p, q in zip(original, outside, strict=True)) > 6
            or any(max(abs(p - q) for p, q in zip(a[point], b[point], strict=True)) > 8 for point in ring)
            or sum(min(p - q for p, q in zip(b[point], a[point], strict=True)) >= 6
                   for point in core) < .85 * len(core)):
        return None
    return cx, cy, (w + h) / 2


def _translucent_pointer_fade(visible, background, points, width):
    """Prove a complete neutral translucent disc, retaining its underlying glyphs.

    The existing flat-colour detector cannot see a pointer across blue letters.
    Fit one opacity and one neutral colour, never a separate alpha per pixel.
    Only a bounded circular antialias edge may differ from the uniform core.
    """
    xs, ys = zip(*points, strict=True)
    left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
    w, h = right-left+1, bottom-top+1
    if not (.03*width <= min(w, h) <= max(w, h) <= .067*width
            and .85 <= w/h <= 1.18 and .65 <= len(points)/(w*h) <= .88):
        return False
    cx, cy, radius = (left+right)/2, (top+bottom)/2, (w+h)/4
    footprint = set(points)
    ideal = {(x, y) for y in range(top, bottom+1) for x in range(left, right+1)
             if math.hypot(x-cx, y-cy) <= radius}
    if (any(math.hypot(x-cx, y-cy) > radius+1 for x, y in footprint)
            or len(footprint ^ ideal) > .12*len(ideal)):
        return False
    margin = 4
    box = left-margin, top-margin, right+margin+1, bottom+margin+1
    if min(box[:2]) < 0 or box[2] > visible.width or box[3] > visible.height:
        return False
    a, b = visible.crop(box), background.crop(box)
    cx, cy = cx-box[0], cy-box[1]
    core = [(x, y) for y in range(a.height) for x in range(a.width)
            if math.hypot(x-cx, y-cy) <= .75*radius]
    al, bl = a.convert("L"), b.convert("L")
    mean_a = sum(al.getpixel(p) for p in core)/len(core)
    mean_b = sum(bl.getpixel(p) for p in core)/len(core)
    variance = sum((bl.getpixel(p)-mean_b)**2 for p in core)
    if variance/len(core) < 225:
        return False  # a new flat gray button cannot prove preserved underlying text
    scale = sum((al.getpixel(p)-mean_a)*(bl.getpixel(p)-mean_b) for p in core)/variance
    if not .30 <= scale <= .75:
        return False  # opaque shapes and weak arbitrary tint changes are unsupported
    alpha = 1-scale
    color = tuple((sum(a.getpixel(p)[c] for p in core)-scale*sum(b.getpixel(p)[c] for p in core))
                  / (len(core)*alpha) for c in range(3))
    if not 24 <= min(color) <= max(color) <= 100 or max(color)-min(color) > 20:
        return False
    gray = (mean_a-scale*mean_b)/alpha
    core_errors = [abs(al.getpixel(p)-scale*bl.getpixel(p)-alpha*gray) for p in core]
    if max(core_errors) > 12 or sum(core_errors)/len(core_errors) > 4:
        return False  # raw luminance retains even thin glyph edits; no blur here
    pixels = [(x, y) for y in range(a.height) for x in range(a.width)]
    around = [p for p in pixels if math.hypot(p[0]-cx, p[1]-cy) > radius+2]
    ring_errors = [max(abs(u-v) for u, v in zip(a.getpixel(p), b.getpixel(p), strict=True)) for p in around]
    if max(ring_errors) > 24 or sum(ring_errors)/len(ring_errors) > 6:
        return False
    # Center quantization is at most a quarter pixel; a thresholded bounding
    # box can overstate a downsampled radius by three quarters of a pixel.
    # Include a fractional antialias width for circles over sharp icon edges;
    # one radial model must still explain every original pixel and its colour.
    for dx in (0, -.25, .25):
        for dy in (0, -.25, .25):
            for dr in (0, -.25, -.5, .25, -.75):
                for edge in (1., 1.5, 2., 1.375):
                    coverages, errors = [], []
                    for p in pixels:
                        coverage = max(0., min(1., (radius+dr+edge/2-math.hypot(p[0]-cx-dx, p[1]-cy-dy))/edge))
                        coverages.append(alpha*coverage)
                        errors.append(abs(al.getpixel(p)-(bl.getpixel(p)*(1-alpha*coverage)+gray*alpha*coverage)))
                        if errors[-1] > 18:
                            break
                    if len(errors) != len(pixels) or sum(errors)/len(errors) > 3:
                        continue
                    predicted = b.copy()
                    for p, opacity in zip(pixels, coverages, strict=True):
                        predicted.putpixel(p, tuple(round(value*(1-opacity)+tint*opacity)
                                                    for value, tint in zip(b.getpixel(p), color, strict=True)))
                    # JPEG subsampled chroma rings at glyph edges. This check
                    # only smooths chroma evidence; the raw luma test above
                    # already checked every pixel against the same opacity.
                    aa, pp = a.filter(ImageFilter.GaussianBlur(1)), predicted.filter(ImageFilter.GaussianBlur(1))
                    chroma_errors = [max(abs(u-v) for u, v in zip(aa.getpixel(p), pp.getpixel(p), strict=True))
                                     for p in pixels]
                    if max(chroma_errors) <= 18 and sum(chroma_errors)/len(chroma_errors) <= 5:
                        return True
    return False


def _mask_translucent_pointer_fade(before, after, diff):
    candidates = [points for points in _regions(diff, 24) if len(points) >= 6]
    if len(candidates) != 1:
        return
    a, b = (_rgb_at_size(frame, diff.size) for frame in (before, after))
    points = candidates[0]
    if (_translucent_pointer_fade(a, b, points, diff.width)
            or _translucent_pointer_fade(b, a, points, diff.width)):
        for point in points:
            diff.putpixel(point, 0)


def cursor_overlay_pair(reference: bytes, before: bytes, after: bytes, near, shift, reach, *,
                        reference_pointer, report_reference=False, axis=0, caret_frames=(), context_regions=None):
    """Find motion of local cursor-sized overlays over a previously observed background."""
    with Image.open(io.BytesIO(reference)) as image:
        width, height = image.size
    x, y = near
    rx, ry = reach
    bounds = (max(0, round((x - rx) * width)), max(0, round((y - ry) * height)),
              min(width, round((x + rx) * width)), min(height, round((y + ry) * height)))
    with Image.open(io.BytesIO(reference)) as image:
        background = image.convert("RGB").crop(bounds)

    def overlays(frame):
        with Image.open(io.BytesIO(frame)) as image:
            if image.size != (width, height):
                return []
            current = image.convert("RGB").crop(bounds)
            red, green, blue = ImageChops.difference(background, current).split()
        delta = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        found = []
        for points in _regions(delta, 20):
            if len(points) < (.022 * width) ** 2:
                continue
            xs, ys = zip(*points, strict=True)
            w, h = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
            if not (.03 * width <= min(w, h) <= max(w, h) <= .067 * width
                    and .75 <= w / h <= 1.33 and .29 <= len(points) / (w * h) <= .90):
                continue
            found.append(((bounds[0] + (min(xs) + max(xs)) / 2) / width,
                          (bounds[1] + (min(ys) + max(ys)) / 2) / height, (w + h) / 2))
        # The gray pointer is only ~10 RGB levels above a dark AssistiveTouch
        # menu. Never lower the general diff threshold: these extra candidates
        # must independently prove a full, uniform disc on an unchanged field.
        for points in _regions(delta, 8):
            if len(points) < (.022 * width) ** 2:
                continue
            disc = _dim_overlay_disc(background, current, points, width)
            if disc is not None:
                x, y, size = disc
                center = (bounds[0] + x) / width, (bounds[1] + y) / height
                if not any(math.hypot((center[0] - px) * width, (center[1] - py) * height) < .02 * width
                           for px, py, _ in found):
                    found.append((*center, size))
        return found

    pairs = []
    old, new = overlays(before), overlays(after)
    for ax, ay, a_size in old:
        for bx, by, b_size in new:
            movement = (bx - ax, by - ay)
            along = movement[axis]
            across = movement[1 - axis] * (width, height)[1 - axis]
            if (along * shift > 0 and .25 * abs(shift) <= abs(along) <= 1.8 * abs(shift)
                    and abs(across) <= .16 * min(a_size, b_size)
                    and abs(a_size - b_size) <= .26 * min(a_size, b_size)):
                pairs.append(((ax, ay), (bx, by)))
    if len(pairs) != 1:
        return None
    pair = pairs[0]
    match, reference_pointer = verify_cursor_reference(
        reference, before, after, pair, reference_pointer, caret_frames=caret_frames,
        context_regions=context_regions)
    if not match:
        return None
    return (pair, reference_pointer) if report_reference else pair


def verify_cursor_reference(reference, before, after, pair, reference_pointer, *,
                            caret_frames=(), context_regions=None):
    """Check the original view against both halves of an observed pointer move."""
    if reference_pointer is None:
        # A translucent pointer can start over colored or textured content.
        # Its footprint must disappear in both motion frames.
        first, second = (_difference(reference, frame, 360, ignore_status=False) for frame in (before, after))
        if first is None or second is None:
            return False, None
        retain_context_regions(first, context_regions)
        retain_context_regions(second, context_regions)
        _mask_locations(first, (pair[0],), ())
        _mask_locations(second, (pair[1],), ())
        mask_clock_ticks(reference, before, first)
        mask_clock_ticks(reference, after, second)
        mask_caret_blink(reference, before, first, caret_frames)
        mask_caret_blink(reference, after, second, caret_frames)
        w, h = first.size
        footprints = []
        for points in _regions(ImageChops.darker(first, second), 24):
            xs, ys = zip(*points, strict=True)
            bw, bh = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
            if (.025 * w <= min(bw, bh) <= max(bw, bh) <= .067 * w
                    and .73 <= bw / bh <= 1.37 and .30 <= len(points) / (bw * bh) <= .89):
                footprints.append(((min(xs) + max(xs)) / (2 * w), (min(ys) + max(ys)) / (2 * h)))
        if len(footprints) > 1:
            return False, None
        reference_pointer = footprints[0] if footprints else None
    original = () if reference_pointer is None else (reference_pointer,)
    unchanged = all(not observation_changed(reference, frame, moved_pointer=(*original, point),
                                           caret_frames=caret_frames, context_regions=context_regions)
                    for frame, point in zip((before, after), pair, strict=True))
    return unchanged, reference_pointer


def _orange(pixel):
    r, g, b = pixel
    return r >= 145 and g >= 45 and r - g >= 25 and g - b >= 12 and r - b >= 55


def _orange_edge(pixel):
    r, g, b = pixel
    return r - b >= 10 and r - g >= 2 and g - b >= 3


def _clock_face(image: Image.Image, seed):
    """Locate a white circular dial on a dark tile around the changed orange pixels."""
    w, h = image.size
    sx, sy = seed
    reach = round(.21 * w)
    left, top = max(0, sx - reach), max(0, sy - reach)
    right, bottom = min(w, sx + reach + 1), min(h, sy + reach + 1)
    rgb = image.load()
    white = {(x, y) for y in range(top, bottom) for x in range(left, right)
             if min(rgb[x, y]) >= 160 and max(rgb[x, y]) - min(rgb[x, y]) <= 45}
    for points in _point_regions(white):
        if len(points) < (.08 * w) ** 2:
            continue
        xs, ys = zip(*points, strict=True)
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        if not (.10 * w <= min(bw, bh) <= max(bw, bh) <= .20 * w
                and .93 <= bw / bh <= 1.07 and .62 <= len(points) / (bw * bh) <= .84):
            continue
        cx, cy, radius = (x0 + x1) / 2, (y0 + y1) / 2, (bw + bh) / 4
        if math.hypot(sx - cx, sy - cy) > radius or not (
                1.10 * radius <= cx < w - 1.10 * radius and 1.10 * radius <= cy < h - 1.10 * radius):
            continue
        directions = [(math.cos(i * math.tau / 16), math.sin(i * math.tau / 16)) for i in range(16)]
        outer = [rgb[round(cx + dx * radius * 1.08), round(cy + dy * radius * 1.08)] for dx, dy in directions]
        if sum(max(pixel) <= 100 for pixel in outer) >= 14:
            return cx, cy, radius
    return None


def _radial_hand(points, face, reach):
    cx, cy, radius = face
    distances = [math.hypot(x - cx, y - cy) for x, y in points]
    tip = max(range(len(points)), key=distances.__getitem__)
    length = distances[tip]
    if not (reach * radius <= length <= .98 * radius and min(distances) <= .24 * radius):
        return False
    dx, dy = points[tip][0] - cx, points[tip][1] - cy
    return all(abs((x - cx) * dy - (y - cy) * dx) <= .14 * radius * length for x, y in points)


def _clock_hand_masks(first, second, face):
    """Return only two observed second hands and the narrow, shared black hand cores."""
    cx, cy, radius = face
    a, b = first.load(), second.load()
    dial = {(x, y) for y in range(int(cy - radius), math.ceil(cy + radius))
            for x in range(int(cx - radius), math.ceil(cx + radius))
            if math.hypot(x - cx, y - cy) <= .97 * radius}
    orange = []
    for pixels in (a, b):
        hand_pixels = {p for p in dial if _orange(pixels[p])}
        shapes = [set(points) for points in _point_regions(hand_pixels.copy())
                  if _radial_hand(points, face, .62)]
        # Subsampled JPEG chroma can split the thin hand into disconnected pieces.
        if not shapes and len(hand_pixels) >= 6 and _radial_hand(list(hand_pixels), face, .62):
            shapes = [hand_pixels]
        if len(shapes) != 1:
            return None
        orange.append(shapes[0])
    if len(orange[0] ^ orange[1]) < 3:
        return None
    # Exclude the pivot so intersecting hour/minute cores do not join into a bent component.
    dark = {p for p in dial if math.hypot(p[0] - cx, p[1] - cy) >= .12 * radius
            and max(*a[p], *b[p]) < 105}
    stable = [set(points) for points in _point_regions(dark) if _radial_hand(points, face, .40)]
    if not 1 <= len(stable) <= 2:
        return None
    return orange, set().union(*stable), dial


def mask_clock_ticks(before: bytes | Image.Image, after: bytes | Image.Image, diff: Image.Image) -> None:
    """Mask the movement of a matching analog Clock icon without hiding its face or neighbors."""
    w, h = diff.size
    histogram = diff.histogram()
    if not 6 <= sum(histogram[25:]) <= .015 * w * h:
        return
    first, second = (_rgb_at_size(frame, diff.size) for frame in (before, after))
    a, b, delta = first.load(), second.load(), diff.load()
    faces = []
    checks = 0
    for points in _regions(diff, 24):
        if len(points) < 6:
            continue
        seed = next((p for p in points if _orange(a[p]) or _orange(b[p])), None)
        if seed is None or any(math.hypot(seed[0] - x, seed[1] - y) <= r for x, y, r in faces):
            continue
        checks += 1
        if checks > 4:
            return
        face = _clock_face(first, seed)
        matching = _clock_face(second, seed) if face is not None else None
        if matching is None or any(abs(x - y) > 1 for x, y in zip(face, matching, strict=True)):
            continue
        masks = _clock_hand_masks(first, second, face)
        if masks is None:
            continue
        faces.append(face)
        hands, dark, dial = masks
        orange = hands[0] | hands[1]
        for core, margin in ((orange, max(1, round(.10 * face[2]))), (dark, max(1, round(.06 * face[2])))):
            for x, y in core:
                for dy in range(-margin, margin + 1):
                    for dx in range(-margin, margin + 1):
                        p = x + dx, y + dy
                        if p not in dial or not delta[p]:
                            continue
                        colors = a[p], b[p]
                        spreads = tuple(max(pixel) - min(pixel) for pixel in colors)
                        if core is orange:
                            orange_edge = tuple(_orange_edge(pixel) for pixel in colors)
                            dial_colors = all(s <= 42 or e for s, e in zip(spreads, orange_edge, strict=True))
                            if (any(orange_edge) and dial_colors) or (max(spreads) <= 30 and delta[p] <= 55):
                                delta[p] = 0
                        elif delta[p] <= 100 and max(spreads) <= 25:
                            delta[p] = 0
        # JPEG chroma can extend several pixels past an otherwise well-defined tip.
        # Restrict this allowance to warm tint against a neutral dial tone.
        for index, hand in enumerate(hands):
            cx, cy, radius = face
            tx, ty = max(hand, key=lambda point: math.hypot(point[0] - cx, point[1] - cy))
            margin = max(2, round(.30 * radius))
            layers = (a, b) if index == 0 else (b, a)
            for y in range(ty - margin, ty + margin + 1):
                for x in range(tx - margin, tx + margin + 1):
                    p = (x, y)
                    if p not in dial or not delta[p] or math.hypot(x - tx, y - ty) > margin:
                        continue
                    warm, neutral = layers[0][p], layers[1][p]
                    if (min(warm) >= 90 and max(neutral) - min(neutral) <= 38
                            and warm[0] - warm[2] >= 12
                            and warm[0] >= warm[1] >= warm[2]
                            and abs(sum(warm) - sum(neutral)) <= 150):
                        delta[p] = 0


def _mask_pointer_discs(before: bytes, after: bytes, diff: Image.Image, max_discs=None) -> None:
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
    masks = set()
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
                masks.add((x - radius, y - radius, x + radius, y + radius))
    if max_discs is not None and len(masks) > max_discs:
        return
    draw = ImageDraw.Draw(diff)
    for bounds in masks:
        draw.ellipse(bounds, fill=0)


def mask_pointer_highlights(before: bytes | Image.Image, after: bytes | Image.Image, diff: Image.Image,
                            unmasked: Image.Image | None = None) -> tuple[tuple, tuple]:
    """Ignore neutral, hollow, rounded hover outlines, including an unknown old hover.

    Mask only the outline and its JPEG edge; content inside the control remains visible.
    Solid patches, colored outlines and changes to a control's label are not highlights.
    Return recognized centers in each frame, for disc-to-highlight continuity checks.
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
        return (), ()
    draw = ImageDraw.Draw(diff)
    recognized = [[], []]
    for index, frame in enumerate((before, after)):
        if isinstance(frame, Image.Image):
            image = frame
        else:
            with Image.open(io.BytesIO(frame)) as original:
                image = original.convert("RGB")
        if image.size != diff.size:
            image = image.resize(diff.size, Image.Resampling.BILINEAR)
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
            recognized[index].append((cx, cy))
            for x, y in points:
                draw.rectangle((x - 1, y - 1, x + 1, y + 1), fill=0)
    return tuple(recognized[0]), tuple(recognized[1])


def observation_changed(before: bytes | None, after: bytes | None, *, input_region=None,
                        moved_pointer: tuple = (), caret_frames=(), context_regions=None) -> bool:
    """Reject content edits while tolerating clock and recognized pointer shapes.

    Recognized hover outlines and small compact discs may fade or move. Other larger
    regions, several edits, scrolling and broad low-contrast changes invalidate it.
    """
    if before and before == after:
        return False
    diff = _difference(before, after, 360)
    if diff is None:
        return True
    retain_context_regions(diff, context_regions)
    if moved_pointer:
        _mask_locations(diff, moved_pointer, ())
    if input_region is not None:
        w, h = diff.size
        ImageDraw.Draw(diff).rectangle(tuple(round(v * (w if i % 2 == 0 else h))
                                             for i, v in enumerate(input_region)), fill=0)
    mask_caret_blink(before, after, diff, caret_frames)
    mask_clock_ticks(before, after, diff)
    mask_pointer_highlights(before, after, diff)
    _mask_pointer_discs(before, after, diff, max_discs=2)
    _mask_translucent_pointer_fade(before, after, diff)
    values = diff.tobytes()
    if sum(values) / len(values) > 0.8:
        return True
    return any(len(points) >= 6 for points in _regions(diff, 24))


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
    mask_clock_ticks(before, after, unmasked)
    diff = unmasked.copy()
    _mask_locations(diff, ignore, hover)
    values = diff.tobytes()
    if sum(values) / len(values) > 3:  # a screen transition cannot be just a pair of small discs
        return True
    # A known target can overlap an unknown old hover; recognize the complete outline.
    mask_pointer_highlights(before, after, diff, unmasked)
    _mask_pointer_discs(before, after, diff)
    values = diff.tobytes()
    return sum(values) / len(values) > 0.8 or any(len(region) >= 6 for region in _regions(diff, 25))
