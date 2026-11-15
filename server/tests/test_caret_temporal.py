"""Preedit text/underline is content, while a separately proven caret may blink."""

import io
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from hidagent.caret import temporal_focused_region
from hidagent.screen_compare import observation_changed


def search_frame(level=0, *, text="a", edit=None, bounded=True, caret_x=77, size=(500, 1080)):
    # Synthetic Settings search geometry: a near-gray field and a stable blue
    # preedit underline connected to the on-phase insertion caret.
    image = Image.new("RGB", (500, 1080), (208, 207, 213))
    draw = ImageDraw.Draw(image)
    if bounded:
        draw.rounded_rectangle((21, 68, 418, 119), radius=14, fill=(197, 196, 202))
    else:
        draw.rectangle((0, 0, 499, 1079), fill=(197, 196, 202))
    draw.text((64, 89), text, fill=(25, 25, 28))
    draw.line((64, 105, 77, 105), fill=(10, 113, 240), width=2)
    color = tuple(round(a + level * (b - a))
                  for a, b in zip((197, 196, 202), (20, 113, 242), strict=True))
    draw.rectangle((caret_x, 80, caret_x + 1, 104), fill=color)
    if edit == "short":
        draw.rectangle((77, 80, 78, 104), fill=(197, 196, 202))
        draw.line((77, 88, 77, 98), fill=(20, 113, 242), width=2)
    elif edit == "dot":
        draw.rectangle((77, 80, 78, 104), fill=(197, 196, 202))
        draw.rectangle((76, 98, 79, 101), fill=(20, 113, 242))
    elif edit == "underline_removed":
        draw.rectangle((64, 105, 76, 106), fill=(197, 196, 202))
    elif edit == "underline_added":
        draw.line((80, 105, 96, 105), fill=(10, 113, 240), width=2)
    elif edit == "near_text":
        draw.line((73, 88, 73, 99), fill=(25, 25, 28), width=2)
    elif edit == "outside":
        draw.rectangle((100, 250, 120, 260), fill=(10, 30, 50))
    if size != image.size:
        image = image.resize(size)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def frames(levels=(1, 0, 1, 0), **kwargs):
    return tuple(SimpleNamespace(frame=search_frame(level, **kwargs), arrived=i * .4, generation=1)
                 for i, level in enumerate(levels))


@pytest.mark.parametrize("levels", [(1, 0), (0, 1), (.25, .7)])
def test_bounded_preedit_caret_uses_complete_periodic_templates(levels):
    before, after = (search_frame(level) for level in levels)
    assert observation_changed(before, after)
    assert not observation_changed(before, after, caret_frames=frames())


@pytest.mark.parametrize("edit", ["short", "dot", "underline_removed", "underline_added", "near_text", "outside"])
@pytest.mark.parametrize("reverse", [False, True])
def test_periodic_preedit_caret_never_hides_edits(edit, reverse):
    before, after = search_frame(1), search_frame(0, edit=edit)
    if reverse:
        before, after = after, before
    assert observation_changed(search_frame(0), search_frame(0, edit=edit))
    assert observation_changed(before, after, caret_frames=frames())


@pytest.mark.parametrize("kind", ["one_transition", "no_full_off", "text", "size", "generation",
                                  "unbounded", "moved", "old", "unordered"])
def test_unrelated_or_incomplete_preedit_history_is_not_permission(kind):
    before, after = search_frame(.25), search_frame(.7)
    evidence = frames()
    if kind == "one_transition":
        evidence = frames((1, 1, 0, 0))
    elif kind == "no_full_off":
        evidence = frames((1, .4, 1, .4))
    elif kind == "text":
        evidence = frames(text="b")
    elif kind == "size":
        evidence = frames(size=(510, 1080))
    elif kind == "generation":
        evidence = tuple(SimpleNamespace(**{**vars(f), "generation": i % 2}) for i, f in enumerate(evidence))
    elif kind == "unbounded":
        before, after = search_frame(.25, bounded=False), search_frame(.7, bounded=False)
        evidence = frames(bounded=False)
    elif kind == "moved":
        after = search_frame(.7, caret_x=81)
    elif kind == "old":
        evidence = tuple(SimpleNamespace(**{**vars(f), "arrived": i * 5}) for i, f in enumerate(evidence))
    elif kind == "unordered":
        evidence = evidence[::-1]
    assert observation_changed(before, after, caret_frames=evidence)


@pytest.mark.parametrize("xy,color", [((75, 105), (25, 25, 28)), ((77, 105), (25, 25, 28)),
                                      ((78, 105), (10, 113, 240)), ((80, 104), (10, 113, 240)),
                                      ((76, 102), (25, 25, 28)), ((80, 105), (0, 0, 0))])
@pytest.mark.parametrize("learned", [False, True])
def test_caret_foot_edits_cannot_be_ignored_or_learned_as_blink(xy, color, learned):
    image = Image.open(io.BytesIO(search_frame(0))).convert("RGB")
    x, y = xy
    ImageDraw.Draw(image).rectangle((x, y, x + 1, y + 2), fill=color)
    data = io.BytesIO()
    image.save(data, "PNG")
    changed = data.getvalue()
    assert observation_changed(search_frame(0), changed)
    evidence = frames()
    if learned:
        evidence = tuple(SimpleNamespace(frame=frame, arrived=i * .4, generation=1)
                         for i, frame in enumerate((search_frame(1), changed, search_frame(1), changed)))
    assert observation_changed(search_frame(1), changed, caret_frames=evidence)
    assert observation_changed(changed, search_frame(1), caret_frames=evidence)


def test_history_cannot_bridge_a_field_edit_or_a_caret_move():
    for middle in (search_frame(0, text="b"), search_frame(0, edit="underline_added"), search_frame(1, caret_x=81)):
        evidence = tuple(SimpleNamespace(frame=frame, arrived=i * .4, generation=1)
                         for i, frame in enumerate((search_frame(1), middle, search_frame(1))))
        assert observation_changed(search_frame(1), search_frame(0), caret_frames=evidence)


@pytest.mark.parametrize("level", [0, .25, 1])
def test_temporal_focus_exposes_only_the_proved_current_field(level):
    region = temporal_focused_region(search_frame(level), frames())
    assert region is not None
    assert .03 < region[0] < .05 and .06 < region[1] < .07
    assert .82 < region[2] < .85 and .10 < region[3] < .12
    assert temporal_focused_region(search_frame(level, text="b"), frames()) is None
    assert temporal_focused_region(search_frame(level), frames((0, 1))) is None


def test_temporal_focus_does_not_borrow_white_caret_permission():
    from test_caret_blink import history, spotlight_frame

    assert temporal_focused_region(spotlight_frame(1), history(0, 1, 0, 1)) is None
