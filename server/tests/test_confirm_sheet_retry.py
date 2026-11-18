"""Confirm retries the permission-sheet fallback with recent frames.

The first post-paste frame can miss the sheet entirely (it appears after
cmd+v, or was already dismissed), so a single verify attempt with it fails
every retry. The fix retries with the most recent frames that could still
show the sheet.
"""

import asyncio
import io

import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw
from test_staged_clipboard import copied, field_image, prepare_body, setup

from hidagent.clipboard_control import ConfirmPaste, PasteTransaction


def changed_scene(color):
    with Image.open(io.BytesIO(field_image(text=True))) as image:
        ImageDraw.Draw(image).rectangle((50, 80, 350, 155), fill=color)
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue()


def test_confirm_retries_permission_sheet_from_recent_frames(monkeypatch):
    from hidagent import feedback, paste_confirmation

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        before, sheet, after = field_image(), changed_scene("blue"), changed_scene("orange")
        calls = []

        async def capture(_screen, _completed_at):
            # The first post-paste frame missed the sheet: it is the plain field.
            return before

        async def verify(original, presented, selected, region, text):
            calls.append(presented)
            return presented == sheet

        monkeypatch.setattr(feedback, "wait_after_action", capture)
        monkeypatch.setattr(paste_confirmation, "verify_after_paste_permission", verify)
        prepared = await manager.prepare(prepare_body(control))
        copied(provider, screen)
        await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                             screen_id=control.observation()["screen_id"], focused=True))
        screen._on_jpeg(sheet)  # sheet visible briefly after paste
        screen._on_jpeg(after)  # then the pasted field
        receipt = await manager.confirm(ConfirmPaste(
            transaction_id=prepared["transaction_id"],
            screen_id=control.observation()["screen_id"],
            observed_text="你好00123", confirmed=True))
        assert receipt["phase"] == "completed" and receipt["effect_verified"]
        assert receipt["confirmation_evidence"] == "paste_permission_dismissed"
        # Only the in-window sheet frame was tried; the first post-paste frame
        # (a plain field) is no longer a candidate, and the evidence carries
        # the sheet's own timestamp and session generation.
        assert calls == [sheet]
        sheet_snapshot = next(s for s in screen.recent_frames if s.frame == sheet)
        assert receipt["sheet_frame_updated"] == sheet_snapshot.updated
        assert receipt["sheet_frame_generation"] == sheet_snapshot.generation
        assert provider.current() == {"active": False, "needs_resolution": False}

    asyncio.run(scenario())


def test_confirm_ignores_sheet_from_before_this_paste(monkeypatch):
    """A stale sheet from an earlier run must never verify a new transaction.

    The old sheet sits in recent_frames, but it predates this Cmd+V, so the
    window filter excludes it and confirm refuses even though the (mocked)
    verifier would accept that exact frame.
    """
    from hidagent import feedback, paste_confirmation

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        old_sheet = changed_scene("blue")
        screen._on_jpeg(old_sheet)  # published before this transaction's Cmd+V

        async def capture(_screen, _completed_at):
            return screen.frame

        calls = []

        async def verify(original, presented, selected, region, text):
            calls.append(presented)
            return presented == old_sheet

        monkeypatch.setattr(feedback, "wait_after_action", capture)
        monkeypatch.setattr(paste_confirmation, "verify_after_paste_permission", verify)
        prepared = await manager.prepare(prepare_body(control))
        copied(provider, screen)
        await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                             screen_id=control.observation()["screen_id"], focused=True))
        screen._on_jpeg(changed_scene("orange"))  # result frame; the guard rejects it
        with pytest.raises(RuntimeError):
            await manager.confirm(ConfirmPaste(
                transaction_id=prepared["transaction_id"],
                screen_id=control.observation()["screen_id"],
                observed_text="你好00123", confirmed=True))
        assert old_sheet not in calls
        assert provider.current()["active"]

    asyncio.run(scenario())


