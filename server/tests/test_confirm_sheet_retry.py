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
        before, sheet, after = field_image(), changed_scene("blue"), changed_scene("orange")
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
        before, sheet, after = field_image(), changed_scene("blue"), changed_scene("orange")
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
