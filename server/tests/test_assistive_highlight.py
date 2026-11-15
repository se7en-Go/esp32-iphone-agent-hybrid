"""A blue hovered Home glyph needs the other five original menu icons."""

import io

import pytest
from PIL import Image, ImageDraw

from hidagent.assistive_vision import find_icon_target, find_panel, is_home_screen
from hidagent.freshness import FrameSnapshot
from hidagent.navigation_context import from_navigation

BOX = (.12, .32, .88, .67)
SLOTS = ((.5, .18), (.2, .5), (.8, .5), (.2, .8), (.8, .8))


def highlighted_menu(*, case="home", quality=85):
    image = Image.new("RGB", (360, 780), "#cccce0")
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = BOX[0]*360, BOX[1]*780, BOX[2]*360, BOX[3]*780
    width, height = right-left, bottom-top
    draw.rounded_rectangle((left, top, right, bottom), radius=30, fill="#282828")
    for index, (fx, fy) in enumerate(SLOTS):
        if case == "missing_white" and index == 0:
            continue
        x, y = left+fx*width, top+fy*height
        draw.ellipse((x-12, y-12, x+12, y+12), outline="white", width=4)
    x, y = left+.5*width, top+.8*height
    blue = (55, 115, 190)  # Luma below the original 140; blue channel is visible.
    if case == "wrong_slot":
        x, y = left+.5*width, top+.48*height
    if case == "large":
        draw.ellipse((x-42, y-42, x+42, y+42), fill=blue)
    elif case == "line":
        draw.rectangle((x-25, y-2, x+25, y+2), fill=blue)
    elif case == "square":
        draw.rectangle((x-12, y-12, x+12, y+12), fill=blue)
    elif case == "square_outline":
        draw.rectangle((x-12, y-12, x+12, y+12), outline=blue, width=4)
    elif case == "diamond":
        draw.polygon(((x, y-12), (x+12, y), (x, y+12), (x-12, y)), fill=blue)
    elif case == "letter_h":
        draw.line((x-9, y-12, x-9, y+12), fill=blue, width=4)
        draw.line((x+9, y-12, x+9, y+12), fill=blue, width=4)
        draw.line((x-9, y, x+9, y), fill=blue, width=4)
    elif case == "letter_a":
        draw.line((x-10, y+12, x, y-12, x+10, y+12), fill=blue, width=4)
        draw.line((x-6, y+3, x+6, y+3), fill=blue, width=4)
    elif case in {"plus", "plus_40", "plus_45"}:
        arm = {"plus": 7, "plus_40": 5, "plus_45": 5.625}[case]
        draw.rectangle((x-12, y-arm, x+12, y+arm), fill=blue)
        draw.rectangle((x-arm, y-12, x+arm, y+12), fill=blue)
    elif case == "two":
        for offset in (-18, 18):
            draw.ellipse((x+offset-8, y-8, x+offset+8, y+8), fill=blue)
    else:
        color = {"gray": (105, 105, 105), "red": (190, 60, 60), "green": (50, 190, 60)}.get(case, blue)
        draw.ellipse((x-12, y-12, x+12, y+12), fill=color)
    output = io.BytesIO()
    image.save(output, "JPEG", quality=quality)
    return output.getvalue()


@pytest.mark.parametrize("quality", [80, 85, 95])
def test_hovered_home_retains_panel_and_actual_glyph_target(quality):
    frame = highlighted_menu(quality=quality)
    panel = find_panel(frame)
    assert panel == pytest.approx(BOX, abs=.012)
    left, top, right, bottom = panel
    target = find_icon_target(frame, panel, (left+.5*(right-left), top+.8*(bottom-top)))
    assert target is not None
    center, bounds = target
    assert center == pytest.approx((.5, BOX[1]+.8*(BOX[3]-BOX[1])), abs=.012)
    assert bounds[0] < center[0] < bounds[2] and bounds[1] < center[1] < bounds[3]
    assert bounds[2]-bounds[0] < .1 and bounds[3]-bounds[1] < .05
    context = from_navigation("home", FrameSnapshot(frame, 1., None, 1, True))
    assert context is not None and context.kind == "menu"
    assert context.regions == (panel,) and context.target.center == center and context.target.box == bounds
    assert find_panel(frame, before=frame) is None
    assert not is_home_screen(frame)


@pytest.mark.parametrize("case", ["missing_white", "wrong_slot", "large", "line", "square", "two",
                                  "square_outline", "diamond", "letter_h", "letter_a", "plus", "plus_40", "plus_45",
                                  "gray", "red", "green"])
@pytest.mark.parametrize("quality", [80, 85, 95])
def test_color_alone_or_incomplete_menu_never_supplies_home(case, quality):
    assert find_panel(highlighted_menu(case=case, quality=quality)) is None