def test_confirm_finds_sheet_buried_under_later_frames(monkeypatch):
    """A valid sheet must still be found after many ordinary frames arrive.

    The old candidate list capped at the 4 most recent frames, so a sheet
    followed by 6 ordinary frames was pushed out and confirm refused. The
    window scan tries every same-session frame after Cmd+V.
    """
    from hidagent import feedback, paste_confirmation

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        before, sheet, after = field_image(), changed_scene("blue"), changed_scene("orange")
        calls = []

        async def capture(_screen, _completed_at):
            return before

        async def verify(original, presented, selected, region, text):
            calls.append(presented)
            return presented == sheet

        monkeypatch.setattr(feedback, "wait_after_action", capture)
        monkeypatch.setattr(paste_confirmation, "verify_after_paste_permission", verify)
        prepared = await manager.prepare(prepare_body(control))
        copied(provider, screen)
        await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                             screen_id=control.observation()["screen_id"], focused=True))
        screen._on_jpeg(sheet)  # sheet visible briefly after paste
        for color in ("red", "green", "purple", "yellow", "cyan", "magenta"):
            screen._on_jpeg(changed_scene(color))  # ordinary frames after the sheet
        screen._on_jpeg(after)  # then the pasted field
        receipt = await manager.confirm(ConfirmPaste(
            transaction_id=prepared["transaction_id"],
            screen_id=control.observation()["screen_id"],
            observed_text="你好00123", confirmed=True))
        assert receipt["phase"] == "completed" and receipt["effect_verified"]
        assert receipt["confirmation_evidence"] == "paste_permission_dismissed"
        assert calls == [sheet]
        assert provider.current() == {"active": False, "needs_resolution": False}

    asyncio.run(scenario())


def test_confirm_without_any_sheet_frame_still_refuses(monkeypatch):
    from hidagent import feedback, paste_confirmation

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        before, after = field_image(), changed_scene("orange")

        async def capture(_screen, _completed_at):
            return before

        async def verify(original, presented, selected, region, text):
            return False

        monkeypatch.setattr(feedback, "wait_after_action", capture)
        monkeypatch.setattr(paste_confirmation, "verify_after_paste_permission", verify)
        prepared = await manager.prepare(prepare_body(control))
        copied(provider, screen)
        await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                             screen_id=control.observation()["screen_id"], focused=True))
        screen._on_jpeg(after)
        with pytest.raises(RuntimeError):
            await manager.confirm(ConfirmPaste(
                transaction_id=prepared["transaction_id"],
                screen_id=control.observation()["screen_id"],
                observed_text="你好00123", confirmed=True))
        assert provider.current()["active"]

    asyncio.run(scenario())


def test_confirm_uses_sheet_retained_at_paste_time_after_window_passes(monkeypatch):
    """A sheet that left the 4 s recent window before confirm still verifies.

    paste() retains the frames observed right after Cmd+V in the
    transaction; confirm falls back to them when the live window no longer
    holds the sheet.
    """
    from hidagent import feedback, paste_confirmation

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        _, sheet, after = field_image(), changed_scene("blue"), changed_scene("orange")
        calls = []

        async def capture(_screen, _completed_at):
            # The sheet arrives while paste() waits for post-action evidence,
            # so it is part of the frames retained at paste time.
            screen._on_jpeg(sheet)
            return sheet

        async def verify(original, presented, selected, region, text):
            calls.append(presented)
            return presented == sheet

        monkeypatch.setattr(feedback, "wait_after_action", capture)
        monkeypatch.setattr(paste_confirmation, "verify_after_paste_permission", verify)
        prepared = await manager.prepare(prepare_body(control))
        copied(provider, screen)
        await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                             screen_id=control.observation()["screen_id"], focused=True))
        # The 4 s recent window has passed: the live history no longer holds
        # the sheet, but the transaction retained it at paste time.
        screen._recent_frames.clear()
        screen._on_jpeg(after)  # then the pasted field
        receipt = await manager.confirm(ConfirmPaste(
            transaction_id=prepared["transaction_id"],
            screen_id=control.observation()["screen_id"],
            observed_text="你好00123", confirmed=True))
        assert receipt["phase"] == "completed" and receipt["effect_verified"]
        assert receipt["confirmation_evidence"] == "paste_permission_dismissed"
        assert calls == [sheet]
        assert provider.current() == {"active": False, "needs_resolution": False}

    asyncio.run(scenario())


