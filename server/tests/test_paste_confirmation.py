import asyncio
import io
from dataclasses import replace

import pytest
from PIL import Image, ImageDraw

from hidagent import ocr
from hidagent.paste_confirmation import verify_after_paste_permission

REGION = (.15, .30, .85, .55)
EXPECTED = "第二次：你好00042\n校验：XYZabc"


def label(text, x, y):
    return ocr.TextBox(text, (x - .04, y - .012, x + .04, y + .012), (x, y), .99)


def frame(*, sheet=False, inserted=False, moved=False):
    image = Image.new("RGB", (500, 1000), (235, 235, 230))
    draw = ImageDraw.Draw(image)
    offset = 10 if moved else 0
    draw.rectangle((75 + offset, 300, 425 + offset, 550), fill="white", outline="black", width=3)
    if inserted:
        draw.rectangle((116, 354, 230, 358), fill="black")
        draw.rectangle((116, 394, 197, 398), fill="black")
    if sheet:
        draw.rounded_rectangle((90, 350, 410, 650), radius=20, fill=(180, 180, 180))
    result = io.BytesIO()
    image.save(result, "PNG")
    return result.getvalue()


@pytest.mark.parametrize("defect", [None, "still_prompted", "wrong_field", "outside_edit", "existing_text",
                                    "field_moved", "wrong_payload", "ordinary_dialog", "uncertain_original",
                                    "crossing_text"])
def test_permission_confirmation_keeps_evidence_inside_the_original_field(monkeypatch, defect):
    original = frame()
    sheet = frame(sheet=True)
    after = frame(inserted=True, moved=defect == "field_moved")
    # The fixture deliberately includes an identical pair of lines elsewhere.
    # They must never count as evidence of a paste in the focused field.
    context = [label("Safari test page", .25, .13), label("Tester", .25, .25), label("Expected text", .23, .65),
               label("第二次：你好00042", .25, .72), label("校验：XYZabc", .25, .76)]
    dialog = [label('“Safari浏览器”想从“快捷指令”粘贴', .5, .39), label("你允许这样做吗？", .5, .44),
              label("不允许粘贴", .27, .52), label("允许粘贴", .7, .52)]
    actual = [label("第二次：你好00042", .25, .36), label("校验：XYZabc", .25, .40)]
    before_boxes, after_boxes = context.copy(), context + actual
    if defect == "still_prompted":
        after_boxes += dialog
    if defect == "wrong_field":
        after_boxes = context + [label("第二次：你好00042", .25, .57), label("校验：XYZabc", .25, .61)]
    if defect == "outside_edit":
        after_boxes[1] = label("Different recipient", .25, .25)
    if defect == "existing_text":
        before_boxes += actual
    if defect == "uncertain_original":
        before_boxes += [replace(actual[0], conf=.5), actual[1]]
    if defect == "crossing_text":
        after_boxes.append(label("EXTRA", .16, .47))
    if defect == "wrong_payload":
        after_boxes[-1] = label("校验：XYZaBc", .25, .4)
    if defect == "ordinary_dialog":
        dialog[0] = label("Do you want to open this page?", .5, .39)
    frames = {original: before_boxes, sheet: dialog, after: after_boxes}

    async def read(image):
        return frames[image]

    monkeypatch.setattr(ocr, "read_text_async", read)
    proof = verify_after_paste_permission(original, sheet, after, REGION, EXPECTED)
    assert asyncio.run(proof) is (defect is None)


def test_permission_confirmation_accepts_append_after_existing_borderless_text(monkeypatch):
    """Pasting after existing body text in a borderless field still verifies.

    The field has no visible outline, so the outline check is skipped; the
    pasted lines must be new and appear after the lines that were already
    there. The identical pair elsewhere must never count as evidence.
    """
    def borderless_frame(bars, sheet=False):
        image = Image.new("RGB", (500, 1000), (235, 235, 230))
        draw = ImageDraw.Draw(image)
        for y in bars:
            draw.rectangle((116, y - 2, 230, y + 2), fill="black")
        if sheet:
            draw.rounded_rectangle((90, 350, 410, 650), radius=20, fill=(180, 180, 180))
        result = io.BytesIO()
        image.save(result, "PNG")
        return result.getvalue()

    existing = [label("第一行旧文字", .25, .36), label("第二行旧文字", .25, .40)]
    pasted = [label("第二次：你好00042", .25, .44), label("校验：XYZabc", .25, .48)]
    context = [label("Safari test page", .25, .13), label("Tester", .25, .25), label("Expected text", .23, .65),
               label("第二次：你好00042", .25, .72), label("校验：XYZabc", .25, .76)]
    dialog = [label('“Safari浏览器”想从“快捷指令”粘贴', .5, .39), label("你允许这样做吗？", .5, .44),
              label("不允许粘贴", .27, .52), label("允许粘贴", .7, .52)]
    original = borderless_frame([360, 400])
    sheet = borderless_frame([360, 400], sheet=True)
    after = borderless_frame([360, 400, 440, 480])
    frames = {original: context + existing, sheet: dialog, after: context + existing + pasted}

    async def read(image):
        return frames[image]

    monkeypatch.setattr(ocr, "read_text_async", read)
    proof = verify_after_paste_permission(original, sheet, after, REGION, EXPECTED)
    assert asyncio.run(proof) is True

def test_permission_confirmation_accepts_append_starting_with_newline(monkeypatch):
    """Appending text that starts with a newline still verifies.

    The leading blank line has no OCR row, so only the visible lines are
    compared; the transaction digest already pins the exact intended text.
    """
    def borderless_frame(bars, sheet=False):
        image = Image.new("RGB", (500, 1000), (235, 235, 230))
        draw = ImageDraw.Draw(image)
        for y in bars:
            draw.rectangle((116, y - 2, 230, y + 2), fill="black")
        if sheet:
            draw.rounded_rectangle((90, 350, 410, 650), radius=20, fill=(180, 180, 180))
        result = io.BytesIO()
        image.save(result, "PNG")
        return result.getvalue()

    existing = [label("第一行旧文字", .25, .36), label("第二行旧文字", .25, .40)]
    pasted = [label("第二次：你好00042", .25, .44), label("校验：XYZabc", .25, .48)]
    context = [label("Safari test page", .25, .13), label("Tester", .25, .25), label("Expected text", .23, .65),
               label("第二次：你好00042", .25, .72), label("校验：XYZabc", .25, .76)]
    dialog = [label('“Safari浏览器”想从“快捷指令”粘贴', .5, .39), label("你允许这样做吗？", .5, .44),
              label("不允许粘贴", .27, .52), label("允许粘贴", .7, .52)]
    original = borderless_frame([360, 400])
    sheet = borderless_frame([360, 400], sheet=True)
    after = borderless_frame([360, 400, 440, 480])
    frames = {original: context + existing, sheet: dialog, after: context + existing + pasted}

    async def read(image):
        return frames[image]

    monkeypatch.setattr(ocr, "read_text_async", read)
    proof = verify_after_paste_permission(original, sheet, after, REGION,
                                          "\n第二次：你好00042\n校验：XYZabc")
    assert asyncio.run(proof) is True


