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