def test_confirm_reuses_verified_sheet_on_retry(monkeypatch):
    """A sheet verified once stays usable for a later confirm retry.

    The first confirm verifies the sheet but fails to finish (stage_finish
    raises); the retry reuses the retained verified evidence even though
    the live recent window no longer holds the sheet.
    """
    from hidagent import feedback, paste_confirmation

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        _, sheet, after = field_image(), changed_scene("blue"), changed_scene("orange")
        calls = []

        async def capture(_screen, _completed_at):
            screen._on_jpeg(sheet)
            return sheet

        async def verify(original, presented, selected, region, text):
            calls.append(presented)
            return presented == sheet

        monkeypatch.setattr(feedback, "wait_after_action", capture)
        monkeypatch.setattr(paste_confirmation, "verify_after_paste_permission", verify)
        prepared = await manager.prepare(prepare_body(control))
        copied(provider, screen)
        await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                             screen_id=control.observation()["screen_id"], focused=True))
        finish = provider.stage_finish
        attempts = []

        def flaky_finish(nonce, **kwargs):
            attempts.append(nonce)
            if len(attempts) == 1:
                raise RuntimeError("transient finish failure")
            return finish(nonce, **kwargs)

        monkeypatch.setattr(provider, "stage_finish", flaky_finish)
        screen._on_jpeg(after)
        with pytest.raises(RuntimeError, match="transient finish failure"):
            await manager.confirm(ConfirmPaste(
                transaction_id=prepared["transaction_id"],
                screen_id=control.observation()["screen_id"],
                observed_text="你好00123", confirmed=True))
        assert calls == [sheet]
        assert provider.current()["active"]
        # The window has passed and the live history no longer holds the
        # sheet; the retry reuses the evidence verified in the first attempt.
        screen._recent_frames.clear()
        screen._on_jpeg(changed_scene("orange"))
        receipt = await manager.confirm(ConfirmPaste(
            transaction_id=prepared["transaction_id"],
            screen_id=control.observation()["screen_id"],
            observed_text="你好00123", confirmed=True))
        assert receipt["phase"] == "completed" and receipt["effect_verified"]
        assert receipt["confirmation_evidence"] == "paste_permission_dismissed"
        assert calls == [sheet, sheet]
        assert provider.current() == {"active": False, "needs_resolution": False}

    asyncio.run(scenario())


