import io
import math
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from hidagent.screen_compare import cursor_overlay_pair, effect_changed, observation_changed

FIXTURES = Path(__file__).parent / "fixtures" / "ios26"
SIZE = (720, 1560)


def test_overlay_pair_tracks_a_cursor_over_icon_detail_and_rejects_edit_elsewhere():
    reference = Image.new("RGB", (500, 1080), (220, 55, 202))
    draw = ImageDraw.Draw(reference)
    draw.rounded_rectangle((240, 578, 328, 663), radius=19, fill=(161, 161, 161))
    for i in range(7):
        draw.ellipse((247 + 4*i, 585 + 4*i, 320 - 4*i, 657 - 4*i), outline=(20, 22, 28), width=2)
    draw.ellipse((368, 705, 392, 729), fill=(130, 46, 122))

    def take(image, x=None, changed=False):
        image = image.copy()
        if x is not None:
            draw = ImageDraw.Draw(image)
            draw.rectangle((367, 704, 393, 730), fill=(220, 55, 202))
            overlay = Image.new("RGBA", image.size)
            ImageDraw.Draw(overlay).ellipse((x-12, 608, x+12, 632), fill=(235, 235, 235, 100))
            image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        if changed:
            ImageDraw.Draw(image).rectangle((347, 587, 374, 621), fill="black")
        out = io.BytesIO()
        image.save(out, "JPEG", quality=84)
        return out.getvalue()

    baseline, outward, returned = take(reference), take(reference, 300), take(reference, 287)

    def match(frame, reference_pointer=(.76, .665)):
        return cursor_overlay_pair(baseline, outward, frame, (.575, .57), -13/500, (.20, .12),
                                   reference_pointer=reference_pointer)

    found = match(returned)
    assert found is not None
    assert found[1] == pytest.approx((287/500, 620/1080), abs=.006)
    inferred = match(returned, reference_pointer=None)
    assert inferred is not None
    assert inferred[1] == pytest.approx(found[1], abs=.006)
    assert match(outward) is None
    assert match(take(reference, 287, changed=True)) is None
    assert match(take(reference, 287, changed=True), reference_pointer=None) is None


@pytest.mark.parametrize("changed", [False, True])
def test_vertical_overlay_pair_at_pen_control(changed):
    width, height = 500, 1080
    x, outward_y, landed_y = 38, 984, 1006
    board = Image.new("RGB", (width, height), (249, 249, 249))
    controls = ImageDraw.Draw(board)
    controls.ellipse((20, 988, 56, 1024), fill=(42, 143, 182))
    controls.line((31, 1013, 45, 999), fill="white", width=4)

    def jpeg(pointer_y=None, *, changed=False):
        frame = board.copy()
        if pointer_y is not None:
            pointer = Image.new("RGBA", frame.size)
            ImageDraw.Draw(pointer).ellipse((x - 12, pointer_y - 12, x + 12, pointer_y + 12),
                                           fill=(110, 110, 110, 120))
            frame = Image.alpha_composite(frame.convert("RGBA"), pointer).convert("RGB")
        if changed:
            ImageDraw.Draw(frame).rectangle((112, 970, 129, 993), fill=(22, 24, 26))
        buffer = io.BytesIO()
        frame.save(buffer, "JPEG", quality=84)
        return buffer.getvalue()

    target = (x / width, landed_y / height)
    found = cursor_overlay_pair(jpeg(), jpeg(outward_y), jpeg(landed_y, changed=changed), target,
                                (landed_y - outward_y) / height, (.2, .12),
                                axis=1, reference_pointer=None)
    if changed:
        assert found is None
    else:
        assert found is not None
        assert found[0] == pytest.approx((x / width, outward_y / height), abs=.006)
        assert found[1] == pytest.approx(target, abs=.006)