KB_REGION = (.08, .11, .92, .53)


def keyboard_frame(bars, *, suggestion_variant=0, sheet=False, header_tamper=False, keyboard=True, moved=False):
    image = Image.new("RGB", (500, 1000), (248, 248, 245))
    draw = ImageDraw.Draw(image)
    offset = 10 if moved else 0
    draw.rectangle((40 + offset, 110, 460 + offset, 530), fill="white", outline=(20, 20, 20), width=3)
    for y in bars:
        draw.rectangle((60 + offset, y - 2, 240 + offset, y + 2), fill=(20, 20, 20))
    if keyboard:
        draw.rectangle((0, 580, 500, 1000), fill=(210, 212, 216))
        if suggestion_variant == 0:
            draw.rectangle((60, 608, 140, 624), fill=(60, 60, 60))
        else:
            draw.rectangle((200, 608, 310, 624), fill=(60, 60, 60))
    if header_tamper:
        draw.rectangle((200, 70, 300, 95), fill=(255, 140, 0))
    if sheet:
        draw.rounded_rectangle((90, 350, 410, 600), radius=20, fill=(240, 240, 240))
    result = io.BytesIO()
    image.save(result, "PNG")
    return result.getvalue()


def _kb_context_boxes(*, nav_top_label="返回", nav_top_y=.08, key_bottom_label="return"):
    return [
        label(nav_top_label, .12, nav_top_y),
        label("完成", .91, .08),
        label("123", .06, .88),
        label("space", .50, .88),
        label(key_bottom_label, .88, .88),
    ]


def _kb_suggestion_boxes(variant):
    if variant == 0:
        return [label("effort", .50, .619), label("efforts", .83, .619)]
    return [label('“kLm"', .16, .619), label("part", .50, .620), label("parents", .83, .620)]


def test_permission_confirmation_accepts_located_suggestion_bar_refresh(monkeypatch):
    existing = [label("已有第一行", .25, .20), label("第二行 AbD eFg", .25, .25)]
    pasted = [label("修验通过：云海00417", .25, .30), label("末行 GhJ kLm", .25, .35)]
    dialog = [label("“备忘录”想从“快捷指令”粘贴", .5, .44), label("你允许这样做吗？", .5, .48),
              label("不允许粘贴", .27, .55), label("允许粘贴", .7, .55)]
    original = keyboard_frame([200, 250], suggestion_variant=0)
    sheet = keyboard_frame([200, 250], suggestion_variant=0, sheet=True)
    after = keyboard_frame([200, 250, 300, 350], suggestion_variant=1)
    frames = {
        original: _kb_context_boxes() + _kb_suggestion_boxes(0) + existing,
        sheet: dialog,
        after: _kb_context_boxes() + _kb_suggestion_boxes(1) + existing + pasted,
    }

    async def read(image):
        return frames[image]

    monkeypatch.setattr(ocr, "read_text_async", read)
    proof = verify_after_paste_permission(original, sheet, after, KB_REGION,
                                          "\n修验通过：云海00417\n末行 GhJ kLm")
    assert asyncio.run(proof) is True


@pytest.mark.parametrize("defect", [
    "header_label_changed",
    "header_visual_tamper",
    "keyboard_key_changed",
    "field_moved",
    "existing_text_modified",
    "duplicate_append",
    "keyboard_dismissed",
    "unlocated_bar",
    "region_overlaps_bar",
])
def test_permission_confirmation_rejects_defects_with_suggestion_bar(monkeypatch, defect):
    existing = [label("已有第一行", .25, .20), label("第二行 AbD eFg", .25, .25)]
    pasted = [label("修验通过：云海00417", .25, .30), label("末行 GhJ kLm", .25, .35)]
    dialog = [label("“备忘录”想从“快捷指令”粘贴", .5, .44), label("你允许这样做吗？", .5, .48),
              label("不允许粘贴", .27, .55), label("允许粘贴", .7, .55)]
    kb_before = defect != "unlocated_bar"
    kb_after = defect not in {"keyboard_dismissed", "unlocated_bar"}
    original = keyboard_frame([200, 250], suggestion_variant=0, keyboard=kb_before)
    sheet = keyboard_frame([200, 250], suggestion_variant=0, sheet=True)
    after = keyboard_frame(
        [200, 250, 300, 350],
        suggestion_variant=1,
        header_tamper=(defect == "header_visual_tamper"),
        keyboard=kb_after,
        moved=(defect == "field_moved"),
    )
    after_ctx = _kb_context_boxes(
        nav_top_label="群聊" if defect == "header_label_changed" else "返回",
        key_bottom_label="前往" if defect == "keyboard_key_changed" else "return",
    )
    after_existing = (
        [label("已改第一行", .25, .20), label("第二行 AbD eFg", .25, .25)]
        if defect == "existing_text_modified"
        else existing
    )
    after_pasted = (
        pasted + [label("修验通过：云海00417", .25, .40), label("末行 GhJ kLm", .25, .45)]
        if defect == "duplicate_append"
        else pasted
    )
    frames = {
        original: _kb_context_boxes() + _kb_suggestion_boxes(0) + existing,
        sheet: dialog,
        after: after_ctx + _kb_suggestion_boxes(1) + after_existing + after_pasted,
    }

    async def read(image):
        return frames[image]

    monkeypatch.setattr(ocr, "read_text_async", read)
    region = (.08, .11, .92, .60) if defect == "region_overlaps_bar" else KB_REGION
    proof = verify_after_paste_permission(original, sheet, after, region,
                                          "\n修验通过：云海00417\n末行 GhJ kLm")
    assert asyncio.run(proof) is False


