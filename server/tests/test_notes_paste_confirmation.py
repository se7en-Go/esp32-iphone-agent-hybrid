"""Synthetic Notes editor: enabling Share must be the sole toolbar change."""

import asyncio
import io

import pytest
from PIL import Image, ImageDraw
from test_paste_confirmation import label
from test_staged_clipboard import setup

from hidagent import feedback, ocr, paste_confirmation
from hidagent.clipboard_control import ConfirmPaste, PasteTransaction, PreparePaste

FIELD = (.032, .11, .96, .22)
MARKER = "Panda 74263"


def notes_frame(inserted=False, defect=None):
    image = Image.new("RGB", (500, 1080), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 634, 499, 1079), fill="#cccccc")
    draw.text((22, 657), "?" if inserted else "suggestion", fill="black")
    if inserted:
        draw.text((33, 141), MARKER, fill="black")
    color = "#d9b015" if inserted or defect == "already_enabled" else "#e1e1e1"
    x = 306 if inserted and defect == "moved" else 304
    if defect == "wrong_glyph":
        draw.ellipse((x, 72, x + 24, 102), outline=color, width=3)
    else:
        draw.line([(x + 6, 77), (x + 12, 72), (x + 18, 77)], fill=color, width=2)
        draw.line([(x + 12, 72), (x + 12, 90)], fill=color, width=2)
        draw.line([(x + 9, 80), (x + 1, 80), (x + 1, 101), (x + 23, 101),
                   (x + 23, 80), (x + 16, 80)], fill=color, width=2)
    if inserted and defect == "inner_edit":
        draw.rectangle((313, 94, 319, 97), fill="black")
    if inserted and defect == "toolbar_edit":
        draw.rectangle((375, 80, 396, 93), fill="red")
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def fixture(monkeypatch, defect=None):
    before, after = notes_frame(defect=defect), notes_frame(True, defect)
    sheet_image = Image.new("RGB", (500, 1080), "#959595")
    out = io.BytesIO()
    sheet_image.save(out, "PNG")
    sheet = out.getvalue()
    header = [label("Safari" if defect == "other_app" else "iCloud全部备忘录", .24, .0815),
              label("完成", .91, .0815), label("换行", .87, .88), label("123", .07, .88)]
    current = header + [label(MARKER, .24, .14), label("after", .4, .61)]
    if defect == "extra_text":
        current.append(label("extra", .25, .19))
    if defect == "duplicate":
        current.append(label(MARKER, .24, .19))
    if defect == "changed_page":
        current[0] = label("iCloud工作备忘录", .24, .0815)
    items = {before: header + [label("before", .3, .61)], after: current,
             sheet: [label("“备忘录”想从“快捷指令”粘贴", .5, .46),
                     label("允许粘贴", .32, .57), label("不允许粘贴", .70, .57)]}

    async def read(frame):
        return items[frame]

    monkeypatch.setattr(ocr, "read_text_async", read)
    return before, sheet, after


@pytest.mark.parametrize("defect", [
    None, "wrong_glyph", "moved", "inner_edit", "toolbar_edit", "other_app",
    "extra_text", "duplicate", "already_enabled", "changed_page",
])
def test_only_matching_notes_share_transition_can_confirm_without_sheet(monkeypatch, defect):
    before, _, after = fixture(monkeypatch, defect)
    result = asyncio.run(paste_confirmation.verify_before_after_for_permission(
        before, after, FIELD, MARKER, notes_only=True))
    assert result is (defect is None)


@pytest.mark.parametrize("include_sheet", [True, False])
def test_confirm_records_only_one_notes_paste_with_or_without_permission(monkeypatch, include_sheet):
    async def scenario():
        before, sheet, after = fixture(monkeypatch)
        post_command = sheet if include_sheet else after
        monkeypatch.setattr("test_staged_clipboard.field_image", lambda text=False: post_command if text else before)
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(PreparePaste(text=MARKER, focused=True, target_box=FIELD,
                                                       screen_id=control.observation()["screen_id"]))
        transaction = prepared["transaction_id"]
        token = provider.pending()["text_url"].rsplit("/", 1)[1]
        assert provider.fetch(token) == MARKER
        provider.acknowledge(token, copied=True, focused=False)
        screen._on_jpeg(before)

        async def effect(_screen, _since):
            screen._on_jpeg(post_command)
            return post_command

        monkeypatch.setattr(feedback, "wait_after_action", effect)
        await manager.paste(PasteTransaction(transaction_id=transaction, focused=True,
                                             screen_id=control.observation()["screen_id"]))
        screen._on_jpeg(after)
        report = await manager.confirm(ConfirmPaste(transaction_id=transaction, confirmed=True,
                                                   observed_text=MARKER, screen_id=control.observation()["screen_id"]))
        assert report["confirmation_evidence"] == (
            "paste_permission_dismissed" if include_sheet else "notes_share_enabled")
        assert report["effect_verified"] and report["phase"] == "completed"
        assert phone.sent.count("cmd+v") == 1

    asyncio.run(scenario())
