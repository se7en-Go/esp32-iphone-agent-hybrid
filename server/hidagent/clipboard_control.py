"""Host-observed clipboard transactions; never hold the action lock awaiting a host decision."""

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import feedback, ocr
from .clipboard import NO_STORE
from .freshness import fresh_frame, snapshot
from .input_context import InputContextGuard
from .operation import check_operation
from .text_input import select_type_mode


class TransactionId(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transaction_id: str = Field(min_length=1, max_length=100)


class PreparePaste(BaseModel):
    model_config = ConfigDict(extra="forbid")
    screen_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=4000)
    focused: StrictBool
    target_box: tuple[float, float, float, float] | None = None


class PasteTransaction(TransactionId):
    screen_id: str = Field(min_length=1)
    focused: StrictBool


class ConfirmPaste(TransactionId):
    screen_id: str = Field(min_length=1)
    observed_text: str = Field(min_length=1, max_length=4000)
    confirmed: StrictBool


@dataclass(repr=False)
class _Transaction:
    nonce: str
    provider: object
    phone: object
    device: object
    guard: InputContextGuard
    links: int
    epoch: int
    expected_digest: bytes
    focus_verified_by: str
    completed_at: float | None = None
    first_post_paste_frame: bytes | None = None


class StagedClipboardControls:
    def __init__(self, get_controls):
        self.get_controls = get_controls
        self.transaction = None

    def _lookup(self, nonce):
        item = self.transaction
        if item is None or not nonce.isascii() or not secrets.compare_digest(nonce, item.nonce):
            raise HTTPException(404, "clipboard transaction is invalid or consumed", headers=NO_STORE)
        try:
            item.provider.stage_status(nonce)
        except HTTPException as exc:
            if exc.status_code == 404:
                self.transaction = None
            raise
        return item

    def _session(self, item):
        current = self.get_controls()
        try:
            # A live static stream may retain old pixels while the Shortcut
            # runs. Preserve its copy transaction, not its input eligibility:
            # prepare/paste/confirm still require fresh action evidence.
            observed = snapshot(current.screen)
            device = item.phone.device
            if (current.phone is not item.phone or current.phone.clipboard is not item.provider
                    or current.screen is not item.guard.screen or device is not item.device
                    or getattr(device, "links", 0) != item.links
                    or getattr(device, "abort_epoch", 0) != item.epoch
                    or not getattr(device, "connected", False)
                    or not observed.live or not observed.frame
                    or observed.generation != item.guard.original.generation):
                raise RuntimeError("clipboard input session changed or stopped; do not paste")
        except (ValueError, RuntimeError):
            item.provider.cancel_staged()
            if self.transaction is item:
                self.transaction = None
            raise

    def _evidence(self):
        try:
            return self.get_controls().observation()
        except (ValueError, RuntimeError):
            self.get_controls().invalidate()
            return {"screen_id": None, "image_base64": None, "fresh": False}

    @staticmethod
    def _post_event_evidence(controls, since):
        selected = SimpleNamespace(snapshot=controls._observation,
                                   _freshness_phone=getattr(controls.screen, "_freshness_phone", None))
        return fresh_frame(selected, since=since) is not None and fresh_frame(controls.screen, since=since) is not None

    def _abandon(self, item):
        try:
            state = item.provider.stage_finish(item.nonce)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            state = item.provider.current()
        if self.transaction is item:
            self.transaction = None
        return {**state, "phase": "cancelled", "effect_verified": False}

    def status(self, nonce):
        item = self._lookup(nonce)
        return {**item.provider.stage_status(nonce), **self._evidence(),
                "focus_verified_by": item.focus_verified_by}

    async def prepare(self, request):
        controls = self.get_controls()
        phone, screen = controls.phone, controls.screen
        provider = getattr(phone, "clipboard", None)
        if provider is None:
            raise ValueError("clipboard companion is not configured; see docs/PASTE_INPUT.md")
        select_type_mode(request.text, "paste", clipboard_available=True)
        if request.focused is not True:
            raise ValueError("visually confirm the intended input field is focused before prepare")
        controls.consume(request.screen_id)
        anchor = InputContextGuard(screen, controls._observation)
        focus_source = "visible_caret"
        if request.target_box is not None:
            anchor.region = tuple(ocr.validate_region(request.target_box))
            focus_source = "host_observation"
        if anchor.region is None:
            raise ValueError("no bounded focused field detected; supply an observed target_box with focused=true")
        nonce = provider.stage_begin(request.text, validate_session=lambda: self._session(item))
        device = phone.device
        item = _Transaction(nonce, provider, phone, device, anchor, getattr(device, "links", 0),
                            getattr(device, "abort_epoch", 0), hashlib.sha256(request.text.encode()).digest(),
                            focus_source)
        self.transaction = item

        def validate():
            self._session(item)
            provider.stage_status(nonce)
            anchor()

        try:
            validate()
            if provider.trigger_key:
                command = f"key {provider.trigger_key}"
                with provider.stage_write(nonce, command):
                    await phone.key(provider.trigger_key, before_action=validate)
        except BaseException:
            self._abandon(item)
            raise
        return self.status(nonce)

    async def paste(self, request):
        item = self._lookup(request.transaction_id)
        controls = self.get_controls()
        if request.focused is not True:
            raise ValueError("observe the original focused field before pasting")
        copied = item.provider.stage_status(item.nonce)
        if not copied["copied"]:
            raise ValueError("Shortcut has not acknowledged an actual copy; check paste status")
        self._session(item)
        controls._validate_ticket(controls.ticket, request.screen_id)
        if not self._post_event_evidence(controls, copied["copied_at"]):
            raise ValueError("observe a fresh frame after the copy ACK and mirror pipeline before pasting")
        controls.consume(request.screen_id)
        item.guard()
        item.provider.stage_start_paste(item.nonce)

        def validate():
            self._session(item)
            if fresh_frame(controls.screen, since=copied["copied_at"]) is None:
                raise ValueError("fresh post-copy frame lost before paste; observe again")
            item.guard()
            item.provider.stage_validate_paste(item.nonce)

        try:
            with item.provider.stage_write(item.nonce, "key cmd+v"):
                await item.phone.key("cmd+v", before_action=validate)
            item.provider.stage_paste_sent(item.nonce)
            item.completed_at = time.time()
            item.first_post_paste_frame = await feedback.wait_after_action(controls.screen, item.completed_at)
        except asyncio.CancelledError:
            self._abandon(item)
            raise
        except (RuntimeError, ValueError, HTTPException) as exc:
            state = self._abandon(item)
            return {**self._evidence(), **state, "command_acknowledged": False, "effect_verified": False,
                    "action_may_have_executed": True, "error": str(exc), "phase": "cancelled"}
        result = self.status(item.nonce)
        if fresh_frame(controls.screen, since=item.completed_at) is None:
            controls.invalidate()
            result["screen_id"] = None
        return {**result, "command_acknowledged": True, "effect_verified": False,
                "verification_required": "Inspect the inserted text in the original field; confirm separately."}

    async def confirm(self, request):
        item = self._lookup(request.transaction_id)
        if request.confirmed is not True:
            raise ValueError("confirm only after visually checking the complete inserted text")
        if not secrets.compare_digest(hashlib.sha256(request.observed_text.encode()).digest(), item.expected_digest):
            raise ValueError("observed_text must match the complete exact intended text")
        if item.completed_at is None or not item.provider.stage_status(item.nonce)["paste_sent"]:
            raise ValueError("paste has not been sent")
        self._session(item)
        controls = self.get_controls()
        controls._validate_ticket(controls.ticket, request.screen_id)
        if not self._post_event_evidence(controls, item.completed_at):
            raise ValueError("confirmation requires a fresh post-paste observation")
        evidence = {}
        try:
            item.guard(stage="accept")  # allow text changes only inside the original observed input area
        except RuntimeError:
            if item.first_post_paste_frame is None:
                raise
            # A native paste permission sheet can re-render the original page.
            # This evidence only records the effect of a paste that already ran.
            # The first post-paste frame may have missed the sheet entirely
            # (it appears after cmd+v, or was already dismissed), so retry
            # with the most recent frames that could still show it.
            from .paste_confirmation import verify_after_paste_permission

            after = controls._observation.frame
            candidates = [item.first_post_paste_frame]
            seen = {item.guard.original.frame, after, item.first_post_paste_frame}
            for snapshot in reversed(getattr(controls.screen, "recent_frames", ())):
                frame = getattr(snapshot, "frame", snapshot)
                if frame and frame not in seen:
                    seen.add(frame)
                    candidates.append(frame)
                if len(candidates) >= 4:
                    break
            verified = False
            for sheet in candidates:
                if await verify_after_paste_permission(
                        item.guard.original.frame, sheet, after,
                        item.guard.region, request.observed_text):
                    verified = True
                    break
            if not verified:
                raise
            evidence["confirmation_evidence"] = "paste_permission_dismissed"
        if evidence:
            if self._lookup(item.nonce) is not item:
                raise ValueError("clipboard transaction changed during confirmation")
            self._session(item)
            if not self._post_event_evidence(controls, item.completed_at):
                raise ValueError("confirmation requires a fresh post-paste observation")
        controls.consume(request.screen_id)
        state = item.provider.stage_finish(item.nonce, confirmed=True)
        self.transaction = None
        return {**state, **self._evidence(), "phase": "completed", "effect_verified": True,
                "verified_by": "host_observation", "server_content_verified": False, **evidence}

    def cancel(self, nonce):
        item = self._lookup(nonce)
        state = item.provider.stage_finish(nonce)
        self.transaction = None
        return {**state, "phase": "cancelled", "effect_verified": False}


