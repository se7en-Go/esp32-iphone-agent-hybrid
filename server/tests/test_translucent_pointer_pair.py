"""Real dt2 double-tap sample: faded pointer over bold text, return == reference.

Fixtures are the device sample (dt2 double-tap on "你好", 500x1080):
- outward.jpg: pointer-frame-09, the escaped disc (clean, at (125,185)).
- return.jpg: pointer-frame-11, the pointer settled back at the target
  (126,241), faded over the "你好" glyphs.
- mid_return.jpg: pointer-frame-10, the pointer mid-return at (126,214).

The reference-based cursor_overlay_pair fails here: the return frame matches
the position reference (the pointer came back to where it started), so a
difference against the reference has nothing to pair. The direct
outward/return translucent pair must find it, and the pre-click
observation_changed must still accept the pair (nothing else changed).
"""
from pathlib import Path

from hidagent.screen_compare import observation_changed, translucent_pointer_pair

FIXTURES = Path(__file__).parent / "fixtures" / "dt2_text_pointer"
NEAR = (0.2545, 0.2303)
REACH = (0.153, 0.235)
SHIFT = 0.06336  # return displacement (down), extended probe inverted


def load(name):
    return (FIXTURES / name).read_bytes()


def test_translucent_pair_finds_return_over_text():
    pair, _ = translucent_pointer_pair(load("outward.jpg"), load("return.jpg"),
                                       NEAR, SHIFT, REACH, axis=1)
    assert pair is not None
    (ax, ay), (bx, by) = pair
    # Outward disc at the escaped position, return disc at the target.
    assert abs(ax * 500 - 126) <= 3 and abs(ay * 1080 - 188) <= 3
    assert abs(bx * 500 - 126) <= 3 and abs(by * 1080 - 241) <= 3
    # The return landing is within the paired-miss tolerance of the target.
    assert abs(bx - NEAR[0]) <= 0.12 and abs(by - NEAR[1]) <= 0.12


def test_translucent_pair_return_is_otherwise_unchanged():
    pair, _ = translucent_pointer_pair(load("outward.jpg"), load("return.jpg"),
                                       NEAR, SHIFT, REACH, axis=1)
    assert pair is not None
    assert not observation_changed(load("outward.jpg"), load("return.jpg"),
                                   moved_pointer=pair)


def test_translucent_pair_rejects_no_movement():
    assert translucent_pointer_pair(load("outward.jpg"), load("outward.jpg"),
                                    NEAR, SHIFT, REACH, axis=1) is None


def test_translucent_pair_rejects_reversed_shift():
    assert translucent_pointer_pair(load("outward.jpg"), load("return.jpg"),
                                    NEAR, -SHIFT, REACH, axis=1) is None


def test_translucent_pair_rejects_wrong_axis():
    assert translucent_pointer_pair(load("outward.jpg"), load("return.jpg"),
                                    NEAR, SHIFT, REACH, axis=0) is None


def test_translucent_pair_rejects_mid_return_frame():
    # Frame 10 caught the pointer mid-return at (126,214), off the target
    # word; the pair must not authorize a landing there.
    assert translucent_pointer_pair(load("outward.jpg"), load("mid_return.jpg"),
                                    NEAR, SHIFT, REACH, axis=1) is None