def test_permission_confirmation_reuses_before_after_ocr_across_sheet_candidates(monkeypatch):
    def plain_variant(shade):
        image = Image.new("RGB", (500, 1000), (235, 235, 230))
        draw = ImageDraw.Draw(image)
        draw.rectangle((75, 300, 425, 550), fill="white", outline="black", width=3)
        draw.rectangle((10, 10, 20, 20), fill=(shade, shade, shade))
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue()

    original = frame()
    cand_1, cand_2 = plain_variant(230), plain_variant(232)
    sheet = frame(sheet=True)
    after = frame(inserted=True)
    context = [label("Safari test page", .25, .13), label("Tester", .25, .25), label("Footer anchor", .23, .65)]
    dialog = [label('“Safari浏览器”想从“快捷指令”粘贴', .5, .39), label("你允许这样做吗？", .5, .44),
              label("不允许粘贴", .27, .52), label("允许粘贴", .7, .52)]
    actual = [label("第二次：你好00042", .25, .36), label("校验：XYZabc", .25, .40)]
    frames = {
        original: context,
        after: context + actual,
        cand_1: context,
        cand_2: context,
        sheet: dialog,
    }
    calls = []

    async def read(image):
        calls.append(image)
        return frames[image]

    monkeypatch.setattr(ocr, "read_text_async", read)
    for cand, ok in ((cand_1, False), (cand_2, False), (sheet, True)):
        assert asyncio.run(verify_after_paste_permission(original, cand, after, REGION, EXPECTED)) is ok
    assert calls.count(original) == 1
    assert calls.count(after) == 1
    assert calls.count(cand_1) == 1 and calls.count(cand_2) == 1 and calls.count(sheet) == 1

    calls.clear()
    wrong = EXPECTED.replace("abc", "aBc")
    for cand in (cand_1, cand_2):
        assert asyncio.run(verify_after_paste_permission(original, cand, after, REGION, wrong)) is False
    assert calls.count(original) == 1
    assert calls.count(after) == 1
    assert cand_1 not in calls and cand_2 not in calls


def test_matches_wrapped_lines_preserves_newlines_and_rejects_edits():
    from hidagent.paste_confirmation import _matches_wrapped_lines

    assert _matches_wrapped_lines(("第一行", "第二行"), ("第一行", "第二行")) is True
    assert _matches_wrapped_lines(("长行前半", "长行后半"), ("长行前半长行后半",)) is True
    assert _matches_wrapped_lines(("第一", "部分", "末尾"), ("第一部分末尾",)) is True
    assert _matches_wrapped_lines(
        ("标题甲", "正文第一段", "前半截", "后半", "截", "落款"),
        ("标题甲", "正文第一段", "前半截后半截", "落款"),
    ) is True

    assert _matches_wrapped_lines(("第一行第二行",), ("第一行", "第二行")) is False
    assert _matches_wrapped_lines(("第一行", "第二行", "第二行"), ("第一行", "第二行")) is False
    assert _matches_wrapped_lines(("长行前半", "长行后"), ("长行前半长行后半",)) is False
    assert _matches_wrapped_lines(("长行前半", "长行改半"), ("长行前半长行后半",)) is False
    assert _matches_wrapped_lines((), ("第一行",)) is False
    assert _matches_wrapped_lines(("多余行",), ()) is False


@pytest.mark.parametrize(
    ("before_right", "after_right", "after_text", "confirmed"),
    [
        pytest.param(.800, .804, "Chat history", True, id="after-box-grazes-button"),
        pytest.param(.804, .800, "Chat history", True, id="before-box-grazes-button"),
        pytest.param(.804, .806, "Changed history", False, id="changed-text-grazes-button"),
        pytest.param(.804, None, "", False, id="removed-text-grazes-button"),
        pytest.param(None, .804, "New history", False, id="added-text-grazes-button"),
    ],
)
def test_scrolled_context_checks_visible_text_at_assistive_edge(before_right, after_right, after_text, confirmed):
    from hidagent.paste_confirmation import _same_context_scrolled

    before_region = (.14, .53, .76, .58)
    after_region = (.14, .45, .76, .58)
    stationary = [label("Conversation", .50, .09), label("Space", .50, .88), label("Return", .89, .88)]
    before = stationary + [label("Another message", .32, .27)]
    after = stationary + [label("Another message", .32, .19)]
    if before_right is not None:
        before.append(ocr.TextBox("Chat history", (.20, .42, before_right, .44),
                                  ((.20 + before_right) / 2, .43), .99))
    if after_right is not None:
        after.append(ocr.TextBox(after_text, (.20, .34, after_right, .36),
                                 ((.20 + after_right) / 2, .35), .99))

    assert _same_context_scrolled(
        before, after, before_region, after_region, split_y=.11, scroll_delta=.08,
        ignored_boxes=((.802, .40, .98, .48),),
    ) is confirmed


@pytest.mark.parametrize("visible_before", [True, False])
def test_scrolled_context_allows_an_anchor_occluded_by_assistive_button(visible_before):
    from hidagent.paste_confirmation import _same_context_scrolled

    stationary = [label("Conversation", .50, .09), label("Space", .50, .88), label("Return", .89, .88)]
    before = stationary + [label("Another message", .32, .27)]
    after = stationary + [label("Another message", .32, .19)]
    if visible_before:
        before.append(label("Covered text", .90, .51))
    else:
        after.append(label("Revealed text", .90, .35))

    assert _same_context_scrolled(
        before, after, (.14, .55, .76, .58), (.14, .47, .76, .58), split_y=.11, scroll_delta=.08,
        ignored_boxes=((.802, .40, .98, .48),),
    ) is True


@pytest.mark.parametrize("drift", [0., .007])
@pytest.mark.parametrize("button_side_right", [.804, .840], ids=["grazing", "occluded"])
def test_scrolled_context_checks_visible_displacement_with_repeated_button_side_text(drift, button_side_right):
    from hidagent.paste_confirmation import _same_context_scrolled

    def repeated_line(left, right, y):
        return ocr.TextBox("Repeated line", (left, y - .010, right, y + .010), (.50, y), .99)

    stationary = [label("Conversation", .50, .09), label("Space", .50, .88), label("Return", .89, .88)]
    before = stationary + [label("Another message", .32, .30), repeated_line(.20, .80, .430),
                           repeated_line(1 - button_side_right, button_side_right, .423)]
    # With drift, both repeated lines are closer to the other line's expected
    # location. The occluded line must not take either visible anchor's place.
    after = stationary + [label("Another message", .32, .22 - drift), repeated_line(.20, .80, .350 - drift),
                          repeated_line(1 - button_side_right, button_side_right, .343 + drift)]

    assert _same_context_scrolled(
        before, after, (.14, .55, .76, .58), (.14, .47, .76, .58), split_y=.11, scroll_delta=.08,
        ignored_boxes=((.802, .40, .98, .48),),
    ) is (drift == 0.)


