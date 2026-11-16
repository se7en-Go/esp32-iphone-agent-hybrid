"""Confirm retries the permission-sheet fallback with recent frames.

The first post-paste frame can miss the sheet entirely (it appears after
cmd+v, or was already dismissed), so a single verify attempt with it fails
every retry. The fix retries with the most recent frames that could still
show the sheet.
"""

import asyncio
import io

import pytest
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
        assert calls[0] == before and sheet in calls
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
