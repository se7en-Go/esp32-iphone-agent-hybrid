"""AssistiveTouch recognition using generated frames and the existing iOS 26 captures."""

import io
import math
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


def panel_frame(box=(.12, .32, .88, .67), background="#cccce0", icons=True, radius=30, home_y=.8):
    image = Image.new("RGB", (360, 780), background)
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = (box[0]*360, box[1]*780, box[2]*360, box[3]*780)
    draw.rounded_rectangle((left, top, right, bottom), radius=radius, fill="#282828")
    if icons:
        for fx, fy in ((.5, .18), (.2, .5), (.8, .5), (.2, .8), (.5, home_y), (.8, .8)):
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


def fragmented_dock_frame(*, dock=False, chrome=False, radius=12, quality=85):
    width, height = 360, 780
    wallpaper = "#2698d8"
    image = Image.new("RGB", (width, height), wallpaper)
    draw = ImageDraw.Draw(image)
    if dock:
        draw.rounded_rectangle((9, 677, 351, 768), radius=16, fill="#d05f55")
        for x in (104, 198, 266):
            draw.rectangle((x, 676, x+7, 685), fill=wallpaper)
    colors = ("#ffdf95", "#53107a", "#fafff9", "#1a2521")
    for y in (281, 370, 460, 723):
        for x, color in zip((54, 139, 221, 306), colors, strict=True):
            draw.rounded_rectangle((x-27, y-27, x+27, y+27), radius=radius, fill=color)
    if chrome:
        draw.line((0, 105, width, 105), fill="white", width=3)

    output = io.BytesIO()
    image.save(output, "JPEG", quality=quality)
    return output.getvalue()


@pytest.mark.parametrize("dock,chrome,expected", [(True, False, True), (False, False, False),
                                                   (True, True, False)])
@pytest.mark.parametrize("quality", [80, 85])
def test_home_recognizes_a_fragmented_dock_over_colorful_wallpaper(dock, chrome, expected, quality):
    from hidagent.assistive_vision import is_home_screen

    assert is_home_screen(fragmented_dock_frame(dock=dock, chrome=chrome, quality=quality)) is expected


@pytest.mark.parametrize("radius", [0, 4, 8])
@pytest.mark.parametrize("quality", [80, 85])
def test_bottom_icon_edges_are_not_a_separate_dock_rim(radius, quality):
    from hidagent.assistive_vision import is_home_screen

    assert not is_home_screen(fragmented_dock_frame(radius=radius, quality=quality))


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


def transparent_list_button(*, at=(.897, .608), overlay=True, glyph=True):
    image = Image.new("RGB", (500, 1080), "#f5f5f8")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((20, 320, 480, 1000), radius=20, fill="white")
    x, y = at[0]*500, at[1]*1080
    if glyph:
        draw.ellipse((x-16, y-6, x+12, y+22), outline="#0088ee", width=3)
        draw.line((x-2, y+4, x-2, y+15), fill="#0088ee", width=3)
        start, end = (x+35, x+55) if x < 250 else (x-55, x-35)
        draw.line((start, y, end, y+6), fill="#222222", width=5)
    if overlay:
        layer = image.copy()
        pen = ImageDraw.Draw(layer)
        for factor, gray in ((1., 85), (.78, 120), (.52, 170)):
            r = 40*factor
            pen.ellipse((x-r, y-r, x+r, y+r), fill=(gray, gray, gray))
        image = Image.blend(image, layer, .4)
    return encoded(image)


@pytest.mark.parametrize("at", [(.897, .608), (.103, .41)])
@pytest.mark.parametrize("glyph", [False, True])
def test_translucent_button_on_light_list_survives_glyph_underlay(at, glyph):
    from hidagent.assistive_vision import find_button_target
    target = find_button_target(transparent_list_button(at=at, glyph=glyph))
    assert target is not None
    assert target[0] == pytest.approx(at, abs=.012)
    assert target[1][0] < target[0][0] < target[1][2]
    assert target[1][1] < target[0][1] < target[1][3]


