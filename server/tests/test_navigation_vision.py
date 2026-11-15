"""Visible iOS Back geometry; no stored user screenshots or phone operations."""

import io

import pytest
from PIL import Image, ImageDraw

from hidagent.navigation_vision import find_back_target


def navigation_frame(*, arrow="back", label=True, y=.081, second=False, color="#007aff"):
    image = Image.new("RGB", (500, 1080), "#f3f3f8")
    draw = ImageDraw.Draw(image)
    cy = round(y*1080)
    if arrow == "back":
        points = ((28, cy-12), (16, cy), (28, cy+12))
    elif arrow == "forward":
        points = ((16, cy-12), (28, cy), (16, cy+12))
    elif arrow == "down":
        points = ((10, cy-8), (22, cy+4), (34, cy-8))
    elif arrow == "stem":
        points = ((28, cy-12), (16, cy), (28, cy+12))
        draw.line((16, cy-12, 16, cy+12), fill=color, width=3)
    else:
        points = None
    if points:
        draw.line(points, fill=color, width=3)
    if second:
        draw.line(((44, cy-12), (32, cy), (44, cy+12)), fill=color, width=3)
    if label:
        for x in ((70, 93) if second else (42, 65)):
            draw.line(((x, cy-10), (x, cy+10), (x+14, cy+10), (x+14, cy-10)), fill=color, width=3)
            draw.line((x, cy, x+14, cy), fill=color, width=3)
    output = io.BytesIO()
    image.save(output, "JPEG", quality=90)
    return output.getvalue()


def test_back_target_requires_visible_chevron_and_same_row_label():
    target = find_back_target(navigation_frame())
    assert target is not None
    assert target.box[0] < target.center[0] < target.box[2]
    assert target.box[1] < target.center[1] < target.box[3]
    assert target.center == pytest.approx((.095, .081), abs=.015)
    assert target.box[2] < .20


@pytest.mark.parametrize("changes", [
    {"arrow": None}, {"label": False}, {"arrow": "forward"}, {"arrow": "down"}, {"arrow": "stem"},
    {"y": .4}, {"second": True}, {"color": "#999999"},
])
def test_non_back_or_ambiguous_pixels_never_authorize_tap(changes):
    assert find_back_target(navigation_frame(**changes)) is None


def test_circle_and_top_left_text_are_not_back_chevrons():
    image = Image.new("RGB", (500, 1080), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((10, 75, 36, 101), outline="#007aff", width=3)
    draw.text((45, 80), "Settings", fill="#007aff")
    data = io.BytesIO()
    image.save(data, "PNG")
    assert find_back_target(data.getvalue()) is None