def test_permission_confirmation_accepts_stationary_assistive_without_suggestion_bar(monkeypatch):
    from hidagent import paste_confirmation, screen_compare

    original = keyboard_frame([200, 250], keyboard=False)
    sheet = keyboard_frame([200, 250], keyboard=False, sheet=True)
    after = keyboard_frame([200, 250, 300, 350], keyboard=False)
    tampered = keyboard_frame([200, 250, 300, 350], keyboard=False, header_tamper=True)

    context = [
        label("返回", .12, .08),
        label("完成", .91, .08),
        label("123", .06, .88),
        label("底部锚点", .50, .88),
    ]
    occluded = [ocr.TextBox("被遮字", (.83, .58, .95, .62), (.89, .60), .96)]
    pasted = [label("修验通过：云海00417", .25, .30), label("末行 GhJ kLm", .25, .35)]
    dialog = [label("“备忘录”想从“快捷指令”粘贴", .5, .44), label("你允许这样做吗？", .5, .48),
              label("不允许粘贴", .27, .55), label("允许粘贴", .7, .55)]

    monkeypatch.setattr(screen_compare, "stationary_assistive_button_box",
                        lambda before, after, *, exclude=(): (.80, .56, .98, .64))

    async def verify_case(after_img):
        paste_confirmation._PAIR_CACHE = None
        frames = {original: context + occluded, sheet: dialog, after_img: context + pasted}

        async def read(image):
            return frames[image]

        monkeypatch.setattr(ocr, "read_text_async", read)
        return await verify_after_paste_permission(
            original, sheet, after_img, KB_REGION, "\n修验通过：云海00417\n末行 GhJ kLm",
        )

    assert asyncio.run(verify_case(after)) is True
    assert asyncio.run(verify_case(tampered)) is False


@pytest.mark.parametrize("edit", [
    pytest.param(None, id="clipped-avatar"),
    pytest.param((87, -1, -75), id="header-edge-edit"),
    pytest.param((87, 0, -75), id="viewport-edge-edit"),
    pytest.param((20, -2, -55), id="header-edit-over-avatar"),
    pytest.param((20, -1, -80), id="header-edge-edit-over-avatar"),
    pytest.param((20, 1, 65), id="viewport-edit-over-avatar"),
])
@pytest.mark.parametrize(("fractional_edge", "overlay"), [
    pytest.param(False, False, id="pixel-aligned-cut"),
    pytest.param(True, False, id="fractional-cut"),
    pytest.param(True, True, id="fractional-cut-with-stationary-overlay"),
])
def test_rigid_scroll_verifies_avatar_clipped_at_header_edge(edit, fractional_edge, overlay):
    from hidagent.paste_confirmation import _scrolled_viewport_match

    width, height = 500, 1080
    split_y = 117
    scroll = 77.1
    original = Image.new("RGB", (width, height), (246, 246, 243))
    draw = ImageDraw.Draw(original)
    draw.rectangle((0, 0, width, split_y - 1), fill=(223, 226, 230))
    draw.rectangle((165, 94, 330, 99), fill=(45, 50, 52))
    draw.rectangle((17, 167, 70, 204), fill=(15, 34, 55))
    draw.rectangle((85, 201, 340, 235), fill=(205, 196, 221))
    draw.rectangle((260, 320, 462, 375), fill=(149, 211, 139))
    draw.rectangle((25, 400, 310, 460), fill=(174, 185, 201))
    draw.rectangle((0, 635, width, height), fill=(207, 212, 219))

    viewport_bottom = 493
    after = original.copy()
    shifted = original.transform(original.size, Image.Transform.AFFINE, (1, 0, 0, 0, 1, scroll),
                                 resample=Image.Resampling.BICUBIC)
    after.paste(shifted.crop((0, split_y, width, viewport_bottom)), (0, split_y))
    if fractional_edge:
        seam_row = (0, split_y, width, split_y + 1)
        mixed = Image.blend(original.crop(seam_row), shifted.crop(seam_row), .55)
        after.paste(mixed, seam_row[:2])
    if edit is not None:
        x, offset, delta = edit
        y = split_y + offset
        color = tuple(value + delta for value in after.getpixel((x, y)))
        ImageDraw.Draw(after).line((x, y, x + 47, y), fill=color)
    assistive_box = None
    if overlay:
        for image in (original, after):
            ImageDraw.Draw(image).rectangle((411, 104, 489, 169), fill=(70, 70, 70))
        assistive_box = (402 / width, 95 / height, 498 / width, 179 / height)

    def encoded(image):
        result = io.BytesIO()
        image.save(result, "PNG")
        return result.getvalue()

    match = _scrolled_viewport_match(
        encoded(original), encoded(after), original, after, (0., viewport_bottom / height, 1., .56),
        None, (), growth_delta=77 / height, split_bounds=(.0965, .1214), assistive_box=assistive_box,
    )
    if edit is not None:
        assert match is None
    else:
        assert match is not None
        assert match == pytest.approx((split_y / height, scroll / height), abs=.002)


def test_matches_chat_composer_lines_tail_punctuation_and_rejections():
    from hidagent.paste_confirmation import _matches_chat_composer_lines

    long_msg = ("这期具体保质期每款不一样，商品详情页一般会写，", "你点开对应链接看下；页面没标的我帮你问群主确认。")
    assert _matches_chat_composer_lines(
        ("这期具体保质期每款不一样，商品详情页一般会写，", "你点开对应链接看下；页面", "没标的我帮你问群主确认。"),
        long_msg,
    ) == ("full", "")
    assert _matches_chat_composer_lines(
        ("这期具体保质期每款不一样，商品详情页一般会写，", "你点开对应链接看下；页面", "没标的我帮你问群主确认"),
        long_msg,
    ) == ("tail_punct", "。")
    assert _matches_chat_composer_lines(
        ("请联系客服并在群内确认好发货时间后再拍……",),
        ("请联系客服并在群内确认好发货时间后再拍……",),
    ) == ("full", "")
    assert _matches_chat_composer_lines(
        ("请联系客服并在群内确认好发货时间后再拍",),
        ("请联系客服并在群内确认好发货时间后再拍……",),
    ) == ("tail_punct", "……")

    # Strict rejections: short strings, protected digits/ASCII/URLs, CJK chars, interior edits, missing lines, dupes.
    assert _matches_chat_composer_lines(("收到了，没问题",), ("收到了，没问题。",)) == (None, "")
    assert _matches_chat_composer_lines(
        ("本次团购活动统一到手价仅需89",), ("本次团购活动统一到手价仅需89。",),
    ) == (None, "")
    assert _matches_chat_composer_lines(("活动优惠直降金额合计是-10",), ("活动优惠直降金额合计是-10。",)) == (None, "")
    assert _matches_chat_composer_lines(
        ("点击活动链接https://a.com/x",), ("点击活动链接https://a.com/x。",),
    ) == (None, "")
    assert _matches_chat_composer_lines(
        ("请联系客服并在群内确认好发货时间后再拍",),
        ("请联系客服并在群内确认好发货时间后再拍了。",),
    ) == (None, "")
    assert _matches_chat_composer_lines(
        ("这期具体保质期每款不一样，商品详情页一般会", "你点开对应链接看下；页面没标的我帮你问群主确认。"),
        long_msg,
    ) == (None, "")
    assert _matches_chat_composer_lines(
        ("这期具体保质期每款不一样，商品详情页一般会写", "你点开对应链接看下；页面没标的我帮你问群主确认"),
        long_msg,
    ) == ("ocr_punct", "，。")
    assert _matches_chat_composer_lines(
        ("这期具体保质期每款不一样商品详情页一般会写", "你点开对应链接看下；页面没标的我帮你问群主确认"),
        long_msg,
    ) == (None, "")
    assert _matches_chat_composer_lines(
        ("这期具体保质期每款不一样，商品详情页一般会写，", "你点开对应链接看下；页面没标的我帮你问群主确人"),
        long_msg,
    ) == (None, "")
    assert _matches_chat_composer_lines(
        ("第一行说明完整内容在这里",),
        ("第一行说明完整内容在这里", "第二行说明完整内容。"),
    ) == (None, "")
    assert _matches_chat_composer_lines(
        ("请直接点击小程序查看详情。", "多余一行"),
        ("请直接点击小程序查看详情。",),
    ) == (None, "")