def test_underlying_info_glyph_and_large_flat_pointer_never_become_button():
    assert find_button(transparent_list_button(overlay=False)) is None
    for outline in (False, True):
        image = Image.new("RGB", (500, 1080), "white")
        ImageDraw.Draw(image).ellipse((408, 615, 488, 695), fill=None if outline else "#bbbbbb",
                                      outline="#bbbbbb", width=10)
        assert find_button(encoded(image)) is None


def test_menu_slot_snaps_only_to_unique_visible_icon_nearby():
    from hidagent.assistive_vision import find_icon_target
    frame = panel_frame(home_y=.74)
    panel = find_panel(frame)
    left, top, right, bottom = panel
    actual = find_icon_target(frame, panel, (left+.5*(right-left), top+.82*(bottom-top)))
    assert actual is not None
    assert actual[0] == pytest.approx((left+.5*(right-left), top+.74*(bottom-top)), abs=.01)
    assert find_icon_target(frame, panel, (.5, (top+bottom)/2)) is None


def test_stationary_button_at_accepts_outlined_button_without_085_radius_mismatch():
    from hidagent.assistive_vision import find_button_target, stationary_button_at

    frame = button_frame()
    target = find_button_target(frame)
    assert target is not None
    assert stationary_button_at(frame, target) is True

    # Colored edit over the button location must still be rejected.
    edited = Image.new("RGB", (360, 780), "white")
    cx, cy = round(target[0][0] * 360), round(target[0][1] * 780)
    ImageDraw.Draw(edited).ellipse((cx - 25, cy - 25, cx + 25, cy + 25), fill=(255, 20, 20))
    assert stationary_button_at(encoded(edited), target) is False


def test_find_button_target_and_find_panel_cache_repeated_frame_bytes():
    from hidagent import assistive_vision

    assistive_vision._find_button_target_cached.cache_clear()
    assistive_vision._find_panel_cached.cache_clear()
    frame = button_frame()
    first = assistive_vision.find_button_target(frame)
    second = assistive_vision.find_button_target(frame)
    assert first == second and assistive_vision._find_button_target_cached.cache_info().hits >= 1
    p1 = assistive_vision.find_panel(frame)
    p2 = assistive_vision.find_panel(frame)
    assert p1 == p2 and assistive_vision._find_panel_cached.cache_info().hits >= 1


def layered_button_scene(center, *, pointer_offset=None, size=(500, 1080)):
    image = Image.new("RGB", size, "#f6f6fa")
    draw = ImageDraw.Draw(image)
    x, y = center
    for radius, gray in ((40, 35), (31, 104), (25, 147), (19, 248)):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(gray,) * 3)
    if pointer_offset is not None:
        px, py = x + pointer_offset[0], y + pointer_offset[1]
        draw.ellipse((px - 12, py - 12, px + 12, py + 12), fill="#808080")
    return encoded(image)


@pytest.mark.parametrize("center", [(448, 490), (52, 250), (448, 800)])
def test_tracked_target_keeps_a_conservative_interior_across_ring_changes(center):
    from hidagent import assistive_vision as vision

    plain = layered_button_scene(center)
    occluded = layered_button_scene(center, pointer_offset=(-8, 39))
    initial = vision.track_button_target(plain)
    hovered = vision.track_button_target(occluded, previous=initial)
    assert initial is not None and hovered is not None
    large_width = initial.box[2] - initial.box[0]
    small_width = hovered.box[2] - hovered.box[0]
    assert large_width > 1.5 * small_width  # Actual outer-ring → bright-core detector switch.
    assert vision.same_button(initial, hovered)
    for target in (initial, hovered):
        x, y = target.point
        left, top, right, bottom = target.safe_box
        assert left < x < right and top < y < bottom
        # The outside edge and its surrounding grey cursor remain outside the safe region.
        assert right - left < small_width
        assert (right - left) * 500 == pytest.approx((bottom - top) * 1080)
    assert hovered.safe_box == pytest.approx(initial.safe_box, abs=.003)


