"""Small, dependency-free AssistiveTouch checks on mirrored screen images."""

import io
import math
from statistics import median

from PIL import Image, ImageChops

Point = tuple[float, float]
Box = tuple[float, float, float, float]
# Normalized icon regions for the default six-item top-level menu.
MENU_ICON_REGIONS = (
    (.35, .08, .65, .28),  # Top centre.
    (.08, .28, .32, .58), (.68, .28, .92, .58),
    (.08, .62, .32, .90), (.68, .62, .92, .90),
    (.35, .68, .65, .92),  # Bottom centre (Home).
)


def _small(frame: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(frame)).convert("RGB")
    scale = min(1, 240 / min(image.size))
    return image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.BILINEAR)


def _components(mask: bytearray, width: int, height: int):
    """Yield connected mask bounds and pixel counts, consuming the mask."""
    for start, value in enumerate(mask):
        if not value:
            continue
        mask[start] = 0
        todo = [start]
        x0 = x1 = start % width
        y0 = y1 = start // width
        count = 0
        while todo:
            index = todo.pop()
            x, y = index % width, index // width
            x0, x1 = min(x0, x), max(x1, x)
            y0, y1 = min(y0, y), max(y1, y)
            count += 1
            neighbors = []
            if x: neighbors.append(index - 1)
            if x + 1 < width: neighbors.append(index + 1)
            if y: neighbors.append(index - width)
            if y + 1 < height: neighbors.append(index + width)
            for neighbor in neighbors:
                if mask[neighbor]:
                    mask[neighbor] = 0
                    todo.append(neighbor)
        yield x0, y0, x1 + 1, y1 + 1, count


def _circle(gray: Image.Image, x: float, y: float, radius: float) -> list[int]:
    return [gray.getpixel((max(0, min(gray.width - 1, round(x + radius * math.cos(a * math.pi / 6)))),
                           max(0, min(gray.height - 1, round(y + radius * math.sin(a * math.pi / 6))))))
            for a in range(12)]


