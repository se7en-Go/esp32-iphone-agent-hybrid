"""Keyboard suggestion-bar guard (Fix 4 negative case).

The round-3 misfire: a bare tap at (0.5, 0.6), meant for the blank note
area, landed on the keyboard suggestion "xylem" and rewrote the note's last
line from "核对：AbC xYz" to "核对：AbC xylem". A bare tap must never land
on the suggestion bar; suggestion words are tapped via tap_text, and a tap
whose target is body text is already contained by its target_box.

Fixtures are real device frames (500x1080):
- keyboard.jpg: item2-start-notes, Notes with the keyboard up; the
  suggestion bar is the top strip of the keyboard.
- menu_over_keyboard.jpg: oa2-return-15, the dark AssistiveTouch menu over
  the keyboard; the menu must not fake a suggestion bar at y 0.6.
"""
import io
from pathlib import Path

from PIL import Image, ImageDraw

from hidagent.input_context import keyboard_suggestion_bar, point_in_suggestion_bar

FIXTURES = Path(__file__).parent / "fixtures" / "suggestion_bar"


def load(name):
    return (FIXTURES / name).read_bytes()


def no_keyboard_frame():
    """White note with text, no keyboard: the detector must find no bar."""
    image = Image.new("RGB", (500, 1080), "white")
    draw = ImageDraw.Draw(image)
    draw.text((30, 120), "alpha target omega", fill="black")
    draw.text((30, 200), "hello world", fill="black")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=84)
    return out.getvalue()


def test_misfire_point_is_in_the_suggestion_bar():
    frame = load("keyboard.jpg")
    bar = keyboard_suggestion_bar(frame)
    assert bar is not None
    # The round-3 misfire point (0.5, 0.6) falls inside the detected bar.
    assert point_in_suggestion_bar(frame, 0.5, 0.6)
    assert bar[0] <= 0.6 <= bar[1]


def test_body_text_and_keys_are_not_in_the_suggestion_bar():
    frame = load("keyboard.jpg")
    # A body-text word and a keyboard key are outside the bar.
    assert not point_in_suggestion_bar(frame, 0.25, 0.23)
    assert not point_in_suggestion_bar(frame, 0.5, 0.8)
    assert not point_in_suggestion_bar(frame, 0.5, 0.45)


def test_no_keyboard_means_no_bar():
    assert keyboard_suggestion_bar(no_keyboard_frame()) is None
    assert not point_in_suggestion_bar(no_keyboard_frame(), 0.5, 0.6)


def test_menu_over_keyboard_does_not_fake_a_bar_at_the_menu():
    # The dark menu sits where the suggestion bar would be; the detector
    # must not report a bar there, or a bare tap on the menu would be
    # refused as a suggestion-bar tap.
    frame = load("menu_over_keyboard.jpg")
    assert not point_in_suggestion_bar(frame, 0.5, 0.6)


def uniform_grey_frame():
    """The legacy test scene: a uniform light-grey background with a dark
    header and a white field. It is not a keyboard; no bar may be found."""
    image = Image.new("RGB", (360, 780), (225, 225, 225))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 60, 340, 90), fill=(60, 90, 120))
    draw.rectangle((20, 580, 340, 635), fill="white", outline=(120, 120, 120), width=2)
    draw.rectangle((32, 596, 34, 620), fill=(0, 122, 255))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def test_uniform_grey_background_is_not_a_keyboard():
    frame = uniform_grey_frame()
    assert keyboard_suggestion_bar(frame) is None
    assert not point_in_suggestion_bar(frame, 0.5, 0.5)


def test_suggestion_bar_reuses_same_frame_within_tight_tap_budget():
    frame = no_keyboard_frame()
    keyboard_suggestion_bar.cache_clear()
    assert not point_in_suggestion_bar(frame, 0.5, 0.5)
    assert not point_in_suggestion_bar(frame, 0.5, 0.6)
    info = keyboard_suggestion_bar.cache_info()
    assert info.misses == 1 and info.hits == 1
