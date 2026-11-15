"""Dark-menu cursor evidence stays bounded to a real round, neutral overlay."""

import io

import pytest
from PIL import Image, ImageDraw

from hidagent.screen_compare import cursor_overlay_pair


def frames(*, shape="disc", texture=False, edited=False, ring_marks=None):
    background = Image.new("RGB", (500, 1080), (39, 37, 40))
    ImageDraw.Draw(background).rectangle((270, 735, 325, 780), fill=(106, 201, 226))
    if texture:
        ImageDraw.Draw(background).line((290, 697, 301, 722), fill=(70, 70, 70), width=3)
    marks = [(297, 694), (297, 726)]
    if ring_marks in {"stationary", "changed"}:
        for point in marks:
            background.putpixel(point, (52, 50, 53))
    outward, returned = background.copy(), background.copy()
    if ring_marks == "new":
        for point in marks:
            outward.putpixel(point, (47, 45, 48))
    if ring_marks == "changed":
        for point in marks:
            outward.putpixel(point, (60, 58, 61))
    draw = ImageDraw.Draw(outward)
    if shape == "disc":
        draw.ellipse((285, 698, 309, 722), fill=(49, 47, 50))
    elif shape == "ring":
        draw.ellipse((285, 698, 309, 722), outline=(49, 47, 50), width=2)
    elif shape == "letter":
        draw.line((285, 698, 285, 722, 309, 722), fill=(49, 47, 50), width=3)
    elif shape == "plus":
        draw.rectangle((290, 698, 304, 722), fill=(49, 47, 50))
        draw.rectangle((285, 703, 309, 717), fill=(49, 47, 50))
    else:
        draw.ellipse((285, 698, 309, 722), fill=(49, 38, 39))
    ImageDraw.Draw(returned).ellipse((285, 742, 309, 766), fill=(116, 145, 154))
    if edited:
        ImageDraw.Draw(returned).rectangle((350, 750, 357, 767), fill="white")
    encoded = []
    for image in (background, outward, returned):
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        encoded.append(buffer.getvalue())
    return encoded


def pair(images):
    return cursor_overlay_pair(*images, (.594, .698), 44/1080, (.18, .14), reference_pointer=None, axis=1)


def test_uniform_ten_level_gray_disc_pairs_with_return_over_blue_icon():
    result = pair(frames())
    assert result is not None
    assert result[0] == pytest.approx((297/500, 710/1080), abs=.003)
    assert result[1] == pytest.approx((297/500, 754/1080), abs=.003)


def test_two_stationary_ring_marks_do_not_obscure_a_complete_dim_disc():
    assert pair(frames(ring_marks="stationary")) == pair(frames())


@pytest.mark.parametrize("changes", [
    {"shape": "ring"}, {"shape": "letter"}, {"shape": "plus"}, {"shape": "colored"},
    {"texture": True}, {"edited": True}, {"ring_marks": "new"}, {"ring_marks": "changed"},
])
def test_dim_shapes_or_content_changes_do_not_authorize_a_landing(changes):
    assert pair(frames(**changes)) is None