def find_button_target(frame: bytes, near: Point | None = None) -> tuple[Point, Box] | None:
    """Find an edge button by its concentric rings; `near` only ranks real candidates.

    iOS 26 captures use a pale centre with two grey rings. Older appearances use a
    white outline on a dark rounded square. A single filled pointer disc fails both.
    """
    image = _small(frame)
    gray = image.convert("L")
    w, h = image.size
    short = min(w, h)
    data = gray.tobytes()
    edge = [i for i in range(w*h) if min(i % w, w-1-i % w, i // w, h-1-i // w) < short * .2]
    candidates = []
    for threshold in (70, 110, 150, 190, 225, 245):
        mask = bytearray(w*h)
        for i in edge:
            mask[i] = data[i] >= threshold
        for left, top, right, bottom, count in _components(mask, w, h):
            bw, bh = right-left, bottom-top
            if not (.035*short <= bw <= .13*short and .8 <= bw/bh <= 1.25):
                continue
            if not .25 <= count/(bw*bh) <= .95:
                continue
            x, y, radius = (left+right-1)/2, (top+bottom-1)/2, (bw+bh)/4
            if min(x, w-x, y, h-y) > .15*short:
                continue
            color = image.getpixel((round(x), round(y)))
            if max(color) - min(color) > 45:
                continue
            centre = _circle(gray, x, y, radius*.35)
            core = median(centre)
            rim = _circle(gray, x, y, radius*.8)
            middle = _circle(gray, x, y, radius*1.35)
            outside = _circle(gray, x, y, radius*1.8)
            # Both sides of the ring must agree; a nearby glyph or icon is not enough.
            outlined = (bw >= .07*short and median(rim)-core > 25 and
                        sum(v > core+18 for v in rim) >= 10 and
                        sum(v < median(rim)-18 for v in middle) >= 10)
            # App glyphs in pale squares have uneven cores/rims, unlike the AT disc.
            uniform = (sum(abs(v-core) <= 12 for v in centre) >= 11 and
                       sum(abs(v-median(rim)) <= 15 for v in rim) >= 11)
            layered = (uniform and abs(core-median(rim)) < 18 and
                       core-median(middle) > 15 and median(middle)-median(outside) > 10 and
                       sum(v < core-10 for v in middle) >= 10 and
                       sum(v < median(middle)-6 for v in outside) >= 10)
            if not (outlined or layered):
                continue
            point = x/w, y/h
            distance = math.dist(point, near) if near is not None else 0
            contrast = max(median(rim)-core, core-median(outside))
            candidates.append((contrast - distance*80, point, (left/w, top/h, right/w, bottom/h)))
    return max(candidates)[1:] if candidates else None


def find_button(frame: bytes, near: Point | None = None) -> Point | None:
    target = find_button_target(frame, near)
    return target[0] if target is not None else None


def _icon_boxes(image: Image.Image, box: tuple[int, int, int, int]):
    panel = image.crop(box)
    w, h = panel.size
    data = panel.tobytes()
    threshold = max(140, median(data) + 55)
    mask = bytearray(value >= threshold for value in data)
    boxes = []
    for left, top, right, bottom, count in _components(mask, w, h):
        bw, bh = right-left, bottom-top
        if (.045*w <= bw <= .24*w and .045*h <= bh <= .24*h
                and .4 <= bw/bh <= 2.4 and count >= .001*w*h):
            boxes.append((left/w, top/h, right/w, bottom/h))
    return boxes


def _menu_icons(image: Image.Image, box: tuple[int, int, int, int]) -> bool:
    centres = [((left+right)/2, (top+bottom)/2) for left, top, right, bottom in _icon_boxes(image, box)]
    return all(any(left < x < right and top < y < bottom for x, y in centres)
               for left, top, right, bottom in MENU_ICON_REGIONS)



def find_icon_box(frame: bytes, panel: Box, point: Point) -> Box | None:
    """Return a visible menu icon's bounds only when it contains the chosen target."""
    image = _small(frame).convert("L")
    left, top, right, bottom = panel
    pixels = (round(left*image.width), round(top*image.height),
              round(right*image.width), round(bottom*image.height))
    for x0, y0, x1, y1 in _icon_boxes(image, pixels):
        box = (left+x0*(right-left), top+y0*(bottom-top),
               left+x1*(right-left), top+y1*(bottom-top))
        if box[0] < point[0] < box[2] and box[1] < point[1] < box[3]:
            return box
    return None


def find_panel(frame: bytes, before: bytes | None = None) -> Box | None:
    """Locate a rounded dark panel with the default six-icon arrangement.

    Supplying `before` rejects unchanged app panels. Ambiguous/custom layouts are
    rejected; a dark rectangle alone must never authorize a Home click.
    """
    image = _small(frame).convert("L")
    w, h = image.size
    short = min(w, h)
    data = image.tobytes()
    difference = None
    if before:
        old = _small(before).convert("L")
        if old.size == image.size:
            difference = ImageChops.difference(old, image)
    candidates = []
    icons = {}
    # Bounded grey bands separate a dark panel from an even darker app background.
    for low, high in ((0, 45), (0, 75), (0, 105), (0, 135), (20, 60), (45, 95), (75, 135)):
        mask = bytearray(low <= v <= high for v in data)
        for left, top, right, bottom, count in _components(mask, w, h):
            bw, bh = right-left, bottom-top
            if not (.35*short <= bw <= .96*w and .3*short <= bh <= .7*h and .65 <= bw/bh <= 1.65):
                continue
            if left < .02*w or right > .98*w or top < .08*h or bottom > .95*h:
                continue
            if not (.3*w < (left+right)/2 < .7*w and .2*h < (top+bottom)/2 < .8*h):
                continue
            fill = count/(bw*bh)
            if fill < .72:
                continue
            corners = (image.getpixel((round(left+fx*(bw-1)), round(top+fy*(bh-1))))
                       for fx, fy in ((.02, .02), (.98, .02), (.02, .98), (.98, .98)))
            if sum(not low <= value <= high for value in corners) < 3:
                continue
            box = left, top, right, bottom
            if box not in icons:
                icons[box] = _menu_icons(image, box)
            if not icons[box]:
                continue
            if difference:
                changed = difference.crop((left, top, right, bottom)).tobytes()
                if sum(v > 18 for v in changed)/len(changed) < .35:
                    continue
            candidates.append((fill*bw*bh, (left/w, top/h, right/w, bottom/h)))
    return max(candidates)[1] if candidates else None


def is_home_screen(frame: bytes) -> bool:
    """Conservative portrait Home signature: aligned app icons and a separate dock.

    This recognizes a conventional icon page, not arbitrary wallpapers/widgets or
    App Library. Unknown layouts fail closed. A dismissed menu or a changed app
    alone supplies none of this evidence.
    """
    from PIL import ImageFilter

    image = _small(frame).convert("L")
    w, h = image.size
    if not 1.4 < h/w < 2.5:
        return False
    edges = image.filter(ImageFilter.FIND_EDGES)
    data = edges.tobytes()
    # A full-width toolbar separator below the status bar is application chrome.
    if any(sum(data[y*w+x] > 45 for x in range(round(.05*w), round(.95*w))) > .72*w
           for y in range(round(.075*h), round(.16*h))):
        return False
    mask = bytearray(value > 18 for value in data)
    icons, docks = [], []
    for left, top, right, bottom, count in _components(mask, w, h):
        bw, bh = right-left, bottom-top
        x, y = (left+right)/(2*w), (top+bottom)/(2*h)
        if (.09*w <= bw <= .19*w and .8 <= bw/bh <= 1.25
                and count > .08*bw*bh and .07 < x < .93):
            icons.append((x, y, bw/w))
        if (.8*w < bw < .98*w and .09*h < bh < .19*h
                and .81 < top/h < .89 and .94 < bottom/h < .995):
            docks.append((left/w, top/h, right/w, bottom/h))
    if not docks:
        return False
    dock = max(docks, key=lambda box: box[2]-box[0])
    dock_icons = [icon for icon in icons if dock[0] < icon[0] < dock[2] and dock[1] < icon[1] < dock[3]]
    if len(dock_icons) < 3 or max(icon[1] for icon in dock_icons)-min(icon[1] for icon in dock_icons) > .025:
        return False
    grid = [icon for icon in icons if .13 < icon[1] < .76]
    rows = []
    for icon in sorted(grid, key=lambda item: item[1]):
        if not rows or abs(icon[1]-rows[-1][0][1]) > .025:
            rows.append([])
        rows[-1].append(icon)
    rows = [sorted(row) for row in rows if 3 <= len(row) <= 4]
    for index, row in enumerate(rows):
        for other in rows[index+1:]:
            if len(row) != len(other) or not .08 < other[0][1]-row[0][1] < .22:
                continue
            if (all(abs(a[0]-b[0]) < .025 for a, b in zip(row, other, strict=True))
                    and max(icon[2] for icon in row+other+dock_icons)
                    / min(icon[2] for icon in row+other+dock_icons) < 1.35):
                return True
    return False