def picture(*, pointers=(), clock=False, panel=False, check=False, color=(30, 40, 50)):
    image = Image.new("RGB", SIZE, color)
    draw = ImageDraw.Draw(image)
    for x, y in pointers:
        draw.ellipse((x - 16, y - 16, x + 16, y + 16), fill=(150, 150, 150))
    if clock:
        draw.rectangle((0, 0, SIZE[0], 70), fill="white")
    if panel:
        draw.rectangle((50, 200, 650, 1200), fill=(50, 60, 70))
    if check:
        draw.line(((500, 500), (510, 510), (530, 480)), fill=(255, 149, 0), width=5)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


def test_overlay_pair_preserves_departure_evidence_at_the_status_bar_boundary():
    width, height = SIZE
    original = (70, 89)
    outbound = (150, 138)
    landing = (128, 138)
    baseline = picture(pointers=(original,))
    outward = picture(pointers=(outbound,))

    def match(frame):
        return cursor_overlay_pair(baseline, outward, frame, (landing[0] / width, landing[1] / height),
                                   -22 / width, (.15, .09), reference_pointer=None, report_reference=True)

    evidence = match(picture(pointers=(landing,)))
    assert evidence is not None
    pair, departed = evidence
    assert pair[1] == pytest.approx((landing[0] / width, landing[1] / height), abs=.003)
    assert departed == pytest.approx((original[0] / width, original[1] / height), abs=.003)
    assert match(picture(pointers=(landing,), check=True)) is None


def test_status_clock_does_not_invalidate_observation_or_prove_a_click():
    before, after = picture(), picture(clock=True)
    assert not observation_changed(before, after)
    assert not effect_changed(before, after)


def clock_icon(*, center=(600, 545), second=0.1, minute=0.32, dark_tile=True,
               edit=None, fragmented=False, chroma_halo=False):
    image = Image.new("RGB", SIZE, (186, 103, 56))
    draw = ImageDraw.Draw(image)
    x, y = center
    if dark_tile:
        draw.rounded_rectangle((x - 58, y - 58, x + 58, y + 58), radius=16, fill=(22, 22, 22))
    draw.ellipse((x - 50, y - 50, x + 50, y + 50), fill=(249, 249, 249))

    def end(angle, length):
        return x + math.cos(angle) * length, y + math.sin(angle) * length

    for i in range(12):
        px, py = end(i * math.tau / 12, 43)
        draw.ellipse((px - 2, py - 2, px + 2, py + 2), fill=(24, 24, 24))
    draw.line((center, end(-1.0, 27)), fill=(20, 20, 20), width=6)
    draw.line((center, end(minute, 45)), fill=(20, 20, 20), width=5)
    def draw_second_hand(start, stop):
        draw.line((end(second, start), end(second, stop)), fill=(240, 112, 35), width=4)

    if chroma_halo:
        draw.line((end(second, 27), end(second, 45)), fill=(236, 220, 183), width=22)
    if fragmented:
        draw_second_hand(-6, 17)
        draw_second_hand(25, 46)
    else:
        draw_second_hand(-6, 46)
    if edit == "inside":
        draw.rectangle((x - 32, y + 14, x - 21, y + 23), fill=(25, 25, 25))
    if edit == "nearby":
        draw.rectangle((x - 79, y + 14, x - 69, y + 24), fill=(40, 40, 40))
    if edit == "colored":
        draw.rectangle((x - 22, y - 30, x - 15, y - 23), fill=(225, 20, 70))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=75)
    return buffer.getvalue()


@pytest.mark.parametrize("center", [(600, 545), (180, 1050)])
def test_home_clock_tick_does_not_invalidate_observation_or_prove_a_click(center):
    before = clock_icon(center=center, second=-2.2, minute=.31)
    after = clock_icon(center=center, second=-.9, minute=.33)
    assert not observation_changed(before, after)
    assert not effect_changed(before, after)


@pytest.mark.parametrize("edit", [None, "inside", "nearby", "colored"])
def test_home_clock_with_fragmented_second_hand_keeps_unrelated_edits_visible(edit):
    before = clock_icon(second=-2.2)
    after = clock_icon(second=-.9, fragmented=True, edit=edit)
    assert observation_changed(before, after) is (edit is not None)
    assert effect_changed(before, after) is (edit is not None)


