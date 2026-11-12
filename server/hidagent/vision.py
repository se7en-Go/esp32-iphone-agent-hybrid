"""Small image checks on screen frames: did the screen change, and where is the pointer."""

import io

from PIL import Image, ImageChops


def _gray(jpeg: bytes) -> Image.Image:
    return Image.open(io.BytesIO(jpeg)).convert("L")


def changed(before: bytes | None, after: bytes | None, ignore: tuple = (), threshold: float = 1.0) -> bool:
    """True when the screen content changed, including small things such as a checkmark.

    `ignore` lists screen points (fractions) where the pointer was or went: those spots are masked, so pointer
    moves never count. The status bar (clock) is ignored too.
    """
    if not before or not after:
        return True
    a, b = _gray(before), _gray(after)
    if a.size != b.size:
        return True
    small = ImageChops.difference(a.resize((32, 64), Image.BILINEAR), b.resize((32, 64), Image.BILINEAR)).tobytes()
    if sum(small) / len(small) > threshold:  # a big change: new screen, scrolled list, keyboard
        return True
    w, h = a.size[0] // 2, a.size[1] // 2  # fine pass for small changes, at half resolution
    diff = ImageChops.difference(a.resize((w, h), Image.BILINEAR), b.resize((w, h), Image.BILINEAR)).tobytes()
    masks = [(px * w, py * h) for px, py in ignore]
    hits = 0
    for i, v in enumerate(diff):
        if v > 25:
            x, y = i % w, i // w
            if y > 0.06 * h and all((x - mx) ** 2 + (y - my) ** 2 > (0.05 * w) ** 2 for mx, my in masks):
                hits += 1
    return hits >= 6


def pointer(before: bytes, after: bytes, near: tuple[float, float], shift: float,
            reach: tuple[float, float] = (0.12, 0.08)) -> tuple[float, float] | None:
    """Where the pointer is in `before`, given it moved right by `shift` (fraction of width) in `after`."""
    found = pointer_move(before, after, near, shift, reach)
    return found[0] if found else None


def pointer_move(before: bytes, after: bytes, near: tuple[float, float], shift: float,
                 reach: tuple[float, float] = (0.12, 0.08), *, strict: bool = False):
    """The pointer in `before` and in `after` ((x, y), (x, y)), given it was nudged by about `shift`.

    First the moved disc: a small round blob of one flat colour that sits in both frames, displaced
    horizontally in the nudge direction (the iOS 26 pointer is a mid-grey filled circle; older ones a light
    dot). Static round things such as the AssistiveTouch button or a switch knob never move, so they never
    pair up. Only if no such blob shows up at all, the centroid of the changed pixels is used (shift/2 to
    the right of the first spot). None if unclear.
    """
    a, b = Image.open(io.BytesIO(before)).convert("RGB"), Image.open(io.BytesIO(after)).convert("RGB")
    if a.size != b.size:
        return None
    w, h = a.size
    rx, ry = reach
    left, top = max(0, int((near[0] - rx) * w)), max(int(0.05 * h), int((near[1] - ry) * h))  # skip the status bar
    box = (left, top, min(w, int((near[0] + rx) * w)), min(h, int((near[1] + ry) * h)))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return None
    diff = ImageChops.difference(a.crop(box).convert("L"), b.crop(box).convert("L"))
    spots = _changed_spots(diff, w)
    if spots is None:
        return None
    saw_disc = False
    best = None
    for sx0, sy0, sx1, sy1 in spots:
        margin = int(0.05 * w)
        win = (max(box[0], box[0] + sx0 - margin), max(box[1], box[1] + sy0 - margin),
               min(box[2], box[0] + sx1 + margin), min(box[3], box[1] + sy1 + margin))
        da, db = _discs(a.crop(win), w), _discs(b.crop(win), w)
        saw_disc = saw_disc or bool(da or db)
        for pa in da:
            for pb in db:
                score = _pair(pa, pb, shift, w)
                if score is not None:
                    ax, ay = (pa[0] + win[0]) / w, (pa[1] + win[1]) / h
                    bx, by = (pb[0] + win[0]) / w, (pb[1] + win[1]) / h
                    score += abs(ax - near[0]) + abs(ay - near[1])
                    if best is None or score < best[0]:
                        best = (score, (ax, ay), (bx, by))
    if best:
        return best[1], best[2]
    if saw_disc or strict:  # corrections require a paired disc, never just changed pixels
        return None
    return _centroid_move(diff, box, w, h, shift)


