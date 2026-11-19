"""WeChat 文件传输助手 on the native Pinyin keyboard, from real device frames (500x1080).

Fixtures (tests/fixtures/wechat_paste, avatar redacted):
- empty_pinyin / empty_pinyin_caret_on: the empty chat field, green caret mid-fade / fully on.
- permission_sheet: "“微信”想从“快捷指令”粘贴" after the single Cmd+V.
- pasted_three_lines: the field grown to three lines after Allow Paste; the
  Pinyin suggestion strip, the send key tint and the expand control changed.
- blink_*: one live blink sequence of the green caret in the empty field.
ocr.json holds the recorded RapidOCR output for the paste frames, so this file
needs no OCR backend.

The two 409s these reproduce: the green caret blink failed the pre-paste
context check, and the field growth plus suggestion strip/send key changes
failed confirmation because no suggestion strip was located on WeChat's grey page.
"""

import asyncio
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from hidagent import ocr, screen_compare
from hidagent.input_context import InputContextGuard, keyboard_suggestion_bar
from hidagent.paste_confirmation import verify_after_paste_permission

FIXTURES = Path(__file__).parent / "fixtures" / "wechat_paste"
FIELD = (.132, .525, .768, .576)  # the observed empty input field (host target_box)
TEXT = "2026年11月18日深圳\n小雨 最低15度最高24度\n来源：中国天气网"
OCR = json.loads((FIXTURES / "ocr.json").read_text(encoding="utf-8"))


def load(name):
    return (FIXTURES / f"{name}.jpg").read_bytes()


def edited(name, draw):
    image = Image.open(io.BytesIO(load(name))).convert("RGB")
    draw(ImageDraw.Draw(image))
    output = io.BytesIO()
    image.save(output, "JPEG", quality=92)
    return output.getvalue()


def boxes(name):
    return [ocr.TextBox(item["text"], tuple(item["box"]), tuple(item["center"]), item["conf"])
            for item in OCR[name]]


def blink_history():
    names = ("blink_on_1", "blink_off_1", "blink_on_2", "blink_off_2", "blink_on_4")
    return tuple(SimpleNamespace(frame=load(name), arrived=i * .55, generation=1) for i, name in enumerate(names))


# --- pre-paste: green caret blink -------------------------------------------------

def test_green_caret_blink_is_masked_only_inside_the_verified_field():
    before, after = load("blink_mid"), load("blink_on_3")
    history = blink_history()
    assert screen_compare.observation_changed(before, after, caret_frames=history)  # the old 409
    assert not screen_compare.observation_changed(before, after, caret_frames=history, caret_scope=FIELD)
    assert screen_compare.observation_changed(before, after, caret_frames=history,
                                              caret_scope=(.132, .20, .768, .30))


@pytest.mark.parametrize("edit", [
    lambda d: d.line((86, 584, 86, 604), fill=(30, 30, 30), width=2),        # glyph stroke beside the caret
    lambda d: d.rectangle((100, 585, 160, 605), fill=(30, 30, 30)),          # text typed into the field
    lambda d: d.rectangle((84, 586, 88, 604), fill=(9, 199, 101)),           # a second green line
    lambda d: d.rectangle((180, 70, 320, 95), fill=(200, 200, 200)),         # conversation title changed
    lambda d: d.rounded_rectangle((140, 330, 400, 400), radius=12, fill=(149, 236, 105)),  # new bubble
])
def test_green_caret_evidence_never_hides_other_changes(edit):
    before = load("blink_mid")
    after = edited("blink_on_3", edit)
    assert screen_compare.observation_changed(before, after, caret_frames=blink_history(), caret_scope=FIELD)


def test_green_caret_needs_a_complete_recent_blink():
    before, after = load("blink_mid"), load("blink_on_3")
    one_way = tuple(SimpleNamespace(frame=load(n), arrived=i * .55, generation=1)
                    for i, n in enumerate(("blink_on_1", "blink_on_2", "blink_off_1")))
    assert screen_compare.observation_changed(before, after, caret_frames=one_way, caret_scope=FIELD)


