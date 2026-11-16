"""Real dt2 double-tap sample: faded pointer over bold text, return == reference.

Fixtures are the device sample (dt2 double-tap on "你好", 500x1080):
- outward.jpg: pointer-frame-09, the escaped disc (clean, at (125,185)).
- return.jpg: pointer-frame-11, the pointer settled back at the target
  (126,241), faded over the "你好" glyphs.
- mid_return.jpg: pointer-frame-10, the pointer mid-return at (126,214).

The reference-based cursor_overlay_pair fails here: the return frame matches
the position reference (the pointer came back to where it started), so a
difference against the reference has nothing to pair. The direct
outward/return translucent pair must find it, and the pre-click
observation_changed must still accept the pair (nothing else changed).
"""
import io
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

from hidagent.screen_compare import (
    _faded_pointer_disc,
    _regions,
    observation_changed,
    translucent_pointer_pair,
)

FIXTURES = Path(__file__).parent / "fixtures" / "dt2_text_pointer"
NEAR = (0.2545, 0.2303)
REACH = (0.153, 0.235)
SHIFT = 0.06336  # return displacement (down), extended probe inverted


def load(name):
    return (FIXTURES / name).read_bytes()


def test_translucent_pair_finds_return_over_text():
    pair, _ = translucent_pointer_pair(load("outward.jpg"), load("return.jpg"),
                                       NEAR, SHIFT, REACH, axis=1)
    assert pair is not None
    (ax, ay), (bx, by) = pair
    # Outward disc at the escaped position, return disc at the target.
    assert abs(ax * 500 - 126) <= 3 and abs(ay * 1080 - 188) <= 3
    assert abs(bx * 500 - 126) <= 3 and abs(by * 1080 - 241) <= 3
    # The return landing is within the paired-miss tolerance of the target.
    assert abs(bx - NEAR[0]) <= 0.12 and abs(by - NEAR[1]) <= 0.12


def test_translucent_pair_return_is_otherwise_unchanged():
    pair, _ = translucent_pointer_pair(load("outward.jpg"), load("return.jpg"),
                                       NEAR, SHIFT, REACH, axis=1)
    assert pair is not None
    assert not observation_changed(load("outward.jpg"), load("return.jpg"),
                                   moved_pointer=pair)


def test_translucent_pair_rejects_no_movement():
    assert translucent_pointer_pair(load("outward.jpg"), load("outward.jpg"),
                                    NEAR, SHIFT, REACH, axis=1) is None


def test_translucent_pair_rejects_reversed_shift():
    assert translucent_pointer_pair(load("outward.jpg"), load("return.jpg"),
                                    NEAR, -SHIFT, REACH, axis=1) is None


def test_translucent_pair_rejects_wrong_axis():
    assert translucent_pointer_pair(load("outward.jpg"), load("return.jpg"),
                                    NEAR, SHIFT, REACH, axis=0) is None


def test_translucent_pair_rejects_mid_return_frame():
    # Frame 10 caught the pointer mid-return at (126,214), off the target
    # word; the pair must not authorize a landing there.
    assert translucent_pointer_pair(load("outward.jpg"), load("mid_return.jpg"),
                                    NEAR, SHIFT, REACH, axis=1) is None


def _glyph_clipped_pair(*, protrusion: bool = False):
    width, height = 500, 1080
    base = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(base)
    # Dark horizontal glyph strokes near y=240..265 (clipping bottom rows so
    # vertical delta spans h=26 while horizontal span stays w=29) + vertical
    # stroke for high background variance inside the faded disc core.
    draw.rectangle((146, 238, 182, 241), fill=(25, 25, 25))
    draw.rectangle((146, 265, 182, 268), fill=(25, 25, 25))
    draw.rectangle((160, 241, 166, 265), fill=(35, 35, 35))

    def png(image):
        buf = io.BytesIO()
        image.save(buf, "PNG")
        return buf.getvalue()

    outward = base.copy()
    odraw = ImageDraw.Draw(outward)
    # Clean escaped disc at (163, 303), d=25, rgb=(151, 149, 150)
    odraw.ellipse((151, 291, 175, 315), fill=(151, 149, 150))

    ret = base.copy()
    px_base = base.load()
    px_ret = ret.load()
    cx, cy = 163.0, 252.5
    alpha = 0.51
    tint = (66, 64, 68)
    def blend(x, y):
        px_ret[x, y] = tuple(round((1 - alpha) * b + alpha * t)
                             for b, t in zip(px_base[x, y], tint, strict=True))

    # Draw a 29px-wide, 26px-tall disc (shaved top/bottom past y=240..265) so
    # w=29, h=26 gives (w+h)/4 + 1 = 14.75 while horizontal rim is at ~15.4 px.
    for y in range(240, 266):
        for x in range(149, 178):
            dx, dy = (x - cx) / 14.5, (y - cy) / 13.0
            if dx * dx + dy * dy <= 1.0:
                blend(x, y)
    # Force an extreme left/right pixel inside 29x26 at distance ~15.4 (> 14.75, <= 15.5)
    for x in (149, 177):
        for y in (248, 257):
            blend(x, y)
    if protrusion:
        # 4-connected corner spike inside the 29x26 bounding box (keeping w=29,
        # h=26, max_r=14.5) out to (177, 240), distance ~18.8 > 15.5.
        for x, y in ((172, 243), (172, 242), (173, 242), (174, 242),
                     (174, 241), (175, 241), (176, 241), (176, 240), (177, 240)):
            blend(x, y)
    return outward, ret, png(outward), png(ret)