def _changed_spots(diff: Image.Image, w: int):
    """Bounding boxes (in `diff`) of compact clusters of changed pixels, ignoring JPEG noise; None if nothing."""
    cell = max(4, w // 90)
    cw, ch = (diff.size[0] + cell - 1) // cell, (diff.size[1] + cell - 1) // cell
    mask = diff.point(lambda v: 255 if v > 30 else 0)
    counts = mask.resize((cw, ch), Image.BOX).tobytes()  # mean of the mask per cell
    least = 255 * 3 / (cell * cell)  # a cell needs about three changed pixels
    hot = {(i % cw, i // cw) for i, v in enumerate(counts) if v >= least}
    if not hot:
        return None
    spots = []
    while hot:
        stack = [hot.pop()]
        xs, ys = [], []
        while stack:
            cx, cy = stack.pop()
            xs.append(cx)
            ys.append(cy)
            for dx in (-2, -1, 0, 1, 2):
                for dy in (-2, -1, 0, 1, 2):
                    n = (cx + dx, cy + dy)
                    if n in hot:
                        hot.remove(n)
                        stack.append(n)
        x0, y0, x1, y1 = min(xs) * cell, min(ys) * cell, (max(xs) + 1) * cell, (max(ys) + 1) * cell
        if x1 - x0 <= 0.3 * w and y1 - y0 <= 0.3 * w:  # bigger: new content, not a pointer
            spots.append((x0, y0, x1, y1))
    return spots


def _discs(img: Image.Image, w: int) -> list[tuple[float, float, float, tuple]]:
    """Filled round blobs of one flat colour, pointer sized: (cx, cy, diameter, colour) in `img` pixels."""
    iw, ih = img.size
    px = img.load()
    seen = bytearray(iw * ih)
    lo, hi = 0.02 * w, 0.09 * w
    found = []
    for y in range(ih):
        for x in range(iw):
            if seen[y * iw + x]:
                continue
            seed = px[x, y]
            seen[y * iw + x] = 1
            stack, pts, edge = [(x, y)], 0, False
            sx = sy = 0
            x0 = x1 = x
            y0 = y1 = y
            while stack:
                cx, cy = stack.pop()
                pts += 1
                sx += cx
                sy += cy
                x0, x1, y0, y1 = min(x0, cx), max(x1, cx), min(y0, cy), max(y1, cy)
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if not (0 <= nx < iw and 0 <= ny < ih):
                        edge = True
                        continue
                    i = ny * iw + nx
                    if seen[i]:
                        continue
                    p = px[nx, ny]
                    if (abs(p[0] - seed[0]) <= 26 and abs(p[1] - seed[1]) <= 26 and abs(p[2] - seed[2]) <= 26):
                        seen[i] = 1
                        stack.append((nx, ny))
            bw, bh = x1 - x0 + 1, y1 - y0 + 1
            if edge or not (lo <= bw <= hi and lo <= bh <= hi) or not 0.75 <= bw / bh <= 1.33:
                continue
            if 0.6 <= pts / (bw * bh) <= 0.92:  # a filled disc covers pi/4 of its box; rings and glyphs much less
                found.append((sx / pts, sy / pts, (bw + bh) / 2, seed))
    return found


def _pair(pa, pb, shift: float, w: int) -> float | None:
    """How well disc `pb` is disc `pa` nudged by `shift`; None if it cannot be."""
    if abs(pa[2] - pb[2]) > 0.3 * max(pa[2], pb[2]) or max(abs(u - v) for u, v in zip(pa[3], pb[3], strict=True)) > 40:
        return None
    dx, dy = pb[0] - pa[0], pb[1] - pa[1]
    # iOS accelerates the pointer, so a small nudge moves less than the calibrated scale says (12 units: 10 px
    # where 12 / span predicts 12.8 px on a 720 px wide iPhone 15 Pro Max frame); allow a broad band around it
    # (live, 15 Pro Max: 10.6 and 5.1 px for the same 12 unit nudge at different moments)
    if dx * shift <= 0 or not max(2.0, 0.25 * abs(shift) * w) <= abs(dx) <= 2.2 * abs(shift) * w:
        return None
    if abs(dy) > max(2.5, 0.15 * pa[2]):
        return None
    return abs(dy) / w


def _centroid_move(diff: Image.Image, box: tuple, w: int, h: int, shift: float):
    data = diff.tobytes()
    bw = box[2] - box[0]
    hits = [(i % bw, i // bw) for i, v in enumerate(data) if v > 30]
    if not 15 <= len(hits) <= len(data) // 4:  # nothing moved, or much more than a pointer
        return None
    xs, ys = [x for x, _ in hits], [y for _, y in hits]
    if max(xs) - min(xs) > 0.15 * w or max(ys) - min(ys) > 0.07 * h:  # not one compact spot
        return None
    cx = (sum(xs) / len(hits) + box[0]) / w
    cy = (sum(ys) / len(hits) + box[1]) / h
    return (cx - shift / 2, cy), (cx + shift / 2, cy)