def test_green_caret_masks_immediately_after_permission_sheet_dismissal():
    # For 4 s after Allow Paste closes, recent_frames still begins with the
    # dimmed permission sheet; those frames must not poison caret discovery.
    before, after = load("blink_mid"), load("blink_on_3")
    names = ("permission_sheet", "permission_sheet", "blink_on_1", "blink_off_1", "blink_on_2")
    history = tuple(SimpleNamespace(frame=load(n), arrived=i * .45, generation=1) for i, n in enumerate(names))
    assert not screen_compare.observation_changed(before, after, caret_frames=history, caret_scope=FIELD)
    for tamper in (
        lambda d: d.rectangle((77, 592, 79, 596), fill=(30, 30, 30)),    # black dot inside caret column
        lambda d: d.rectangle((77, 590, 79, 596), fill=(9, 199, 101)),   # short green tick inside caret column
        lambda d: d.rectangle((77, 581, 79, 608), fill=(0, 120, 255)),   # blue line in caret column
    ):
        assert screen_compare.observation_changed(
            load("blink_off_1"), edited("blink_off_1", tamper), caret_frames=history, caret_scope=FIELD)


def test_pre_paste_context_guard_accepts_only_the_blink_in_its_field():
    now = time.time()
    history = tuple(SimpleNamespace(**vars(item), updated=now - 3 + item.arrived) for item in blink_history())
    screen = SimpleNamespace(frame=load("blink_on_3"), live=True, updated=now, generation=1,
                             recent_frames=history)
    original = SimpleNamespace(frame=load("blink_mid"), generation=1, updated=now)
    guard = InputContextGuard(screen, original)
    guard.region = FIELD
    guard()  # the paste-time context check passes while the caret blinks
    screen.frame = edited("blink_on_3", lambda d: d.rectangle((180, 70, 320, 95), fill=(200, 200, 200)))
    with pytest.raises(RuntimeError, match="screen changed"):
        guard()


# --- suggestion strip on WeChat's grey page -----------------------------------------

def test_wechat_pinyin_suggestion_strip_is_located_consistently():
    bars = {name: keyboard_suggestion_bar(load(name))
            for name in ("empty_pinyin", "empty_pinyin_caret_on", "pasted_three_lines", "blink_on_1")}
    assert len(set(bars.values())) == 1
    top, bottom = bars["empty_pinyin"]
    assert FIELD[3] < top < .60 and .61 < bottom < .66  # strip of 我你有好… at y~0.62


def test_notes_strip_is_unchanged():
    notes = (Path(__file__).parent / "fixtures" / "suggestion_bar" / "keyboard.jpg").read_bytes()
    assert keyboard_suggestion_bar(notes) == pytest.approx((0.5791505791505791, 0.6391505791505792))


# --- confirmation after Allow Paste -----------------------------------------------

def verify(monkeypatch, *, before="empty_pinyin", sheet="permission_sheet", after="pasted_three_lines",
           after_frame=None, after_boxes=None, text=TEXT, region=FIELD):
    frames = {load(before): boxes(before), load(sheet): boxes(sheet)}
    current = after_frame or load(after)
    frames[current] = after_boxes if after_boxes is not None else boxes(after)

    async def read(image):
        return frames[image]

    monkeypatch.setattr(ocr, "read_text_async", read)
    return asyncio.run(verify_after_paste_permission(load(before), load(sheet), current, region, text))


def test_three_line_paste_after_allow_paste_confirms(monkeypatch):
    assert verify(monkeypatch) is True
    assert verify(monkeypatch, before="empty_pinyin_caret_on") is True


LIVE_TEXT = "微信粘贴验收 00731\n第二行 AbC xYz\n第三行 中文与换行保持原样"