@pytest.mark.parametrize("edit", [None, "inside", "colored"])
def test_clock_tip_chroma_halo_preserves_other_dial_changes(edit):
    before = clock_icon(second=.1)
    after = clock_icon(second=-.81, chroma_halo=True, edit=edit)
    assert observation_changed(before, after) is (edit is not None)
    assert effect_changed(before, after) is (edit is not None)


@pytest.mark.parametrize("edit", ["inside", "nearby", "colored"])
def test_content_edits_remain_visible_when_the_home_clock_ticks(edit):
    before = clock_icon(second=-2.2)
    after = clock_icon(second=-.9, edit=edit)
    assert observation_changed(before, after)
    assert effect_changed(before, after)


def test_moving_analog_icon_or_orange_line_in_non_clock_shape_is_content_change():
    assert observation_changed(clock_icon(second=-2.2), clock_icon(center=(610, 555), second=-.9))
    assert observation_changed(clock_icon(second=-2.2, dark_tile=False), clock_icon(second=-.9, dark_tile=False))


@pytest.mark.parametrize("before,after", [
    ("00_home_1500ms", "01_m150_2000ms"),
    ("01_m150_2000ms", "02_nudge_r0_400ms"),
    ("01_m150_2000ms", "04_m200_2000ms"),
    ("live_tap_landed", "live_tap_corrected"),
])
def test_real_pointer_appearance_nudge_and_long_move_keep_observation(before, after):
    assert not observation_changed((FIXTURES / f"{before}.jpg").read_bytes(),
                                   (FIXTURES / f"{after}.jpg").read_bytes())


def test_real_pointer_move_is_not_click_evidence_when_masked():
    before = (FIXTURES / "01_m150_2000ms.jpg").read_bytes()
    after = (FIXTURES / "04_m200_2000ms.jpg").read_bytes()
    assert not effect_changed(before, after, ignore=((250.9 / 720, 158.8 / 1560),
                                                     (463.5 / 720, 371.3 / 1560)))


def test_delayed_intermediate_pointer_position_is_not_click_evidence():
    before = (FIXTURES / "01_m150_2000ms.jpg").read_bytes()
    after = (FIXTURES / "04_m200_200ms.jpg").read_bytes()
    assert not effect_changed(before, after, ignore=((250.9 / 720, 158.8 / 1560),
                                                     (463.5 / 720, 371.3 / 1560)))


def test_broad_low_contrast_change_still_invalidates_observation():
    before, after = picture(), picture(panel=True)
    assert observation_changed(before, after)
    assert effect_changed(before, after)


def test_small_checkmark_away_from_pointer_is_click_evidence():
    before = picture(pointers=((200, 500),))
    after = picture(pointers=((350, 500),), check=True)
    assert effect_changed(before, after, ignore=((200 / 720, 500 / 1560), (350 / 720, 500 / 1560)))


def test_small_gray_checkmark_is_not_masked_as_a_disc():
    before = picture()
    image = Image.open(io.BytesIO(before)).convert("RGB")
    ImageDraw.Draw(image).line(((500, 500), (510, 510), (530, 480)), fill=(150, 150, 150), width=5)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=80)
    assert effect_changed(before, buffer.getvalue())


def test_known_pointer_masks_apply_before_the_coarse_comparison():
    before = picture(pointers=((200, 500),), color=(0, 0, 0))
    after = picture(pointers=((500, 1000),), clock=True, color=(0, 0, 0))
    assert not effect_changed(before, after, ignore=((200 / 720, 500 / 1560), (500 / 720, 1000 / 1560)))


def test_several_small_changes_are_not_discarded_as_pointer_noise():
    assert observation_changed(picture(), picture(pointers=((200, 500), (400, 800), (600, 1100))))