def _chat_tail_frame(*, pasted: bool, grown: bool = False, key_blue: bool = False):
    image = Image.new("RGB", (500, 1080), (237, 237, 237))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 500, 118), fill=(237, 237, 237))
    top = 514 if (pasted and grown) else 562
    draw.rectangle((66, top, 382, 618), fill="white")
    if pasted:
        draw.rectangle((92, top + 16, 320, top + 24), fill=(35, 35, 35))
        if grown:
            draw.rectangle((92, top + 56, 300, top + 64), fill=(35, 35, 35))
    draw.rectangle((0, 626, 500, 700), fill=(214, 217, 222))
    draw.rectangle((10, 646, 490, 676), fill=(214, 217, 222))
    draw.rectangle((0, 700, 500, 1080), fill=(214, 217, 222))
    send_fill = (10, 132, 255) if key_blue else (200, 204, 212)
    draw.rounded_rectangle((380, 924, 494, 974), radius=8, fill=send_fill)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


@pytest.mark.parametrize("grown", [False, True])
def test_chat_structural_sync_accepts_tail_punct_only_when_send_turns_blue(monkeypatch, grown):
    from hidagent import paste_confirmation

    field = (66 / 500, 562 / 1080, 382 / 500, 618 / 1080)
    text = (
        "第一行详细团购活动说明文字内容\n第二行请按链接提示核对并确认。"
        if grown else "这期具体保质期看详情页链接确认。"
    )
    before = _chat_tail_frame(pasted=False, grown=False, key_blue=False)
    after = _chat_tail_frame(pasted=True, grown=grown, key_blue=True)
    stale_before = _chat_tail_frame(pasted=False, grown=False, key_blue=True)
    grey_after = _chat_tail_frame(pasted=True, grown=grown, key_blue=False)

    header = [label("测试群(10)", .45, .075)]
    kb = [label("发送", .88, .88), label("123", .08, .88)]
    if grown:
        field_items = [
            ocr.TextBox("第一行详细团购活动说明文字内容", (.18, .49, .70, .505), (.44, .497), .99),
            ocr.TextBox("第二行请按链接提示核对并确认", (.18, .53, .68, .545), (.43, .537), .99),
        ]
    else:
        field_items = [ocr.TextBox("这期具体保质期看详情页链接确认", (.18, .535, .68, .555), (.43, .545), .99)]

    items = {
        before: header + [label("候选A", .20, .615)] + kb,
        stale_before: header + [label("候选A", .20, .615)] + kb,
        after: header + field_items + [label("候选B", .20, .615)] + kb,
        grey_after: header + field_items + [label("候选B", .20, .615)] + kb,
    }
    monkeypatch.setattr(ocr, "read_text_async", lambda frame: _async_val(items[frame]))
    assert asyncio.run(paste_confirmation.verify_chat_structural_paste(before, after, field, text)) is not None
    # Reject stale-blue send key, non-blue send key, and non-send keys in both grown and single-line branches.
    assert asyncio.run(paste_confirmation.verify_chat_structural_paste(stale_before, after, field, text)) is None
    assert asyncio.run(paste_confirmation.verify_chat_structural_paste(before, grey_after, field, text)) is None
    search_kb = [label("搜索", .88, .88), label("123", .08, .88)]
    monkeypatch.setattr(ocr, "read_text_async", lambda frame: _async_val({
        before: header + [label("候选A", .20, .615)] + search_kb,
        after: header + field_items + [label("候选B", .20, .615)] + search_kb,
    }[frame]))
    assert asyncio.run(paste_confirmation.verify_chat_structural_paste(before, after, field, text)) is None

    # Permission-sheet path remains strictly exact on trailing punctuation using 3 distinct frames.
    sheet = _chat_tail_frame(pasted=False, grown=False, key_blue=False) + b"\x00"
    dialog = [
        label("“微信”想从“快捷指令”粘贴", .5, .39),
        label("你允许这样做吗？", .5, .44),
        label("不允许粘贴", .27, .52),
        label("允许粘贴", .7, .52),
    ]
    exact_items = (
        [
            ocr.TextBox("第一行详细团购活动说明文字内容", (.18, .49, .70, .505), (.44, .497), .99),
            ocr.TextBox("第二行请按链接提示核对并确认。", (.18, .53, .70, .545), (.44, .537), .99),
        ]
        if grown else [ocr.TextBox("这期具体保质期看详情页链接确认。", (.18, .535, .70, .555), (.44, .545), .99)]
    )
    after_exact = _chat_tail_frame(pasted=True, grown=grown, key_blue=True) + b"\x01"
    monkeypatch.setattr(ocr, "read_text_async", lambda frame: _async_val({
        before: header + [label("候选A", .20, .615)] + kb,
        sheet: dialog,
        after: header + field_items + [label("候选A", .20, .615)] + kb,
        after_exact: header + exact_items + [label("候选A", .20, .615)] + kb,
    }[frame]))
    assert asyncio.run(verify_after_paste_permission(before, sheet, after, field, text)) is False
    assert asyncio.run(verify_after_paste_permission(before, sheet, after_exact, field, text)) is True


