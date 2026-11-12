"""AssistiveTouch recognition using generated frames and the existing iOS 26 captures."""

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from hidagent.assistive_vision import find_button, find_panel

FIXTURES = Path(__file__).parent / "fixtures" / "ios26"


def encoded(image):
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


def button_frame(at=(.91, .78), size=(360, 780), opacity=1, square=False, background="#dddde4"):
    image = Image.new("RGB", size, background)
    overlay = image.copy()
    draw = ImageDraw.Draw(overlay)
    x, y, radius = at[0] * size[0], at[1] * size[1], size[0] * .07
    if square:
        draw.rounded_rectangle((x-radius, y-radius, x+radius, y+radius), radius=radius*.4, fill="#303030")
        draw.ellipse((x-radius*.68, y-radius*.68, x+radius*.68, y+radius*.68), outline="white",
                     width=max(2, round(radius*.17)))
    else:
        for factor, color in ((1, "#202020"), (.77, "#747474"), (.65, "#b4b4b4"), (.49, "#fafafa")):
            r = radius * factor
            draw.ellipse((x-r, y-r, x+r, y+r), fill=color)
    return encoded(Image.blend(image, overlay, opacity))


def panel_frame(box=(.12, .32, .88, .67), background="#cccce0", icons=True, radius=30):
    image = Image.new("RGB", (360, 780), background)
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = (box[0]*360, box[1]*780, box[2]*360, box[3]*780)
    draw.rounded_rectangle((left, top, right, bottom), radius=radius, fill="#282828")
    if icons:
        for fx, fy in ((.5, .18), (.2, .5), (.8, .5), (.2, .8), (.5, .8), (.8, .8)):
            x, y = left + fx*(right-left), top + fy*(bottom-top)
            draw.ellipse((x-12, y-12, x+12, y+12), outline="white", width=4)
    return encoded(image)


@pytest.mark.parametrize("at, opacity, square", [
    ((.91, .78), 1, False), ((.09, .32), .4, False),
    ((.91, .5), 1, True), ((.09, .6), .4, True),
])
def test_edge_button_variants(at, opacity, square):
    assert find_button(button_frame(at, opacity=opacity, square=square)) == pytest.approx(at, abs=.012)


def test_button_on_dark_background():
    assert find_button(button_frame(background="#121212", opacity=.4)) == pytest.approx((.91, .78), abs=.012)


@pytest.mark.parametrize("name", ["00_home_1500ms", "01_m150_2000ms", "live_tap_corrected"])
def test_real_faded_ios26_button(name):
    frame = (FIXTURES / f"{name}.jpg").read_bytes()
    assert find_button(frame) == pytest.approx((655/720, 1220/1560), abs=.012)


@pytest.mark.parametrize("near", [None, (.1375, .4385)])
def test_airplay_receiver_is_not_the_button_without_a_cached_position(near):
    frame = (FIXTURES / "airplay_receiver_button.jpg").read_bytes()
    assert find_button(frame, near=near) == pytest.approx((655/720, 1220/1560), abs=.012)


def test_filled_pointer_and_centre_ring_are_not_buttons():
    image = Image.new("RGB", (360, 780), "white")
    ImageDraw.Draw(image).ellipse((320, 320, 335, 335), fill="#969696")
    assert find_button(encoded(image)) is None
    assert find_button(button_frame(at=(.5, .5))) is None


def test_missing_button_does_not_turn_last_position_into_detection():
    assert find_button(encoded(Image.new("RGB", (360, 780), "white")), near=(.91, .78)) is None


@pytest.mark.parametrize("box", [(.12, .32, .88, .67), (.08, .43, .92, .81)])
def test_panel_bounds_include_icons(box):
    assert find_panel(panel_frame(box)) == pytest.approx(box, abs=.012)


def test_existing_dark_panel_is_not_a_new_menu():
    frame = panel_frame()
    assert find_panel(frame, before=frame) is None
    assert find_panel(frame, before=encoded(Image.new("RGB", (360, 780), "white"))) is not None


def test_panel_on_a_dark_app_background():
    assert find_panel(panel_frame(background="#080808")) == pytest.approx((.12, .32, .88, .67), abs=.012)


def test_dark_screen_and_button_are_not_menu_panels():
    assert find_panel(encoded(Image.new("RGB", (360, 780), "#282828"))) is None
    assert find_panel(button_frame()) is None
    assert find_panel((FIXTURES / "01_m150_2000ms.jpg").read_bytes()) is None


@pytest.mark.parametrize("rounded", [False, True])
def test_featureless_dark_dialog_is_not_a_menu(rounded):
    image = Image.new("RGB", (360, 780), "#dddde4")
    draw = ImageDraw.Draw(image)
    if rounded:
        draw.rounded_rectangle((50, 245, 310, 505), radius=28, fill="#252525")
    else:
        draw.rectangle((50, 245, 310, 505), fill="#252525")
    assert find_panel(encoded(image)) is None


def test_sharp_corner_panel_with_icons_is_not_a_menu():
    assert find_panel(panel_frame(radius=0)) is None


def test_panel_without_bottom_centre_icon_is_ambiguous():
    image = Image.open(io.BytesIO(panel_frame())).convert("RGB")
    ImageDraw.Draw(image).rectangle((160, 450, 200, 485), fill="#282828")
    assert find_panel(encoded(image)) is None


def test_unrelated_dialog_with_text_and_buttons_is_not_a_menu():
    image = Image.new("RGB", (360, 780), "#dddde4")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((50, 245, 310, 505), radius=28, fill="#252525")
    for y in (285, 305, 325):
        draw.rectangle((90, y, 270, y+3), fill="white")
    for x in (125, 235):
        draw.rounded_rectangle((x-30, 435, x+30, 470), radius=8, outline="white", width=3)
    assert find_panel(encoded(image)) is None


def test_home_requires_grid_dock_and_no_application_chrome():
    from home_scene import draw_home

    from hidagent.assistive_vision import is_home_screen

    for dock, chrome, expected in ((True, False, True), (False, False, False), (True, True, False)):
        image = Image.new("RGB", (360, 780), "#a9bdcf")
        draw_home(image, dock=dock, chrome=chrome)
        assert is_home_screen(encoded(image)) is expected
    assert not is_home_screen(encoded(Image.new("RGB", (360, 780), "#a9bdcf")))
    assert not is_home_screen(panel_frame())


@pytest.mark.parametrize("name", ["00_home_1500ms", "dial_final", "airplay_receiver_button"])
def test_real_apps_are_not_home(name):
    from hidagent.assistive_vision import is_home_screen
    assert not is_home_screen((FIXTURES / f"{name}.jpg").read_bytes())


def test_targets_expose_detected_bounds_and_reject_menu_whitespace():
    from hidagent.assistive_vision import find_button_target, find_icon_box

    center, bounds = find_button_target(button_frame())
    assert bounds[0] < center[0] < bounds[2]
    assert bounds[1] < center[1] < bounds[3]
    assert 0 < bounds[2]-bounds[0] < .14
    frame = panel_frame()
    panel = find_panel(frame)
    left, top, right, bottom = panel
    point = (left + .5*(right-left), top + .8*(bottom-top))
    bounds = find_icon_box(frame, panel, point)
    assert bounds is not None
    assert bounds[0] < point[0] < bounds[2] and bounds[1] < point[1] < bounds[3]
    assert find_icon_box(frame, panel, (.5, (top+bottom)/2)) is None
