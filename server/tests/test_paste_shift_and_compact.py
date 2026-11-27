"""Tests for English Shift auto-cap transition and 9:16 compact portrait confirmation."""

import asyncio
import io

import pytest
from PIL import Image, ImageDraw

from hidagent import ocr, screen_compare
from hidagent.paste_confirmation import verify_chat_structural_paste, verify_direct_paste


def _tb(text: str, box: tuple[float, float, float, float], conf: float = .99) -> ocr.TextBox:
    return ocr.TextBox(text, box, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), conf)


def _encode_png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def _draw_qwerty_rows(
    draw: ImageDraw.ImageDraw,
    w: int,
    h: int,
    *,
    shift_on: bool,
    lowercase: bool,
    tamper: str | None = None,
) -> tuple[list[ocr.TextBox], list[tuple[int, int, int, int]]]:
    row_ys = ((round(.652 * h), round(.703 * h)),
              (round(.719 * h), round(.770 * h)),
              (round(.785 * h), round(.836 * h)))
    rows_letters = ("QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM")
    key_w = max(24, round(.080 * w))
    step_x = round(.100 * w)
    boxes: list[ocr.TextBox] = []
    letter_rects: list[tuple[int, int, int, int]] = []

    for r_idx, (y0, y1) in enumerate(row_ys):
        letters = rows_letters[r_idx]
        if r_idx == 0:
            x_start = round(.010 * w)
        elif r_idx == 1:
            x_start = round(.056 * w)
        else:
            x_start = round(.158 * w)
            sx0, sx1 = round(.012 * w), round(.114 * w)
            if shift_on:
                draw.rounded_rectangle((sx0, y0, sx1, y1), radius=6, fill=(255, 255, 255))
                mid = (sx0 + sx1) // 2
                draw.rectangle((mid - 6, y0 + 18, mid + 6, y1 - 12), fill=(15, 15, 15))
                draw.polygon([(mid, y0 + 10), (mid - 12, y0 + 24), (mid + 12, y0 + 24)], fill=(15, 15, 15))
            else:
                draw.rounded_rectangle((sx0, y0, sx1, y1), radius=6, fill=(176, 178, 188))
                mid = (sx0 + sx1) // 2
                draw.rectangle((mid - 5, y0 + 20, mid + 5, y1 - 14), outline=(45, 45, 48), width=2)
            bx0, bx1 = round(.882 * w), round(.988 * w)
            draw.rounded_rectangle((bx0, y0, bx1, y1), radius=6, fill=(176, 178, 188))
            if tamper == "backspace_edit":
                draw.rectangle((bx0 + 6, y0 + 8, bx1 - 6, y1 - 8), fill=(18, 132, 255))

        for c_idx, ch in enumerate(letters):
            kx0 = x_start + c_idx * step_x
            kx1 = kx0 + key_w
            draw.rounded_rectangle((kx0, y0, kx1, y1), radius=6, fill=(255, 255, 255))
            letter_rects.append((kx0, y0, kx1, y1))
            cx, cy = (kx0 + kx1) // 2, (y0 + y1) // 2
            if lowercase:
                draw.rectangle((cx - 5, cy - 3, cx + 5, cy + 7), fill=(32, 32, 35))
                shown = ch.lower()
            else:
                draw.rectangle((cx - 7, cy - 9, cx + 7, cy + 7), fill=(25, 25, 28))
                shown = ch
            boxes.append(_tb(shown, ((cx - 10) / w, (cy - 12) / h, (cx + 10) / w, (cy + 12) / h), conf=.98))

    if tamper == "blue_keycap":
        kx0, ky0, kx1, ky1 = letter_rects[0]
        draw.rectangle((kx0 + 3, ky0 + 6, kx1 - 3, ky1 - 6), fill=(18, 132, 255))
    elif tamper == "dark_keycap":
        kx0, ky0, kx1, ky1 = letter_rects[0]
        draw.rectangle((kx0 + 2, ky0 + 2, kx1 - 2, ky1 - 2), fill=(35, 35, 35))
    elif tamper == "shift_stays_grey":
        kx0, ky0, kx1, ky1 = letter_rects[0]
        draw.rectangle((kx0 + 6, ky0 + 10, kx1 - 6, ky0 + 18), fill=(30, 30, 32))
    elif tamper == "gutter_dot":
        gy0, gy1 = row_ys[0]
        gx0 = letter_rects[0][2] + 1
        draw.rectangle((gx0, gy0 + 10, gx0 + 7, gy1 - 10), fill=(20, 180, 90))
    return boxes, letter_rects