def test_matches_chat_composer_lines_thin_punct_width_and_numeric_safety():
    from hidagent.paste_confirmation import _matches_chat_composer_lines

    # Intact digits and URLs allow thin CJK punctuation recovery elsewhere in the message.
    assert _matches_chat_composer_lines(
        ("一般48小时内发货，点进「订单详情就能看到物流单号。",),
        ("一般48小时内发货，点进「订单详情」就能看到物流单号。",),
    ) == ("ocr_punct", "」")
    assert _matches_chat_composer_lines(
        ("本次团购到手价89元，请先查看详情确认",), ("本次团购到手价89元，请先查看详情确认。",),
    ) == ("tail_punct", "。")
    assert _matches_chat_composer_lines(
        ("活动链接https://a.com/x，请确认后下单",), ("活动链接https://a.com/x，请确认后下单。",),
    ) == ("tail_punct", "。")

    # Exact 144526 repro: missing thin bracket '」' + corner_fade '帮->都' + width-equiv ';'.
    msg_144526 = ("在群里的官方团购小程序进「订单详情」就能看到物流单号；找不到的话联系群主帮你看下。",)
    assert _matches_chat_composer_lines(
        ("在群里的官方团购小程序进", "「订单详情就能看到物流单", "号；找不到的话联系群主帮", "你看下。"),
        msg_144526,
    ) == ("ocr_punct", "」")
    assert _matches_chat_composer_lines(
        ("在群里的官方团购小程序进", "「订单详情就能看到物流单", "号;找不到的话联系群主帮", "你看下。"),
        msg_144526,
    ) == ("ocr_punct", "」")
    assert _matches_chat_composer_lines(
        ("在群里的官方团购小程序进", "「订单详情就能看到物流单", "号；找不到的话联系群主都", "你看下。"),
        msg_144526,
        faded_rows={2: "sub"},
    ) == ("corner_fade", "帮->都;punct=」")

    # Safety boundaries: altered digits/price/URL/punctuation adjacent to digit or >2 puncts must reject.
    assert _matches_chat_composer_lines(
        ("一般4小时内发货，点进「订单详情」就能看到物流单号。",),
        ("一般48小时内发货，点进「订单详情」就能看到物流单号。",),
    ) == (None, "")
    assert _matches_chat_composer_lines(
        ("本次团购活动到手价89元，含税仅199元",),
        ("本次团购活动到手价89元，含税仅19.9元",),
    ) == (None, "")
    assert _matches_chat_composer_lines(
        ("请联系客服并在群内确认好发货时间，谢谢。",),
        ("请联系客服并在群内确认好发货时间后再拍。",),
    ) == (None, "")
    assert _matches_chat_composer_lines(
        ("在群里的官方团购小程序进「订单详情就能看到物流单号找不到的话联系群主帮你看下",),
        msg_144526,
    ) == (None, "")
    assert _matches_chat_composer_lines(
        ("本次团购到手价89天，请查看详情说明确认。",),
        ("本次团购到手价89元，请查看详情说明确认。",),
        faded_rows={0: "sub"},
    ) == (None, "")
    # Trailing punctuation immediately after a digit or URL or short last line (< 6 chars) must remain exact.
    assert _matches_chat_composer_lines(
        ("本次团购活动统一到手价仅需89",), ("本次团购活动统一到手价仅需89。",),
    ) == (None, "")
    assert _matches_chat_composer_lines(
        ("点击活动链接https://a.com/x",), ("点击活动链接https://a.com/x。",),
    ) == (None, "")
    assert _matches_chat_composer_lines(
        ("这里是一段足够长的第一行说明文字内容", "好"), ("这里是一段足够长的第一行说明文字内容", "好。"),
    ) == (None, "")