def test_confirm_accepts_newline_leading_append_with_retained_sheet(monkeypatch):
    """Notes append: newline-leading Chinese text, verified in one transaction.

    The appended text starts with a newline (no OCR row for the blank line),
    then Chinese words, a leading-zero number and mixed case. The permission
    sheet left the 4 s recent window before confirm runs, so the only sheet
    candidate is the frame paste() retained at paste time. The dialog wording
    is the Notes one, not Safari's; the Safari wording stays covered by
    test_paste_confirmation. The real verifier runs here, not a mock: the
    transaction digest pins the exact bytes (leading newline, case) while
    the OCR check covers the visible lines.
    """
    import io

    from PIL import Image, ImageDraw
    from test_paste_confirmation import REGION, label

    from hidagent import feedback, ocr
    from hidagent.clipboard_control import PreparePaste

    NEW_TEXT = "\n秋天到了00731\n第二行 AbD eFg"

    def borderless_frame(bars, sheet=False, outside_change=False):
        image = Image.new("RGB", (500, 1000), (235, 235, 230))
        draw = ImageDraw.Draw(image)
        for y in bars:
            draw.rectangle((116, y - 2, 230, y + 2), fill="black")
        if sheet:
            draw.rounded_rectangle((90, 350, 410, 650), radius=20, fill=(180, 180, 180))
        if outside_change:
            # Outside the input region: trips the accept-stage guard into the
            # sheet fallback without moving any OCR anchor.
            draw.rectangle((50, 80, 350, 155), fill="orange")
        result = io.BytesIO()
        image.save(result, "PNG")
        return result.getvalue()

    existing = [label("已有第一行", .25, .36), label("已有第二行", .25, .40)]
    pasted = [label("秋天到了00731", .25, .44), label("第二行 AbD eFg", .25, .48)]
    context = [label("Safari test page", .25, .13), label("Tester", .25, .25), label("Expected text", .23, .65),
               label("页面标题", .25, .72), label("页面副标题", .25, .76)]
    dialog = [label("“备忘录”想从“快捷指令”粘贴", .5, .39), label("你允许这样做吗？", .5, .44),
              label("不允许粘贴", .27, .52), label("允许粘贴", .7, .52)]
    original = borderless_frame([360, 400])
    sheet = borderless_frame([360, 400], sheet=True)
    after = borderless_frame([360, 400, 440, 480], outside_change=True)
    frames = {original: context + existing, sheet: dialog, after: context + existing + pasted}

    async def read(image):
        return frames.get(image, [])

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        # The guard's original observation must be this frame, not the
        # 400x800 fixture setup() publishes first.
        screen._on_jpeg(original)
        monkeypatch.setattr(ocr, "read_text_async", read)

        async def capture(_screen, _completed_at):
            # The sheet arrives while paste() waits for post-action evidence,
            # so it is part of the frames retained at paste time.
            screen._on_jpeg(sheet)
            return sheet

        monkeypatch.setattr(feedback, "wait_after_action", capture)
        prepared = await manager.prepare(PreparePaste(
            text=NEW_TEXT, screen_id=control.observation()["screen_id"],
            focused=True, target_box=REGION))
        # Claim the staged payload the way the Shortcut does, then ack.
        pending = provider.pending()
        assert pending["protocol"] == "staged-v1"
        token = pending["text_url"].rsplit("/", 1)[-1]
        assert provider.fetch_payload(token) == {"copy_allowed": True, "text": NEW_TEXT}
        provider.acknowledge(token, copied=True, focused=False)
        screen._on_jpeg(screen.frame)
        await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                             screen_id=control.observation()["screen_id"], focused=True))
        # The 4 s recent window has passed: the live history no longer holds
        # the sheet, but the transaction retained it at paste time.
        screen._recent_frames.clear()
        screen._on_jpeg(after)
        receipt = await manager.confirm(ConfirmPaste(
            transaction_id=prepared["transaction_id"],
            screen_id=control.observation()["screen_id"],
            observed_text=NEW_TEXT, confirmed=True))
        assert receipt["phase"] == "completed" and receipt["effect_verified"]
        assert receipt["confirmation_evidence"] == "paste_permission_dismissed"
        assert receipt["sheet_frame_updated"] is not None
        assert receipt["sheet_frame_generation"] is not None
        assert provider.current() == {"active": False, "needs_resolution": False}

    asyncio.run(scenario())


def _suggestion_bar_scenario_frames(*, outside_ocr_change=False, outside_visual_change=False):
    from test_paste_confirmation import _kb_context_boxes, _kb_suggestion_boxes, keyboard_frame, label

    new_text = "\n修验通过：云海00417\n末行 GhJ kLm"
    existing = [label("已有第一行", .25, .20), label("第二行 AbD eFg", .25, .25)]
    pasted = [label("修验通过：云海00417", .25, .30), label("末行 GhJ kLm", .25, .35)]
    dialog = [label("“备忘录”想从“快捷指令”粘贴", .5, .44), label("你允许这样做吗？", .5, .48),
              label("不允许粘贴", .27, .55), label("允许粘贴", .7, .55)]
    original = keyboard_frame([200, 250], suggestion_variant=0)
    sheet = keyboard_frame([200, 250], suggestion_variant=0, sheet=True)
    after = keyboard_frame([200, 250, 300, 350], suggestion_variant=1,
                           header_tamper=outside_visual_change)
    after_ctx = _kb_context_boxes(
        nav_top_label="群聊" if outside_ocr_change else "返回",
    )
    frames = {
        original: _kb_context_boxes() + _kb_suggestion_boxes(0) + existing,
        sheet: dialog,
        after: after_ctx + _kb_suggestion_boxes(1) + existing + pasted,
    }
    return new_text, original, sheet, after, frames