def _build_shift_pair(
    *,
    grown: bool = True,
    key_blue: bool = True,
    after_ocr_lines: list[ocr.TextBox] | None = None,
    tamper: str | None = None,
):
    w, h = 500, 1080
    field = (.132, .520, .764, .576)
    sbar_top = round(.586 * h)

    def make_one(*, pasted: bool, shift_on: bool, lowercase: bool, blue_btn: bool, t_flag: str | None):
        img = Image.new("RGB", (w, h), (237, 237, 237))
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, w, round(.105 * h)), fill=(237, 237, 237))
        # Static incoming bubble in viewport above field
        draw.rounded_rectangle((70, 220, 380, 290), radius=10, fill=(255, 255, 255))
        draw.rectangle((90, 240, 280, 252), fill=(40, 40, 45))

        top_px = round((.460 if (pasted and grown) else .520) * h)
        bot_px = round(.576 * h)
        draw.rounded_rectangle((round(.132 * w), top_px, round(.764 * w), bot_px), radius=8, fill=(255, 255, 255))
        if pasted:
            draw.rectangle((round(.17 * w), top_px + 14, round(.68 * w), top_px + 26), fill=(30, 30, 32))
            if grown:
                draw.rectangle((round(.17 * w), top_px + 54, round(.62 * w), top_px + 66), fill=(30, 30, 32))
        draw.rectangle((0, sbar_top, w, h), fill=(212, 215, 221))
        kb_boxes, _ = _draw_qwerty_rows(draw, w, h, shift_on=shift_on, lowercase=lowercase, tamper=t_flag)
        # Bottom row: 123, space, send
        draw.rounded_rectangle((round(.01 * w), round(.855 * h), round(.12 * w), round(.905 * h)), radius=6,
                               fill=(176, 178, 188))
        draw.rounded_rectangle((round(.25 * w), round(.855 * h), round(.73 * w), round(.905 * h)), radius=6,
                               fill=(255, 255, 255))
        send_fill = (10, 132, 255) if blue_btn else (176, 178, 188)
        draw.rounded_rectangle((round(.75 * w), round(.855 * h), round(.99 * w), round(.905 * h)), radius=6,
                               fill=send_fill)
        return _encode_png(img), kb_boxes

    before_bytes, b_kb = make_one(pasted=False, shift_on=(tamper != "shift_stays_grey"),
                                  lowercase=(tamper == "shift_stays_grey"), blue_btn=False, t_flag=None)
    after_bytes, a_kb = make_one(pasted=True, shift_on=False, lowercase=True, blue_btn=key_blue, t_flag=tamper)

    header = [_tb("测试群(10)", (.20, .068, .75, .095))]
    vp = [_tb("你好请查收", (.20, .225, .42, .250))]
    lower = [
        _tb("123", (.03, .866, .10, .890)),
        _tb("space", (.44, .866, .56, .890)),
        _tb("send", (.82, .866, .93, .890)),
    ]
    if tamper == "fewer_than_3_ocr_flips":
        b_kb = b_kb[:2]

    before_ocr = header + vp + b_kb + lower
    if after_ocr_lines is None:
        if grown:
            after_ocr_lines = [
                _tb("小程序首页往下翻，找到", (.16, .470, .68, .492)),
                _tb("「团购」或「活动」这一栏确认。", (.16, .510, .70, .534)),
            ]
        else:
            after_ocr_lines = [_tb("简单回复123", (.16, .535, .45, .560))]
    after_ocr = header + vp + after_ocr_lines + a_kb + lower
    return before_bytes, after_bytes, before_ocr, after_ocr, field


def test_shift_on_to_off_chat_structural_and_bracket_equivalence(monkeypatch):
    text = "小程序首页往下翻，找到「团购」或「活动」这一栏确认。"
    # Exact match + Shift ON -> OFF
    b, a, bo, ao, field = _build_shift_pair(grown=True, key_blue=True)
    monkeypatch.setattr(ocr, "read_text_async", lambda img: asyncio.sleep(0, result={b: bo, a: ao}[img]))
    res = asyncio.run(verify_chat_structural_paste(b, a, field, text))
    assert res is not None and res[0] == "direct_paste_verified"

    # RapidOCR misreading closing corner bracket '」' as halfwidth ']' must confirm when send turns blue.
    b2, a2, bo2, ao2, field = _build_shift_pair(
        grown=True,
        key_blue=True,
        after_ocr_lines=[
            _tb("小程序首页往下翻，找到", (.16, .470, .68, .492)),
            _tb("「团购]或「活动」这一栏确认。", (.16, .510, .70, .534)),
        ],
    )
    monkeypatch.setattr(ocr, "read_text_async", lambda img: asyncio.sleep(0, result={b2: bo2, a2: ao2}[img]))
    res2 = asyncio.run(verify_chat_structural_paste(b2, a2, field, text))
    assert res2 is not None and res2[0] == "direct_paste_verified"

    # Reversed bracket direction '[' for expected '」' must stay rejected.
    b3, a3, bo3, ao3, field = _build_shift_pair(
        grown=True,
        key_blue=True,
        after_ocr_lines=[
            _tb("小程序首页往下翻，找到", (.16, .470, .68, .492)),
            _tb("「团购[或「活动」这一栏确认。", (.16, .510, .70, .534)),
        ],
    )
    monkeypatch.setattr(ocr, "read_text_async", lambda img: asyncio.sleep(0, result={b3: bo3, a3: ao3}[img]))
    assert asyncio.run(verify_chat_structural_paste(b3, a3, field, text)) is None

    # Reverse direction: expected ASCII '[1]' misread as '「1」' must stay rejected.
    b4, a4, bo4, ao4, field = _build_shift_pair(
        grown=True,
        key_blue=True,
        after_ocr_lines=[
            _tb("请按页面指引下单确认序号", (.16, .470, .68, .492)),
            _tb("「1」并核对运费说明再下单。", (.16, .510, .70, .534)),
        ],
    )
    monkeypatch.setattr(ocr, "read_text_async", lambda img: asyncio.sleep(0, result={b4: bo4, a4: ao4}[img]))
    expected_ascii = "请按页面指引下单确认序号[1]并核对运费说明再下单。"
    assert asyncio.run(verify_chat_structural_paste(b4, a4, field, expected_ascii)) is None