def test_chat_structural_sync_filters_stationary_icon_only(monkeypatch):
    from hidagent import paste_confirmation

    field = (66 / 500, 562 / 1080, 382 / 500, 618 / 1080)
    short_text = "收到，这就处理。"
    icon_box = (.69, .545, .725, .562)
    icon_px = (348, 591, 359, 604)

    def make_frame(*, pasted: bool, key_blue: bool, draw_icon: bool = True,
                   draw_new_glyph: bool = False, icon_color=(55, 55, 55), field_bg="white"):
        image = Image.new("RGB", (500, 1080), (237, 237, 237))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 500, 118), fill=(237, 237, 237))
        draw.rectangle((66, 562, 382, 618), fill=field_bg)
        if draw_icon:
            draw.rectangle(icon_px, fill=icon_color)
        if draw_new_glyph:
            draw.rectangle(icon_px, fill=(20, 20, 20))
        if pasted:
            draw.rectangle((92, 578, 220, 586), fill=(35, 35, 35))
        draw.rectangle((0, 626, 500, 700), fill=(214, 217, 222))
        draw.rectangle((10, 646, 490, 676), fill=(214, 217, 222))
        draw.rectangle((0, 700, 500, 1080), fill=(214, 217, 222))
        send_fill = (10, 132, 255) if key_blue else (200, 204, 212)
        draw.rounded_rectangle((380, 924, 494, 974), radius=8, fill=send_fill)
        out = io.BytesIO()
        image.save(out, "PNG")
        return out.getvalue()

    header = [label("测试群(10)", .45, .075)]
    kb = [label("发送", .88, .88), label("123", .08, .88)]
    pasted_item = ocr.TextBox(short_text, (.18, .535, .44, .555), (.31, .545), .99)
    hallucinated_u = ocr.TextBox("U", icon_box, (.707, .553), .97)
    hallucinated_second = ocr.TextBox("0", (.65, .545, .685, .562), (.667, .553), .97)
    overlapping_u = ocr.TextBox("U", (.42, .545, .455, .562), (.437, .553), .97)
    stationary_period = ocr.TextBox("。", icon_box, (.707, .553), .97)
    left_draft = ocr.TextBox("旧", (.16, .545, .19, .562), (.175, .553), .98)

    before_icon = make_frame(pasted=False, key_blue=False, draw_icon=True)
    after_icon = make_frame(pasted=True, key_blue=True, draw_icon=True)
    before_blank = make_frame(pasted=False, key_blue=False, draw_icon=False)
    after_new_u = make_frame(pasted=True, key_blue=True, draw_icon=False, draw_new_glyph=True)
    # Spatially flat colored patch (200, 200, 240): RGB channel spread 40 but grayscale luma contrast 0.
    before_flat_tint = make_frame(pasted=False, key_blue=False, draw_icon=False, field_bg=(200, 200, 240))
    after_flat_tint = make_frame(pasted=True, key_blue=True, draw_icon=False, field_bg=(200, 200, 240))

    # 1. Stationary drawn bottom-right icon misread as 'U' only in after is filtered.
    monkeypatch.setattr(ocr, "read_text_async", lambda frame: _async_val({
        before_icon: header + [label("候选A", .20, .615)] + kb,
        after_icon: header + [pasted_item, hallucinated_u, label("候选B", .20, .615)] + kb,
    }[frame]))
    assert asyncio.run(paste_confirmation.verify_chat_structural_paste(
        before_icon, after_icon, field, short_text)) is not None

    # 2. If 'U' was already read in before OCR, before is non-empty -> strictly rejected.
    monkeypatch.setattr(ocr, "read_text_async", lambda frame: _async_val({
        before_icon: header + [hallucinated_u, label("候选A", .20, .615)] + kb,
        after_icon: header + [pasted_item, hallucinated_u, label("候选B", .20, .615)] + kb,
    }[frame]))
    assert asyncio.run(paste_confirmation.verify_chat_structural_paste(
        before_icon, after_icon, field, short_text)) is None

    # 3. Newly pasted 'U' drawn only in after (white -> dark ink) MUST NOT be filtered.
    monkeypatch.setattr(ocr, "read_text_async", lambda frame: _async_val({
        before_blank: header + [label("候选A", .20, .615)] + kb,
        after_new_u: header + [pasted_item, hallucinated_u, label("候选B", .20, .615)] + kb,
    }[frame]))
    assert asyncio.run(paste_confirmation.verify_chat_structural_paste(
        before_blank, after_new_u, field, short_text)) is None

    # 4. Flat white pixels AND flat tinted patch (200, 200, 240) MUST NOT be filtered.
    monkeypatch.setattr(ocr, "read_text_async", lambda frame: _async_val({
        before_blank: header + [label("候选A", .20, .615)] + kb,
        after_icon: header + [pasted_item, hallucinated_u, label("候选B", .20, .615)] + kb,
    }[frame]))
    assert asyncio.run(paste_confirmation.verify_chat_structural_paste(
        before_blank, after_icon, field, short_text)) is None
    monkeypatch.setattr(ocr, "read_text_async", lambda frame: _async_val({
        before_flat_tint: header + [label("候选A", .20, .615)] + kb,
        after_flat_tint: header + [pasted_item, hallucinated_u, label("候选B", .20, .615)] + kb,
    }[frame]))
    assert asyncio.run(paste_confirmation.verify_chat_structural_paste(
        before_flat_tint, after_flat_tint, field, short_text)) is None

    # 5. Two stationary candidates, overlapping candidate, expected suffix "。", and left draft MUST NOT be filtered.
    for bad_after in (
        header + [pasted_item, hallucinated_second, hallucinated_u, label("候选B", .20, .615)] + kb,
        header + [pasted_item, overlapping_u, label("候选B", .20, .615)] + kb,
        header + [ocr.TextBox(short_text[:-1], (.18, .535, .64, .555), (.41, .545), .99),
                  stationary_period, label("候选B", .20, .615)] + kb,
    ):
        monkeypatch.setattr(ocr, "read_text_async", lambda frame, a=bad_after: _async_val({
            before_icon: header + [label("候选A", .20, .615)] + kb,
            after_icon: a,
        }[frame]))
        target_msg = short_text[:-1] if stationary_period in bad_after else short_text
        assert asyncio.run(paste_confirmation.verify_chat_structural_paste(
            before_icon, after_icon, field, target_msg)) is None

    # Directly verify _filter_stationary_field_icons never drops "。" even when drawn pixels are stationary.
    with Image.open(io.BytesIO(before_icon)) as b_img, Image.open(io.BytesIO(after_icon)) as a_img:
        kept = paste_confirmation._filter_stationary_field_icons(
            [stationary_period], b_img.convert("RGB"), a_img.convert("RGB"), field,
            expected=("这里有足够长的普通聊天消息先发。",),
        )
        assert kept == [stationary_period]

    monkeypatch.setattr(ocr, "read_text_async", lambda frame: _async_val({
        before_icon: header + [left_draft, label("候选A", .20, .615)] + kb,
        after_icon: header + [left_draft, pasted_item, label("候选B", .20, .615)] + kb,
    }[frame]))
    assert asyncio.run(paste_confirmation.verify_chat_structural_paste(
        before_icon, after_icon, field, short_text)) is None