def test_live_acceptance_frames_confirm(monkeypatch):
    # Recorded during the real run: a wide third line, the pointer parked over
    # the text where Allow Paste was, and the input bar grown upward.
    live = {"before": "live_original", "sheet": "live_sheet", "after": "live_pasted"}
    assert verify(monkeypatch, **live, text=LIVE_TEXT) is True
    assert verify(monkeypatch, **live, text=LIVE_TEXT.replace("xYz", "xyz")) is False
    content_in_band = edited("live_pasted", lambda d: d.rectangle((400, 525, 480, 560), fill=(149, 236, 105)))
    assert verify(monkeypatch, **live, text=LIVE_TEXT, after_frame=content_in_band,
                  after_boxes=boxes("live_pasted")) is False


def test_strip_is_the_keyboard_not_a_wide_text_line():
    assert keyboard_suggestion_bar(load("live_pasted")) == keyboard_suggestion_bar(load("live_original"))


def _moved(items, dy):
    return [ocr.TextBox(i.text, (i.box[0], i.box[1] + dy, i.box[2], i.box[3] + dy),
                        (i.center[0], i.center[1] + dy), i.conf) for i in items]


@pytest.mark.parametrize("case", ["wrong_text", "duplicate_paste", "other_conversation", "no_sheet",
                                  "text_elsewhere", "new_bubble", "keyboard_key", "voice_button",
                                  "region_over_strip"])
def test_confirmation_rejects_everything_but_the_expected_change(monkeypatch, case):
    after = boxes("pasted_three_lines")
    lines = [i for i in after if FIELD[0] < i.center[0] < FIELD[2] and .48 < i.center[1] < .57]
    kwargs = {}
    if case == "wrong_text":
        kwargs["text"] = TEXT.replace("24", "25")
    elif case == "duplicate_paste":
        kwargs["after_boxes"] = [i for i in after if i not in lines] + _moved(lines, -.075) + lines
    elif case == "other_conversation":
        kwargs["after_boxes"] = [ocr.TextBox("群聊", i.box, i.center, i.conf) if i.text == "文件传输助手" else i
                                 for i in after]
    elif case == "no_sheet":
        kwargs["sheet"] = "empty_pinyin_caret_on"
    elif case == "text_elsewhere":
        kwargs["after_boxes"] = [i for i in after if i not in lines] + _moved(lines, -.30)
    elif case == "new_bubble":
        kwargs["after_frame"] = edited("pasted_three_lines",
                                       lambda d: d.rounded_rectangle((140, 220, 400, 300), radius=12,
                                                                     fill=(149, 236, 105)))
    elif case == "keyboard_key":
        kwargs["after_frame"] = edited("pasted_three_lines",
                                       lambda d: d.rectangle((220, 720, 260, 765), fill=(18, 132, 255)))
    elif case == "voice_button":
        kwargs["after_frame"] = edited("pasted_three_lines",
                                       lambda d: d.ellipse((15, 580, 48, 613), fill=(60, 60, 60)))
    elif case == "region_over_strip":
        kwargs["region"] = (.132, .525, .768, .62)
    assert verify(monkeypatch, **kwargs) is False