def create_control_router(get_controls, get_lock, check_manual, refuse_if_tripped, while_connected):
    """Use the app's single-controller lock, but only for one bounded stage at a time."""
    router = APIRouter(prefix="/api/control/clipboard")
    manager = StagedClipboardControls(get_controls)

    async def run(request, operation, *, read_only=False):
        if not read_only:
            refuse_if_tripped()
        check_manual()
        async with get_lock():
            controls = get_controls()
            controls.job = asyncio.current_task()
            try:
                async def execute():
                    result = operation()
                    return await result if asyncio.iscoroutine(result) else result
                return JSONResponse(await while_connected(request, execute()), headers=NO_STORE)
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(409, str(exc), headers=NO_STORE) from exc
            except asyncio.CancelledError as exc:
                check_operation()  # Let the action middleware report its deadline or disconnect.
                raise HTTPException(409, "clipboard stage stopped; observe before continuing",
                                    headers=NO_STORE) from exc
            finally:
                controls.job = None

    @router.post("/prepare")
    async def prepare(body: PreparePaste, request: Request):
        return await run(request, lambda: manager.prepare(body))

    @router.post("/paste")
    async def paste(body: PasteTransaction, request: Request):
        return await run(request, lambda: manager.paste(body))

    @router.post("/confirm")
    async def confirm(body: ConfirmPaste, request: Request):
        return await run(request, lambda: manager.confirm(body))

    @router.post("/status")
    async def status(body: TransactionId, request: Request):
        return await run(request, lambda: manager.status(body.transaction_id), read_only=True)

    @router.post("/cancel")
    async def cancel(body: TransactionId, request: Request):
        return await run(request, lambda: manager.cancel(body.transaction_id), read_only=True)

    return router