def test_reencoding_real_keypad_is_not_click_evidence():
    before = (FIXTURES / "keypad_no_disc.jpg").read_bytes()
    with Image.open(io.BytesIO(before)) as image:
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=85)
    assert not effect_changed(before, buffer.getvalue())


def test_large_local_hover_is_inconclusive_but_a_distant_digit_is_evidence():
    before = (FIXTURES / "keypad_no_disc.jpg").read_bytes()
    with Image.open(io.BytesIO(before)) as original:
        image = original.convert("RGB")
    x, y = .5 * image.width, .44 * image.height
    draw = ImageDraw.Draw(image)
    draw.ellipse((x - 33, y - 33, x + 33, y + 33), fill=(150, 150, 150))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    after_hover = buffer.getvalue()
    assert effect_changed(before, after_hover, ignore=((.5, .44),))
    assert not effect_changed(before, after_hover, ignore=((.5, .44),), hover=((.5, .44),))
    draw.rectangle((304, 88, 310, 107), fill="black")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    assert effect_changed(before, buffer.getvalue(), ignore=((.5, .44),), hover=((.5, .44),))


@pytest.mark.parametrize("departing", [False, True])
def test_untracked_keypad_highlight_keeps_observation_and_is_not_click_evidence(departing):
    from test_verified_tap import KEYPAD, keypad_edit

    frames = (KEYPAD, keypad_edit(highlight=True))
    before, after = frames[::-1] if departing else frames
    assert not observation_changed(before, after)
    assert not effect_changed(before, after, hover=((.5, .7),))


@pytest.mark.parametrize("departing", [False, True])
def test_partial_target_mask_does_not_break_unknown_highlight_recognition(departing):
    from test_verified_tap import KEYPAD, keypad_edit

    frames = (KEYPAD, keypad_edit(highlight=True))
    before, after = frames[::-1] if departing else frames
    assert not effect_changed(before, after, hover=((.65, .44),))


@pytest.mark.parametrize("departing", [False, True])
def test_observation_token_survives_untracked_highlight_appearance_and_departure(departing):
    from types import SimpleNamespace

    from test_verified_tap import KEYPAD, keypad_edit

    from hidagent.control import Controls

    frames = (KEYPAD, keypad_edit(highlight=True))
    before, after = frames[::-1] if departing else frames
    screen = SimpleNamespace(live=True, frame=before, updated=time.time())
    control = Controls(None, screen)
    token = control.observation()["screen_id"]
    screen.frame = after
    assert control.consume(token)[1:] == (360, 780)
    with pytest.raises(ValueError, match="consumed"):
        control.consume(token)


@pytest.mark.parametrize("edit", ["solid", "color", "square", "inside", "outside"])
@pytest.mark.parametrize("target", [(.5, .7), (.65, .44)])
def test_highlight_recognition_keeps_content_changes_visible(edit, target):
    from test_verified_tap import KEYPAD, keypad_edit

    image = Image.open(io.BytesIO(keypad_edit(highlight=True) if edit in {"inside", "outside"}
                                  else KEYPAD)).convert("RGB")
    draw = ImageDraw.Draw(image)
    if edit == "solid":
        draw.rounded_rectangle((144, 309, 216, 377), radius=25, fill=(190, 190, 190))
    elif edit == "color":
        draw.rounded_rectangle((144, 309, 216, 377), radius=25, outline=(10, 120, 230), width=3)
    elif edit == "square":
        draw.rectangle((144, 309, 216, 377), outline=(190, 190, 190), width=3)
    elif edit == "inside":
        draw.rectangle((164, 332, 196, 354), fill="black")
    else:
        draw.rectangle((304, 88, 338, 107), fill="black")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    after = buffer.getvalue()
    assert observation_changed(KEYPAD, after)
    assert effect_changed(KEYPAD, after, hover=(target,))


