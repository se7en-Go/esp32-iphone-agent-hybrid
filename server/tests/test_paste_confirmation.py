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