def _claim_and_copy(provider, screen, expected_text):
    pending = provider.pending()
    assert pending["protocol"] == "staged-v1"
    token = pending["text_url"].rsplit("/", 1)[-1]
    assert provider.fetch_payload(token) == {"copy_allowed": True, "text": expected_text}
    provider.acknowledge(token, copied=True, focused=False)
    screen._on_jpeg(screen.frame)


def test_confirm_completes_when_only_suggestion_bar_changes_after_sheet(monkeypatch):
    from test_paste_confirmation import KB_REGION

    from hidagent import feedback, ocr
    from hidagent.clipboard_control import PreparePaste

    new_text, original, sheet, after, frames = _suggestion_bar_scenario_frames()

    async def read(image):
        return frames.get(image, [])

    async def scenario():
        screen, provider, _phone, control, manager = setup(monkeypatch)
        screen._on_jpeg(original)
        monkeypatch.setattr(ocr, "read_text_async", read)

        async def capture(_screen, _completed_at):
            screen._on_jpeg(sheet)
            return sheet

        monkeypatch.setattr(feedback, "wait_after_action", capture)
        prepared = await manager.prepare(PreparePaste(
            text=new_text, screen_id=control.observation()["screen_id"],
            focused=True, target_box=KB_REGION))
        _claim_and_copy(provider, screen, new_text)
        await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                             screen_id=control.observation()["screen_id"], focused=True))
        screen._on_jpeg(after)
        receipt = await manager.confirm(ConfirmPaste(
            transaction_id=prepared["transaction_id"],
            screen_id=control.observation()["screen_id"],
            observed_text=new_text, confirmed=True))
        assert receipt["phase"] == "completed" and receipt["effect_verified"] is True
        assert receipt["confirmation_evidence"] == "paste_permission_dismissed"
        assert provider.current() == {"active": False, "needs_resolution": False}

    asyncio.run(scenario())