def _ringing_30x28_pair(extra_outliers=3):
    """Build a 30x28 faded disc over strokes with weak JPEG ringing beyond max_r+1."""
    base = Image.new("RGB", (500, 1080), (255, 255, 255))
    draw = ImageDraw.Draw(base)
    for y in range(210, 270, 5):
        draw.line((110, y, 220, y), fill=(25, 25, 25), width=2)
    for x in range(135, 195, 7):
        draw.line((x, 210, x, 270), fill=(25, 25, 25), width=2)

    def png(image):
        buf = io.BytesIO()
        image.save(buf, "PNG")
        return buf.getvalue()

    outward = base.copy()
    ImageDraw.Draw(outward).ellipse((150, 285, 174, 309), fill=(151, 149, 150))

    ret = base.copy()
    px_base = base.load()
    px_ret = ret.load()
    cx, cy = 162.5, 242.5
    alpha = 0.51
    tint = (63, 60, 64)
    in_disc = set()
    for y in range(229, 257):
        for x in range(148, 178):
            if math.hypot(x - cx, y - cy) <= 15.2:
                px_ret[x, y] = tuple(round((1 - alpha) * b + alpha * t)
                                     for b, t in zip(px_base[x, y], tint, strict=True))
                in_disc.add((x, y))
    cands = [
        (x, y)
        for y in range(229, 257)
        for x in range(148, 178)
        if (x, y) not in in_disc and 16.0 < math.hypot(x - cx, y - cy) <= 17.3
    ]

    def set_ring(rx, ry):
        r0, g0, b0 = px_base[rx, ry]
        px_ret[rx, ry] = (r0 - 14, g0 - 14, b0 - 14) if r0 > 128 else (r0 + 20, g0 + 20, b0 + 20)

    for x, y in cands[:extra_outliers]:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            bx, by = x + dx, y + dy
            if (bx, by) not in in_disc and math.hypot(bx - cx, by - cy) <= 16.0:
                set_ring(bx, by)
                in_disc.add((bx, by))
                break
        set_ring(x, y)
    return outward, ret, png(outward), png(ret)


def test_faded_pointer_disc_accepts_glyph_clipped_29x26_disc():
    _, _, b_out, b_ret = _glyph_clipped_pair()
    result = translucent_pointer_pair(b_out, b_ret, (0.3257, 0.2308), -0.0616, (0.12, 0.12), axis=1)
    assert result is not None
    pair, ref = result
    assert ref is None
    (ax, ay), (bx, by) = pair
    assert abs(ax * 500 - 163) <= 2 and abs(ay * 1080 - 303) <= 2
    assert abs(bx * 500 - 163) <= 2 and abs(by * 1080 - 252.5) <= 2


def test_faded_pointer_disc_rejects_protrusion_past_max_radius():
    outward, ret, _, _ = _glyph_clipped_pair(protrusion=True)
    red, green, blue = ImageChops.difference(outward, ret).split()
    delta = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    pts = max(_regions(delta, 10), key=lambda region: sum(1 for _, y in region if y < 275))
    xs, ys = zip(*pts, strict=True)
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    max_r = max(max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) / 2
    assert max(math.hypot(x - cx, y - cy) for x, y in pts) > max_r + 1
    assert not _faded_pointer_disc(ret, outward, pts, outward.width)


def test_faded_pointer_disc_accepts_bounded_jpeg_ringing_outliers():
    outward, ret, b_out, b_ret = _ringing_30x28_pair(extra_outliers=3)
    red, green, blue = ImageChops.difference(outward, ret).split()
    delta = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    pts = max(_regions(delta, 10), key=lambda region: sum(1 for _, y in region if y < 275))
    xs, ys = zip(*pts, strict=True)
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    max_r = max(max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) / 2
    outliers = [p for p in pts if math.hypot(p[0] - cx, p[1] - cy) > max_r + 1]
    assert len(outliers) == 3
    result = translucent_pointer_pair(b_out, b_ret, (0.3257, 0.2308), -0.0505, (0.12, 0.12), axis=1)
    assert result is not None
    pair, ref = result
    assert ref is None
    (ax, ay), (bx, by) = pair
    assert abs(ax * 500 - 162.5) <= 2 and abs(ay * 1080 - 297) <= 2
    assert abs(bx * 500 - 162.5) <= 2 and abs(by * 1080 - 242.5) <= 2


def test_faded_pointer_disc_rejects_excess_ringing_outliers():
    outward, ret, _, _ = _ringing_30x28_pair(extra_outliers=9)
    red, green, blue = ImageChops.difference(outward, ret).split()
    delta = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    pts = max(_regions(delta, 10), key=lambda region: sum(1 for _, y in region if y < 275))
    xs, ys = zip(*pts, strict=True)
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    max_r = max(max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) / 2
    outliers = [p for p in pts if math.hypot(p[0] - cx, p[1] - cy) > max_r + 1]
    assert len(outliers) == 9
    assert not _faded_pointer_disc(ret, outward, pts, outward.width)
