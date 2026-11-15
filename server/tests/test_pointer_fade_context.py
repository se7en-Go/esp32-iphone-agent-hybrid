"""Translucent pointer disappearance must preserve the text visible through it."""

import io
import math

import pytest
from PIL import Image, ImageDraw

from hidagent.screen_compare import observation_changed


def label_frame():
    image = Image.new("RGB", (500, 1080), (244, 243, 248))
    draw = ImageDraw.Draw(image)
    draw.line([(26, 77), (14, 89), (26, 101)], fill=(0, 122, 255), width=3)
    for x in (41, 61, 81, 101):
        draw.line([(x, 79), (x, 98), (x+13, 98), (x+13, 79)], fill=(0, 122, 255), width=2)
        draw.line([(x, 87), (x+13, 87)], fill=(0, 122, 255), width=2)
    return image


def encode(image):
    output = io.BytesIO()
    image.save(output, "JPEG", quality=90, subsampling=0)
    return output.getvalue()


def pointer(image, *, shape="circle", opacity=.46, color=(50, 50, 60)):
    mask = Image.new("L", image.size)
    draw = ImageDraw.Draw(mask)
    box = (56, 78, 81, 103)
    if shape == "circle":
        # Antialiased, translucent pointer edge rather than a hard painted dot.
        for y in range(76, 106):
            for x in range(54, 84):
                coverage = max(0, min(1, (13.5-math.hypot(x-68.5, y-90.5))/2))
                mask.putpixel((x, y), round(opacity*coverage*255))
    elif shape == "rectangle":
        draw.rectangle(box, fill=round(opacity*255))
    elif shape == "plus":
        draw.rectangle((56, 84, 81, 97), fill=round(opacity*255))
        draw.rectangle((62, 78, 75, 103), fill=round(opacity*255))
    return Image.composite(Image.new("RGB", image.size, color), image, mask)


@pytest.mark.parametrize("reverse", [False, True])
def test_round_translucent_pointer_can_appear_or_fade_over_unchanged_label(reverse):
    clean = label_frame()
    pair = encode(pointer(clean)), encode(clean)
    if reverse:
        pair = pair[::-1]
    assert not observation_changed(*pair)


@pytest.mark.parametrize("shape,opacity,color", [
    ("rectangle", .46, (50, 50, 60)), ("plus", .46, (50, 50, 60)),
    ("circle", 1., (50, 50, 60)), ("circle", .46, (240, 30, 50)),
])
def test_non_pointer_or_opaque_cover_is_not_the_translucent_exception(shape, opacity, color):
    clean = label_frame()
    assert observation_changed(encode(pointer(clean, shape=shape, opacity=opacity, color=color)), encode(clean))


@pytest.mark.parametrize("position", [(66, 82), (68, 88), (74, 91), (61, 94)])
@pytest.mark.parametrize("edit", ["new_stroke", "erase_stroke", "blue_stroke", "dot"])
def test_label_changes_under_the_fading_pointer_remain_visible(position, edit):
    clean = label_frame()
    edited = clean.copy()
    x, y = position
    if edit == "erase_stroke":
        x = 61  # erase a known vertical label stroke, never an already-blank pixel
    elif edit == "blue_stroke":
        x, y = 66, min(y, 89)  # add inside a glyph's blank cell, not over its outline
    draw = ImageDraw.Draw(edited)
    if edit == "dot":
        draw.rectangle((x, y, x+2, y+2), fill="black")
    else:
        color = {"new_stroke": "black", "erase_stroke": (244, 243, 248),
                 "blue_stroke": (0, 122, 255)}[edit]
        draw.rectangle((x, y, x+1, y+7), fill=color)
    assert observation_changed(encode(clean), encode(edited))
    assert observation_changed(encode(pointer(clean)), encode(edited))


def test_other_content_change_is_not_hidden_by_a_valid_pointer_fade():
    clean = label_frame()
    edited = clean.copy()
    ImageDraw.Draw(edited).text((200, 250), "new content", fill="black")
    assert observation_changed(encode(pointer(clean)), encode(edited))
