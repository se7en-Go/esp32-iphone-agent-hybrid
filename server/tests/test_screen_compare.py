import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from hidagent.screen_compare import effect_changed, observation_changed

FIXTURES = Path(__file__).parent / "fixtures" / "ios26"
SIZE = (720, 1560)


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


def test_status_clock_does_not_invalidate_observation_or_prove_a_click():
    before, after = picture(), picture(clock=True)
    assert not observation_changed(before, after)
    assert not effect_changed(before, after)


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
    screen = SimpleNamespace(live=True, frame=before, updated=1)
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