@pytest.mark.parametrize("case", [
    "missing_sheet",
    "pre_paste_sheet",
    "other_transaction_sheet",
    "new_mirror_generation",
])
def test_confirm_refuses_suggestion_bar_change_when_sheet_missing_expired_or_other_tx(monkeypatch, case):
    from test_paste_confirmation import KB_REGION

    from hidagent import feedback, ocr
    from hidagent.clipboard_control import PreparePaste

    new_text, original, sheet, after, frames = _suggestion_bar_scenario_frames()

    async def read(image):
        return frames.get(image, [])

    async def scenario():
        screen, provider, _phone, control, manager = setup(monkeypatch)
        monkeypatch.setattr(ocr, "read_text_async", read)

        if case == "other_transaction_sheet":
            screen._on_jpeg(original)

            async def capture_sheet(_screen, _completed_at):
                screen._on_jpeg(sheet)
                return sheet

            monkeypatch.setattr(feedback, "wait_after_action", capture_sheet)
            first = await manager.prepare(PreparePaste(
                text=new_text, screen_id=control.observation()["screen_id"],
                focused=True, target_box=KB_REGION))
            _claim_and_copy(provider, screen, new_text)
            await manager.paste(PasteTransaction(transaction_id=first["transaction_id"],
                                                 screen_id=control.observation()["screen_id"], focused=True))
            manager.cancel(first["transaction_id"])
            provider.resolve(paste_finished=True)

        screen._on_jpeg(original)
        if case == "pre_paste_sheet":
            screen._on_jpeg(sheet)
            screen._on_jpeg(original)

        async def capture_plain(_screen, _completed_at):
            if case == "new_mirror_generation":
                screen._on_jpeg(sheet)
                return sheet
            screen._on_jpeg(original)
            return original

        monkeypatch.setattr(feedback, "wait_after_action", capture_plain)
        prepared = await manager.prepare(PreparePaste(
            text=new_text, screen_id=control.observation()["screen_id"],
            focused=True, target_box=KB_REGION))
        _claim_and_copy(provider, screen, new_text)
        await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                             screen_id=control.observation()["screen_id"], focused=True))
        if case == "new_mirror_generation":
            screen._on_ready()
        screen._on_jpeg(after)
        expected_exc = HTTPException if case == "new_mirror_generation" else RuntimeError
        with pytest.raises(expected_exc):
            await manager.confirm(ConfirmPaste(
                transaction_id=prepared["transaction_id"],
                screen_id=control.observation()["screen_id"],
                observed_text=new_text, confirmed=True))
        assert provider.current()["active"] is (case != "new_mirror_generation")

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["outside_ocr_label", "outside_visual_banner"])
def test_confirm_refuses_when_outside_ui_changes_alongside_suggestion_bar(monkeypatch, case):
    from test_paste_confirmation import KB_REGION

    from hidagent import feedback, ocr
    from hidagent.clipboard_control import PreparePaste

    new_text, original, sheet, after, frames = _suggestion_bar_scenario_frames(
        outside_ocr_change=(case == "outside_ocr_label"),
        outside_visual_change=(case == "outside_visual_banner"),
    )

    async def read(image):
        return frames.get(image, [])

    async def scenario():
        screen, provider, _phone, control, manager = setup(monkeypatch)
        screen._on_jpeg(original)
        monkeypatch.setattr(ocr, "read_text_async", read)

        async def capture(_screen, _completed_at):
            screen._on_jpeg(sheet)
            return sheet

        monkeypatch.setattr(feedback, "wait_after_action", capture)
        prepared = await manager.prepare(PreparePaste(
            text=new_text, screen_id=control.observation()["screen_id"],
            focused=True, target_box=KB_REGION))
        _claim_and_copy(provider, screen, new_text)
        await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                             screen_id=control.observation()["screen_id"], focused=True))
        screen._on_jpeg(after)
        with pytest.raises(RuntimeError):
            await manager.confirm(ConfirmPaste(
                transaction_id=prepared["transaction_id"],
                screen_id=control.observation()["screen_id"],
                observed_text=new_text, confirmed=True))
        assert provider.current()["active"]

    asyncio.run(scenario())


def test_confirm_skips_pre_sheet_plain_frames_and_checks_latest_retained_first(monkeypatch):
    from hidagent import feedback, paste_confirmation

    async def scenario():
        screen, provider, _phone, control, manager = setup(monkeypatch)
        before, sheet, after = field_image(), changed_scene("blue"), changed_scene("orange")
        calls = []

        async def capture(_screen, _completed_at):
            for _ in range(4):
                screen._on_jpeg(before)  # pre-sheet plain frames before sheet pops up
            screen._on_jpeg(sheet)
            return sheet

        async def verify(original, presented, selected, region, text):
            calls.append(presented)
            return presented == sheet

        monkeypatch.setattr(feedback, "wait_after_action", capture)
        monkeypatch.setattr(paste_confirmation, "verify_after_paste_permission", verify)
        prepared = await manager.prepare(prepare_body(control))
        copied(provider, screen)
        await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                             screen_id=control.observation()["screen_id"], focused=True))
        screen._recent_frames.clear()
        screen._on_jpeg(after)
        receipt = await manager.confirm(ConfirmPaste(
            transaction_id=prepared["transaction_id"],
            screen_id=control.observation()["screen_id"],
            observed_text="你好00123", confirmed=True))
        assert receipt["phase"] == "completed" and receipt["effect_verified"]
        assert calls == [sheet]

    asyncio.run(scenario())