def test_shift_on_to_off_single_line_direct_paste(monkeypatch):
    b, a, bo, ao, field = _build_shift_pair(grown=False, key_blue=True)
    monkeypatch.setattr(ocr, "read_text_async", lambda img: asyncio.sleep(0, result={b: bo, a: ao}[img]))
    assert asyncio.run(verify_direct_paste(b, a, field, "简单回复123")) == "direct_paste_verified"


@pytest.mark.parametrize("tamper", [
    "blue_keycap",
    "dark_keycap",
    "gutter_dot",
    "backspace_edit",
    "shift_stays_grey",
    "fewer_than_3_ocr_flips",
])
def test_shift_on_to_off_rejects_tamper_and_weak_evidence(monkeypatch, tamper):
    text = "小程序首页往下翻，找到「团购」或「活动」这一栏确认。"
    b, a, bo, ao, field = _build_shift_pair(grown=True, key_blue=True, tamper=tamper)
    monkeypatch.setattr(ocr, "read_text_async", lambda img: asyncio.sleep(0, result={b: bo, a: ao}[img]))
    assert asyncio.run(verify_chat_structural_paste(b, a, field, text)) is None
    assert asyncio.run(verify_direct_paste(b, a, field, text)) is None


def test_compact_portrait_9_16_verifies_composer_and_guards_header(monkeypatch):
    assert screen_compare.is_compact_portrait_size((608, 1080)) is True
    assert screen_compare.status_height_for_size((608, 1080)) == pytest.approx(0.036)
    assert screen_compare.is_compact_portrait_size((500, 1080)) is False
    assert screen_compare.status_height_for_size((500, 1080)) == pytest.approx(0.06)

    w, h = 608, 1080
    field = (.118, .556, .786, .622)

    def make_compact(*, pasted: bool, header_tamper: bool = False):
        img = Image.new("RGB", (w, h), (237, 237, 237))
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, w, round(.090 * h)), fill=(237, 237, 237))
        if header_tamper:
            draw.rectangle((round(.40 * w), round(.045 * h), round(.56 * w), round(.070 * h)), fill=(10, 132, 255))
        draw.rounded_rectangle((70, 220, 420, 310), radius=10, fill=(255, 255, 255))
        draw.rectangle((100, 250, 320, 265), fill=(40, 40, 45))
        draw.rounded_rectangle(
            (round(.118 * w), round(.556 * h), round(.786 * w), round(.622 * h)), radius=8, fill=(255, 255, 255))
        if pasted:
            draw.rectangle((round(.16 * w), round(.575 * h), round(.50 * w), round(.595 * h)), fill=(30, 30, 32))
        draw.rectangle((0, round(.634 * h), w, h), fill=(212, 215, 221))
        draw.rounded_rectangle(
            (round(.750 * w), round(.932 * h), round(.990 * w), round(.988 * h)),
            radius=8,
            fill=(10, 132, 255) if pasted else (176, 178, 188),
        )
        return _encode_png(img)

    b = make_compact(pasted=False)
    a = make_compact(pasted=True)
    a_tamper = make_compact(pasted=True, header_tamper=True)

    # Touch ID header sits at center_y=0.057 (< 0.06 Face ID cutoff), send sits at y=[0.950, 0.978] (> 0.94).
    header = [_tb("文件传输助手", (.351, .041, .649, .073))]
    vp = [_tb("历史消息", (.20, .230, .42, .258))]
    lower = [
        _tb("123", (.030, .952, .105, .977)),
        _tb("space", (.498, .955, .611, .978)),
        _tb("send", (.827, .952, .923, .977)),
    ]
    bo = header + vp + lower
    ao = header + vp + [_tb("客户机测试成功123", (.16, .568, .50, .602))] + lower

    monkeypatch.setattr(
        ocr, "read_text_async", lambda img: asyncio.sleep(0, result={b: bo, a: ao, a_tamper: ao}[img]))
    assert asyncio.run(verify_chat_structural_paste(b, a, field, "客户机测试成功123"))[0] == "direct_paste_verified"
    assert asyncio.run(verify_direct_paste(b, a, field, "客户机测试成功123")) == "direct_paste_verified"
    assert asyncio.run(verify_chat_structural_paste(b, a_tamper, field, "客户机测试成功123")) is None
    assert asyncio.run(verify_direct_paste(b, a_tamper, field, "客户机测试成功123")) is None