def test_codec_quantization_drift_confirms_while_broad_dim_still_fails(monkeypatch):
    def adjust_outside(delta_fn):
        image = Image.open(io.BytesIO(load("live_pasted"))).convert("RGB")
        w, h = image.size
        pixels = image.load()
        for y in range(h):
            if int(.45 * h) <= y <= int(.66 * h):
                continue
            for x in range(w):
                d = delta_fn(x, y, h)
                if d:
                    r, g, b = pixels[x, y]
                    pixels[x, y] = (min(255, max(0, r + d)), min(255, max(0, g + d)), min(255, max(0, b + d)))
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue()

    jittered = adjust_outside(lambda x, y, _h: 3 if ((x // 3) + (y // 3)) % 2 == 0 else -3)
    dimmed = adjust_outside(lambda _x, y, h: -8 if int(.10 * h) <= y <= int(.46 * h) else 0)
    assert verify(monkeypatch, before="live_original", sheet="live_sheet",
                  after_frame=jittered, after_boxes=boxes("live_pasted"), text=LIVE_TEXT) is True
    assert verify(monkeypatch, before="live_original", sheet="live_sheet",
                  after_frame=dimmed, after_boxes=boxes("live_pasted"), text=LIVE_TEXT) is False


# --- pre-paste: stationary AssistiveTouch idle-to-opaque shift --------------------

def test_stationary_assistive_bubble_opacity_requires_opt_in_and_matches_real_frames():
    dim, bright = load("assistive_dim"), load("assistive_bright")
    assert screen_compare.observation_changed(dim, bright, caret_scope=FIELD)
    assert not screen_compare.observation_changed(
        dim, bright, caret_scope=FIELD, allow_assistive_opacity=True)
    assert not screen_compare.observation_changed(
        bright, dim, caret_scope=FIELD, allow_assistive_opacity=True)

    now = time.time()
    screen = SimpleNamespace(frame=bright, live=True, updated=now, generation=1, recent_frames=())
    original = SimpleNamespace(frame=dim, generation=1, updated=now)
    guard = InputContextGuard(screen, original, allow_assistive_opacity=True)
    guard.region = FIELD
    guard()

    # Never mask when the AssistiveTouch bubble overlaps the verified input field.
    overlapping = InputContextGuard(screen, original, allow_assistive_opacity=True)
    overlapping.region = (.80, .30, .98, .40)
    with pytest.raises(RuntimeError, match="screen changed"):
        overlapping()


@pytest.mark.parametrize("tamper", ["title", "field_text", "new_bubble", "bubble_moved",
                                    "bubble_appeared", "menu_opened"])
def test_stationary_assistive_opacity_never_hides_content_or_moved_bubble(monkeypatch, tamper):
    from hidagent import assistive_vision

    dim, bright = load("assistive_dim"), load("assistive_bright")
    before, after = dim, bright
    if tamper == "title":
        after = edited("assistive_bright", lambda d: d.rectangle((180, 70, 320, 95), fill=(200, 200, 200)))
    elif tamper == "field_text":
        after = edited("assistive_bright", lambda d: d.rectangle((100, 585, 180, 605), fill=(30, 30, 30)))
    elif tamper == "new_bubble":
        after = edited("assistive_bright",
                       lambda d: d.rounded_rectangle((140, 200, 380, 270), radius=12, fill=(149, 236, 105)))
    elif tamper == "bubble_moved":
        image = Image.open(io.BytesIO(bright)).convert("RGB")
        bubble = image.crop((405, 330, 490, 420))
        ImageDraw.Draw(image).rectangle((405, 330, 490, 420), fill=(237, 237, 237))
        image.paste(bubble, (405, 450))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=92)
        after = out.getvalue()
    elif tamper == "bubble_appeared":
        before = edited("assistive_dim", lambda d: d.rectangle((405, 330, 490, 420), fill=(237, 237, 237)))
    elif tamper == "menu_opened":
        monkeypatch.setattr(assistive_vision, "find_panel",
                            lambda frame: (.15, .25, .85, .75) if frame == bright else None)
    assert screen_compare.observation_changed(
        before, after, caret_scope=FIELD, allow_assistive_opacity=True)


def test_pointer_occluded_dim_assistive_bubble_passes_opt_in_pre_paste_check():
    from hidagent import assistive_vision

    dim, bright = load("assistive_dim_occluded"), load("assistive_bright_occluded")
    assert assistive_vision.find_button_target(dim) is None
    assert assistive_vision.find_button_target(bright) is not None
    assert screen_compare.observation_changed(dim, bright, caret_scope=FIELD)
    assert not screen_compare.observation_changed(
        dim, bright, caret_scope=FIELD, allow_assistive_opacity=True)
    assert not screen_compare.observation_changed(
        bright, dim, caret_scope=FIELD, allow_assistive_opacity=True)

    now = time.time()
    screen = SimpleNamespace(frame=bright, live=True, updated=now, generation=1, recent_frames=())
    original = SimpleNamespace(frame=dim, generation=1, updated=now)
    guard = InputContextGuard(screen, original, allow_assistive_opacity=True)
    guard.region = FIELD
    guard()

    overlapping = InputContextGuard(screen, original, allow_assistive_opacity=True)
    overlapping.region = (.80, .30, .98, .40)
    with pytest.raises(RuntimeError, match="screen changed"):
        overlapping()


@pytest.mark.parametrize("tamper", ["title", "field_text", "new_bubble", "bubble_erased",
                                    "bubble_shifted", "colored_core_smudge", "menu_opened"])
def test_pointer_occluded_assistive_fallback_still_rejects_tamper(monkeypatch, tamper):
    from hidagent import assistive_vision

    dim, bright = load("assistive_dim_occluded"), load("assistive_bright_occluded")
    before, after = dim, bright
    if tamper == "title":
        after = edited("assistive_bright_occluded", lambda d: d.rectangle((180, 70, 320, 95), fill=(200, 200, 200)))
    elif tamper == "field_text":
        after = edited("assistive_bright_occluded", lambda d: d.rectangle((100, 585, 180, 605), fill=(30, 30, 30)))
    elif tamper == "new_bubble":
        after = edited("assistive_bright_occluded",
                       lambda d: d.rounded_rectangle((140, 200, 380, 270), radius=12, fill=(149, 236, 105)))
    elif tamper == "bubble_erased":
        before = edited("assistive_dim_occluded", lambda d: d.rectangle((400, 320, 495, 425), fill=(238, 237, 239)))
    elif tamper == "bubble_shifted":
        image = Image.open(io.BytesIO(dim)).convert("RGB")
        patch = image.crop((405, 330, 490, 420))
        ImageDraw.Draw(image).rectangle((405, 330, 490, 420), fill=(238, 237, 239))
        image.paste(patch, (405, 352))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=92)
        before = out.getvalue()
    elif tamper == "colored_core_smudge":
        before = edited("assistive_dim_occluded", lambda d: d.ellipse((440, 365, 470, 395), fill=(220, 40, 40)))
    elif tamper == "menu_opened":
        monkeypatch.setattr(assistive_vision, "find_panel",
                            lambda frame: (.15, .25, .85, .75) if frame == bright else None)
    assert screen_compare.observation_changed(
        before, after, caret_scope=FIELD, allow_assistive_opacity=True)


WRAPPED_TEXT = "Pi 第一期底座验收\n第二行 AbC 123\n第三行 粘贴回执与对照帧正常"
WRAPPED_FIELD = (.13627, .53105, .75752, .57275)


def test_soft_wrapped_three_line_paste_after_allow_paste_confirms(monkeypatch):
    from hidagent import paste_confirmation

    wrapped = {
        "before": "wrapped_original",
        "sheet": "wrapped_sheet",
        "after": "wrapped_pasted",
        "region": WRAPPED_FIELD,
    }
    assert verify(monkeypatch, **wrapped, text=WRAPPED_TEXT) is True
    assert verify(monkeypatch, **wrapped, text=WRAPPED_TEXT[:-1]) is False
    assert verify(monkeypatch, **wrapped, text=WRAPPED_TEXT.replace("常", "崇")) is False
    assert verify(monkeypatch, **wrapped, text=WRAPPED_TEXT.replace("AbC", "abc")) is False

    after = boxes("wrapped_pasted")
    first_two = [item for item in after if item.text in ("Pi第一期底座验收", "第二行AbC123")]
    assert len(first_two) == 2
    merged_box = ocr.TextBox(
        "Pi第一期底座验收第二行AbC123",
        (.158, .466, .72, .489),
        (.439, .477),
        .99,
    )
    merged_boxes = [item for item in after if item not in first_two] + [merged_box]
    paste_confirmation._PAIR_CACHE = None
    assert verify(monkeypatch, **wrapped, text=WRAPPED_TEXT, after_boxes=merged_boxes) is False

    dup_row = ocr.TextBox("常", (.158, .566, .206, .571), (.182, .5685), .99)
    dup_boxes = after + [dup_row]
    paste_confirmation._PAIR_CACHE = None
    assert verify(monkeypatch, **wrapped, text=WRAPPED_TEXT, after_boxes=dup_boxes) is False

    tampered_title = edited("wrapped_pasted", lambda d: d.rectangle((180, 70, 320, 95), fill=(200, 200, 200)))
    assert verify(monkeypatch, **wrapped, text=WRAPPED_TEXT,
                  after_frame=tampered_title, after_boxes=after) is False


def test_stationary_assistive_button_occluding_ocr_anchor_confirms(monkeypatch):
    from hidagent import assistive_vision, paste_confirmation

    before_img = load("wrapped_original")
    after_img = load("wrapped_pasted")
    assistive_box = screen_compare.stationary_assistive_button_box(before_img, after_img, exclude=(WRAPPED_FIELD,))
    assert assistive_box is not None

    inside_button = ocr.TextBox("良策", (.850, .348, .960, .368), (.905, .358), .95)
    base_before = boxes("wrapped_original")
    base_after = boxes("wrapped_pasted")

    async def run(before_ocr, after_ocr, after_bytes=after_img):
        paste_confirmation._PAIR_CACHE = None
        frames = {before_img: before_ocr, load("wrapped_sheet"): boxes("wrapped_sheet"), after_bytes: after_ocr}

        async def read(image):
            return frames[image]

        with monkeypatch.context() as mp:
            mp.setattr(ocr, "read_text_async", read)
            return await verify_after_paste_permission(before_img, load("wrapped_sheet"), after_bytes,
                                                       WRAPPED_FIELD, WRAPPED_TEXT)

    # 1) Dim -> bright occlusion: anchor inside the stationary AssistiveTouch button only in before.
    assert asyncio.run(run(base_before + [inside_button], base_after)) is True
    # 2) Bright -> dim reveal: anchor inside stationary button only in after.
    assert asyncio.run(run(base_before, base_after + [inside_button])) is True

    # 3) Wide anchor touching stationary button: allowed if outside-button pixels are intact,
    #    rejected immediately if pixels on the non-button left portion change.
    wide_before = ocr.TextBox("通知预览良策", (.620, .348, .940, .368), (.780, .358), .95)
    wide_after = ocr.TextBox("通知预览", (.620, .348, .820, .368), (.720, .358), .95)
    assert asyncio.run(run(base_before + [wide_before], base_after + [wide_after])) is True
    tampered_left = edited("wrapped_pasted", lambda d: d.rectangle((310, 376, 370, 396), fill=(180, 40, 40)))
    assert asyncio.run(run(base_before + [wide_before], base_after + [wide_after], after_bytes=tampered_left)) is False

    # 4) Negative: missing/extra anchor outside AssistiveTouch button must still fail.
    outside_anchor = ocr.TextBox("其他联系人", (.200, .348, .350, .368), (.275, .358), .95)
    assert asyncio.run(run(base_before + [outside_anchor], base_after)) is False
    assert asyncio.run(run(base_before, base_after + [outside_anchor])) is False

    # 5) Negative: moved AssistiveTouch button rejects the occluded anchor.
    orig_find = assistive_vision.find_button_target
    with monkeypatch.context() as mp:
        mp.setattr(
            assistive_vision, "find_button_target",
            lambda f: ((.89, .45), (.82, .42, .96, .48)) if f == after_img else orig_find(f),
        )
        assert asyncio.run(run(base_before + [inside_button], base_after)) is False

    # 6) Negative: open AssistiveTouch panel rejects.
    with monkeypatch.context() as mp:
        mp.setattr(
            assistive_vision, "find_panel",
            lambda f: (.15, .25, .85, .75) if f == after_img else None,
        )
        assert asyncio.run(run(base_before + [inside_button], base_after)) is False

    # 7) Negative: stationary AssistiveTouch box excluded if it overlaps the input field region.
    assert screen_compare.stationary_assistive_button_box(
        before_img, after_img, exclude=((.80, .30, .98, .57),)) is None
    near_field_anchor = ocr.TextBox("良策", (.780, .480, .920, .500), (.850, .490), .95)
    with monkeypatch.context() as mp:
        mp.setattr(
            assistive_vision, "find_button_target",
            lambda f: ((.85, .51), (.75, .47, .95, .55)),
        )
        assert asyncio.run(run(base_before + [near_field_anchor], base_after)) is False