def test_chat_structural_sync_corner_fade_physical_proof_and_rejections(monkeypatch):
    from hidagent import paste_confirmation

    field = (66 / 500, 562 / 1080, 382 / 500, 618 / 1080)
    icon_px = (348, 591, 359, 604)
    text = "这个得赶紧找群主确认，没发货前一般还能协调改，具体看订单状态。"

    def make_frame(*, pasted: bool, key_blue: bool, draw_icon: bool = True,
                   faded_corner: bool = True, kb_label_blue: bool = True,
                   draw_right_glyph: bool = True):
        image = Image.new("RGB", (500, 1080), (237, 237, 237))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 500, 118), fill=(237, 237, 237))
        top = 514 if pasted else 562
        draw.rectangle((66, top, 382, 618), fill="white")
        if draw_icon:
            draw.rectangle(icon_px, fill=(45, 45, 45))
        if pasted:
            draw.rectangle((92, top + 16, 260, top + 24), fill=(35, 35, 35))
            # Row 2 left text crisp black (luma 15, wide enough white margin for _field_span)
            draw.rectangle((140, 558, 326, 582), fill=(15, 15, 15))
            if draw_right_glyph:
                draw.rectangle((335, 558, 356, 568), fill=(70, 70, 70))
                lower_fill = (155, 155, 155) if faded_corner else (10, 10, 10)
                draw.rectangle((335, 570, 356, 582), fill=lower_fill)
            draw.rectangle((78, 588, 228, 606), fill=(15, 15, 15))
        draw.rectangle((0, 626, 500, 700), fill=(214, 217, 222))
        draw.rectangle((10, 646, 490, 676), fill=(214, 217, 222))
        draw.rectangle((0, 700, 500, 1080), fill=(214, 217, 222))
        send_fill = (10, 132, 255) if (key_blue and kb_label_blue) else (200, 204, 212)
        draw.rounded_rectangle((380, 924, 494, 974), radius=8, fill=send_fill)
        out = io.BytesIO()
        image.save(out, "PNG")
        return out.getvalue()

    header = [label("测试群(10)", .45, .075)]
    kb = [label("发送", .88, .88), label("123", .08, .88)]
    search_kb = [label("搜索", .88, .88), label("123", .08, .88)]
    row1 = ocr.TextBox("这个得赶紧找群主确认，没", (.158, .492, .713, .515), (.435, .503), .99)
    row2_mu = ocr.TextBox("发货前一般还能协调改，目", (.156, .514, .713, .542), (.434, .528), .98)
    row2_interior_bad = ocr.TextBox("发货前一般还能协凋改，目", (.156, .514, .713, .542), (.434, .528), .98)
    row3 = ocr.TextBox("体看订单状态。", (.158, .542, .459, .565), (.308, .553), .99)
    row3_no_punct = ocr.TextBox("体看订单状态", (.158, .542, .440, .565), (.299, .553), .99)

    prep_icon = make_frame(pasted=False, key_blue=False, draw_icon=True)
    after_faded = make_frame(pasted=True, key_blue=True, draw_icon=True, faded_corner=True)
    after_crisp = make_frame(pasted=True, key_blue=True, draw_icon=True, faded_corner=False)
    after_omit_blank = make_frame(
        pasted=True, key_blue=True, draw_icon=True, faded_corner=True, draw_right_glyph=False,
    )
    prep_no_icon = make_frame(pasted=False, key_blue=False, draw_icon=False)
    after_no_icon = make_frame(pasted=True, key_blue=True, draw_icon=False, faded_corner=True)
    row2_omitted = ocr.TextBox("发货前一般还能协调改，", (.158, .517, .659, .540), (.408, .528), .99)

    # Positive: stationary corner icon + bleached right glyph accepts "sub" (具->目) and "omit" (具->).
    monkeypatch.setattr(ocr, "read_text_async", lambda frame: _async_val({
        prep_icon: header + [label("候选A", .20, .615)] + kb,
        after_faded: header + [row1, row2_mu, row3, label("候选B", .20, .615)] + kb,
    }[frame]))
    assert asyncio.run(paste_confirmation.verify_chat_structural_paste(prep_icon, after_faded, field, text)) is not None

    monkeypatch.setattr(ocr, "read_text_async", lambda frame: _async_val({
        prep_icon: header + [label("候选A", .20, .615)] + kb,
        after_faded: header + [row1, row2_omitted, row3, label("候选B", .20, .615)] + kb,
    }[frame]))
    assert asyncio.run(paste_confirmation.verify_chat_structural_paste(prep_icon, after_faded, field, text)) is not None

    # Omission with blank white pixels (no glyph rendered right of x1) or crisp glyph MUST fail.
    for bad_frame in (after_omit_blank, after_crisp):
        monkeypatch.setattr(ocr, "read_text_async", lambda frame, bf=bad_frame: _async_val({
            prep_icon: header + [label("候选A", .20, .615)] + kb,
            bf: header + [row1, row2_omitted, row3, label("候选B", .20, .615)] + kb,
        }[frame]))
        assert asyncio.run(paste_confirmation.verify_chat_structural_paste(prep_icon, bad_frame, field, text)) is None

    # Negative 1: crisp black right glyph (no physical bleach) MUST NOT use corner_fade.
    monkeypatch.setattr(ocr, "read_text_async", lambda frame: _async_val({
        prep_icon: header + [label("候选A", .20, .615)] + kb,
        after_crisp: header + [row1, row2_mu, row3, label("候选B", .20, .615)] + kb,
    }[frame]))
    assert asyncio.run(paste_confirmation.verify_chat_structural_paste(prep_icon, after_crisp, field, text)) is None

    # Negative 2: no stationary corner icon in before MUST reject even if faded.
    monkeypatch.setattr(ocr, "read_text_async", lambda frame: _async_val({
        prep_no_icon: header + [label("候选A", .20, .615)] + kb,
        after_no_icon: header + [row1, row2_mu, row3, label("候选B", .20, .615)] + kb,
    }[frame]))
    assert asyncio.run(
        paste_confirmation.verify_chat_structural_paste(prep_no_icon, after_no_icon, field, text),
    ) is None

    # Intact digits + corner_fade, and stacked corner_fade + thin CJK punct now pass ONLY when faded & blue send.
    row1_with_price = ocr.TextBox("这个89元请找群主确认，没", row1.box, row1.center, .99)
    text_with_price = "这个89元请找群主确认，没发货前一般还能协调改，具体看订单状态。"
    for after_boxes, target_text in (
        ([row1_with_price, row2_mu, row3], text_with_price),
        ([row1, row2_mu, row3_no_punct], text),
    ):
        cand_label = label("候选B", .20, .615)
        monkeypatch.setattr(
            ocr,
            "read_text_async",
            lambda frame, a=after_boxes, cl=cand_label: _async_val({
                prep_icon: header + [label("候选A", .20, .615)] + kb,
                after_faded: header + a + [cl] + kb,
            }[frame]),
        )
        assert asyncio.run(paste_confirmation.verify_chat_structural_paste(
            prep_icon, after_faded, field, target_text)) is not None
        monkeypatch.setattr(
            ocr,
            "read_text_async",
            lambda frame, a=after_boxes, cl=cand_label: _async_val({
                prep_icon: header + [label("候选A", .20, .615)] + kb,
                after_crisp: header + a + [cl] + kb,
            }[frame]),
        )
        assert asyncio.run(paste_confirmation.verify_chat_structural_paste(
            prep_icon, after_crisp, field, target_text)) is None

    # Negative 3..6: negation char, digit-adjacent fade, interior edit, non-send key.
    row2_bu = ocr.TextBox("发货前一般还能协调改，不", (.156, .514, .713, .542), (.434, .528), .98)
    row2_digit_adj = ocr.TextBox("发货前核对订单一共89件", (.156, .514, .713, .542), (.434, .528), .98)
    for after_boxes, target_text, used_kb in (
        ([row1, row2_mu, row3], text.replace("具体", "不体"), kb),
        ([row1, row2_bu, row3], text, kb),
        ([row1, row2_digit_adj, row3], "这个得赶紧找群主确认，没发货前核对订单一共89元体看订单状态。", kb),
        ([row1, row2_interior_bad, row3], text, kb),
        ([row1, row2_mu, row3], text, search_kb),
    ):
        cand_label = label("候选B", .20, .615)
        monkeypatch.setattr(
            ocr,
            "read_text_async",
            lambda frame, a=after_boxes, u_kb=used_kb, cl=cand_label: _async_val({
                prep_icon: header + [label("候选A", .20, .615)] + u_kb,
                after_faded: header + a + [cl] + u_kb,
            }[frame]),
        )
        assert asyncio.run(paste_confirmation.verify_chat_structural_paste(
            prep_icon, after_faded, field, target_text)) is None


async def _async_val(val):
    return val
