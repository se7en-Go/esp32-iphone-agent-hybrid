"""AssistiveTouch menu Home hover-highlight as landing evidence.

Fixtures are real device frames (500x1080), menu in the same place:
- highlighted.jpg: item2-after-openapp2, Home icon+label blue (pointer on it).
- highlighted_oa2.jpg: oa2-return-15, Home blue; find_panel fails here
  (the blue Home breaks _menu_icons), but the highlight detector works.
- unhighlighted.jpg: item2-menu-tap3, menu open, Home white (pointer elsewhere).
- no_menu.jpg: item2-after-esc, menu dismissed, no highlight anywhere.

The pointer disc is invisible over the dark menu, so the disc pair cannot
prove a landing there. The Home item turning blue is the evidence instead.
"""
from pathlib import Path

from hidagent.assistive_vision import menu_home_highlighted

FIXTURES = Path(__file__).parent / "fixtures" / "menu_highlight"
# AssistiveTouch menu box (normalized), from the oa2 frame; the menu sits in
# the same place in every fixture frame.
PANEL = (43 / 240, 286 / 518, 233 / 240, 473 / 518)


def load(name):
    return (FIXTURES / name).read_bytes()


def test_highlighted_home_is_detected():
    assert menu_home_highlighted(load("highlighted.jpg"), PANEL)
    assert menu_home_highlighted(load("highlighted_oa2.jpg"), PANEL)


def test_unhighlighted_home_is_not_detected():
    assert not menu_home_highlighted(load("unhighlighted.jpg"), PANEL)


def test_no_menu_is_not_highlighted():
    assert not menu_home_highlighted(load("no_menu.jpg"), PANEL)