@pytest.mark.parametrize("destination", [(448, 380), (52, 490)])
def test_tracked_target_reacquires_moved_button_and_rejects_absent_button(destination):
    from hidagent import assistive_vision as vision

    first = vision.track_button_target(layered_button_scene((448, 490)))
    moved = vision.track_button_target(layered_button_scene(destination), previous=first)
    assert moved is not None and not vision.same_button(first, moved)
    assert moved.point == pytest.approx((destination[0] / 500, destination[1] / 1080), abs=.003)
    blank = Image.new("RGB", (500, 1080), "#f6f6fa")
    ImageDraw.Draw(blank).ellipse((436, 478, 460, 502), fill="#808080")
    assert vision.track_button_target(encoded(blank), previous=first) is None


def test_tracking_rejects_two_distinct_concentric_buttons_even_with_previous_position():
    from hidagent import assistive_vision as vision

    left = layered_button_scene((52, 490))
    right = layered_button_scene((448, 490))
    with Image.open(io.BytesIO(left)) as left_image, Image.open(io.BytesIO(right)) as right_image:
        left_image.paste(right_image.crop((250, 0, 500, 1080)), (250, 0))
        both = encoded(left_image)
    assert vision.find_button_target(both) is not None  # Legacy API can rank candidates.
    for previous in (None, vision.track_button_target(left), vision.track_button_target(right)):
        assert vision.track_button_target(both, previous) is None


def test_same_button_rejects_a_radically_smaller_ring_at_unchanged_center():
    from hidagent import assistive_vision as vision

    first = vision.track_button_target(layered_button_scene((448, 490)))
    x, y = first.point
    w, h = first.size
    small_box = x - 9 / w, y - 9 / h, x + 9 / w, y + 9 / h
    assert not vision.same_button(first, first._replace(box=small_box))


def test_parked_cursor_requires_fresh_visible_rings_to_reuse_previous_location():
    from hidagent import assistive_vision as vision

    frame = transparent_list_button(glyph=False)
    first = vision.track_button_target(frame)
    x, y = (coord * length for coord, length in zip(first.point, first.size, strict=True))
    with Image.open(io.BytesIO(frame)) as image:
        draw = ImageDraw.Draw(image)
        draw.ellipse((x - 14, y - 6, x + 14, y + 22), fill="#808080")
        partial = encoded(image)
        draw.ellipse((x - 45, y - 45, x + 45, y + 45), fill="#808080")
        full = encoded(image)
    assert vision.track_button_target(partial) is None
    assert vision.track_button_target(partial, previous=first) == first
    assert vision.track_button_target(full, previous=first) is None


@pytest.mark.parametrize("center", [(448, 490), (52, 490)])
def test_measuring_escape_clears_outer_ring_and_cursor_even_when_only_core_is_visible(tmp_path, center):
    from test_pointer_session import setup_phone

    from hidagent import assistive_vision as vision

    phone, _, mirror = setup_phone(tmp_path)
    phone.span = {"w": 270, "h": 584}
    plain = layered_button_scene(center)
    obscured = layered_button_scene(center, pointer_offset=(-8, 39))
    first = vision.track_button_target(plain)
    hovered = vision.track_button_target(obscured, previous=first)
    no_history = vision.track_button_target(obscured)
    assert hovered.probe_radius_px == pytest.approx(first.probe_radius_px, abs=2)
    for frame, target in ((plain, first), (obscured, hovered), (obscured, no_history)):
        mirror.frame = frame
        axis, units = phone._target_escape_probe(target.point, target.probe_box)
        destination = [v * n for v, n in zip(target.point, target.size, strict=True)]
        offset_px = units * target.size[axis] / phone.span["wh"[axis]]
        destination[axis] += offset_px
        assert abs(offset_px) <= .18 * 500
        # Clear the painted 40px outer ring by the probe's full 27.5px clearance.
        assert math.dist(destination, center) >= 40 + .055 * 500
        assert target.probe_box[0] < target.safe_box[0] < target.safe_box[2] < target.probe_box[2]


def test_escape_envelope_is_not_inherited_across_a_move_or_resized_frame():
    from hidagent import assistive_vision as vision

    previous = vision.track_button_target(layered_button_scene((52, 490)))
    for center, size in (((448, 490), (500, 1080)), ((52, 490), (500, 1100))):
        frame = layered_button_scene(center, pointer_offset=(-8, 39), size=size)
        fresh = vision.track_button_target(frame)
        assert vision.track_button_target(frame, previous=previous) == fresh
