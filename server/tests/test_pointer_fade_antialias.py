"""A narrow antialiased neutral disc crossing a VPN icon preserves every glyph."""

import math

import pytest
from PIL import Image, ImageDraw
from test_pointer_fade_context import encode

from hidagent.screen_compare import observation_changed


def vpn_frame():
    image = Image.new("RGB", (500, 1080), (244, 243, 248))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 108, 480, 198), fill=(252, 252, 252))
    draw.rounded_rectangle((40, 107, 76, 143), radius=7, fill=(0, 122, 255))
    draw.line([(44, 112), (48, 121), (52, 112)], fill="white", width=2)
    draw.line([(56, 122), (56, 112), (61, 112), (61, 117), (56, 117)], fill="white", width=2)
    draw.line([(65, 122), (65, 112), (70, 122), (70, 112)], fill="white", width=2)
    return image


def pointer(image, *, color=(52, 54, 64), opacity=.4866):
    mask = Image.new("L", image.size)
    # Analytic circle at native pixels. Downsampling a 24.5px disc across the
    # high-contrast icon makes its thresholded footprint overstate its radius.
    cx, cy, radius, edge = 53.25, 112.75, 12.25, 1.9
    for y in range(97, 130):
        for x in range(37, 71):
            coverage = max(0, min(1, (radius + edge / 2 - math.hypot(x - cx, y - cy)) / edge))
            mask.putpixel((x, y), round(opacity * coverage * 255))
    return Image.composite(Image.new("RGB", image.size, color), image, mask)


@pytest.mark.parametrize("reverse", [False, True])
def test_vpn_edge_disc_can_appear_or_fade_without_changing_the_page(reverse):
    clean = vpn_frame()
    pair = encode(pointer(clean)), encode(clean)
    assert not observation_changed(*(pair[::-1] if reverse else pair))


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("edit", ["add_I", "erase_P", "black_stroke", "dot", "icon_edge", "remote_text"])
def test_narrow_circle_model_still_rejects_sparse_underlying_edits(reverse, edit):
    clean = vpn_frame()
    edited = clean.copy()
    draw = ImageDraw.Draw(edited)
    if edit == "add_I":
        draw.line((53, 110, 53, 122), fill="white", width=1)
    elif edit == "erase_P":
        draw.line((56, 112, 56, 122), fill=(0, 122, 255), width=2)
    elif edit == "black_stroke":
        draw.line((51, 110, 51, 121), fill="black", width=1)
    elif edit == "dot":
        draw.rectangle((52, 110, 55, 113), fill="black")
    elif edit == "icon_edge":
        draw.line((41, 108, 49, 108), fill="black", width=2)
    else:
        draw.text((200, 300), "different content", fill="black")
    assert observation_changed(encode(clean), encode(edited))
    pair = encode(pointer(clean)), encode(edited)
    assert observation_changed(*(pair[::-1] if reverse else pair))


@pytest.mark.parametrize("color,opacity", [((230, 30, 40), .4866), ((52, 54, 64), 1.)])
def test_colored_or_opaque_vpn_disc_is_not_a_pointer_exception(color, opacity):
    clean = vpn_frame()
    assert observation_changed(encode(pointer(clean, color=color, opacity=opacity)), encode(clean))