@pytest.mark.parametrize("level", [150, 200])
def test_gray_background_is_not_itself_a_highlight_when_a_colored_outline_appears(level):
    image = Image.new("RGB", (360, 780), (level, level, level))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    before = buffer.getvalue()
    ImageDraw.Draw(image).rounded_rectangle((144, 309, 216, 377), radius=25,
                                            outline=(10, 120, 230), width=3)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    after = buffer.getvalue()
    assert observation_changed(before, after)
    assert effect_changed(before, after)


@pytest.mark.parametrize("compare", [observation_changed, effect_changed])
def test_missing_or_resized_frame_is_changed(compare):
    assert compare(None, picture())
    image = Image.new("RGB", (360, 780), (30, 40, 50))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")
    assert compare(picture(), buffer.getvalue())


@pytest.mark.parametrize("xy", [(300, 180), (300, 1300), (500, 700)])
def test_single_character_change_in_header_composer_or_target_invalidates_token(xy):
    before = picture()
    image = Image.open(io.BytesIO(before))
    x, y = xy
    ImageDraw.Draw(image).rectangle((x, y, x + 14, y + 24), fill="white")
    out = io.BytesIO()
    image.save(out, "PNG")
    assert observation_changed(before, out.getvalue())


def _flat_gray_disc_scene(*, with_disc=True, rim=True, dot_color=(135, 135, 140),
                          textured_bg=False, extra_stroke=False, green_caret=False, two_discs=False):
    image = Image.new("RGB", (500, 1080), (212, 212, 222))
    draw = ImageDraw.Draw(image)
    if textured_bg:
        for y in range(880, 1040, 14):
            draw.rectangle((60, y, 220, y + 5), fill=(40, 40, 45))
    def paste_disc(cx, cy):
        if rim:
            draw.ellipse((cx - 13, cy - 13, cx + 13, cy + 13), fill=(157, 157, 167))
        draw.ellipse((cx - 11, cy - 11, cx + 11, cy + 11), fill=dot_color)
    if with_disc:
        paste_disc(114, 992)
    if two_discs:
        paste_disc(320, 992)
    if extra_stroke:
        draw.rectangle((128, 990, 136, 996), fill=(30, 30, 30))
    if green_caret:
        draw.rectangle((77, 582, 80, 610), fill=(9, 199, 101))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


@pytest.mark.parametrize("disappearing", [True, False])
def test_flat_background_pointer_disc_masked_only_in_observation_changed(monkeypatch, disappearing):
    from hidagent import screen_compare

    before = _flat_gray_disc_scene(with_disc=True)
    after = _flat_gray_disc_scene(with_disc=False)
    if not disappearing:
        before, after = after, before
    # Simulate _discs splitting on antialiased rim so _mask_pointer_discs alone would miss it.
    monkeypatch.setattr(screen_compare, "_discs", lambda *_a, **_kw: [])
    residuals = []
    assert not observation_changed(before, after, residual_out=residuals)
    assert residuals == []
    # effect_changed must NOT mask flat-background pointer discs on its own when _discs fails.
    assert effect_changed(before, after)


def test_flat_background_pointer_disc_rejects_caret_textured_colored_rim_edit_and_multi():
    bg = _flat_gray_disc_scene(with_disc=False)
    for bad in (
        _flat_gray_disc_scene(with_disc=False, green_caret=True),
        _flat_gray_disc_scene(with_disc=True, dot_color=(30, 120, 235)),
        _flat_gray_disc_scene(with_disc=True, extra_stroke=True),
        _flat_gray_disc_scene(with_disc=True, two_discs=True),
    ):
        residuals = []
        assert observation_changed(bad, bg, residual_out=residuals)
        assert residuals and residuals[0]["count"] >= 6

    textured_with_disc = _flat_gray_disc_scene(with_disc=True, textured_bg=True)
    textured_bg = _flat_gray_disc_scene(with_disc=False, textured_bg=True)
    residuals = []
    assert observation_changed(textured_with_disc, textured_bg, residual_out=residuals)
    assert residuals and residuals[0]["count"] >= 6
