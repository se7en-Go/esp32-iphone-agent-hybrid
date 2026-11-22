"""Host-observed clipboard transactions; never hold the action lock awaiting a host decision."""

import asyncio
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import feedback, ocr, screen_compare
from .clipboard import NO_STORE
from .freshness import fresh_frame, snapshot
from .input_context import InputContextGuard
from .operation import check_operation
from .text_input import select_type_mode

log = logging.getLogger(__name__)


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


class StatusRequest(TransactionId):
    include_observation: StrictBool = True


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
    # Frames observed right after Cmd+V, retained for this transaction: the
    # permission sheet may leave the 4 s recent window before confirm runs.
    post_paste_frames: tuple = ()
    # (frame, updated, generation) of a sheet frame the triple check already
    # verified for this transaction; a later confirm retry reuses it instead
    # of re-scanning frames that have since rolled out of the window.
    sheet_evidence: tuple | None = None


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

    def _abandon(self, item, *, suppress_errors=False):
        try:
            state = item.provider.stage_finish(item.nonce)
        except HTTPException as exc:
            if exc.status_code != 404:
                if not suppress_errors:
                    raise
                log.warning("clipboard abandon failed (HTTP %s): %s", exc.status_code, exc.detail)
            state = item.provider.current()
        except Exception as exc:
            if not suppress_errors:
                raise
            log.warning("clipboard abandon failed: %s: %s", type(exc).__name__, exc)
            state = item.provider.current()
        if self.transaction is item:
            self.transaction = None
        return {**state, "phase": "cancelled", "effect_verified": False}

    def status(self, nonce, *, include_observation=True):
        item = self._lookup(nonce)
        state = {**item.provider.stage_status(nonce), "focus_verified_by": item.focus_verified_by}
        if not include_observation:
            # Read-only poll while waiting for the copy ACK: no image and no new
            # screen_id. The next action still needs its own fresh observation.
            return state
        return {**state, **self._evidence()}

    async def prepare(self, request):
        controls = self.get_controls()
        if hasattr(controls, "clear_verified_caret_scope"):
            controls.clear_verified_caret_scope()
        phone, screen = controls.phone, controls.screen
        provider = getattr(phone, "clipboard", None)
        if provider is None:
            raise ValueError("clipboard companion is not configured; see docs/PASTE_INPUT.md")
        select_type_mode(request.text, "paste", clipboard_available=True)
        if request.focused is not True:
            raise ValueError("visually confirm the intended input field is focused before prepare")
        scope = tuple(ocr.validate_region(request.target_box)) if request.target_box is not None else None
        controls.consume(request.screen_id, caret_scope=scope)
        anchor = InputContextGuard(screen, controls._observation, allow_assistive_opacity=True)
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
            self._abandon(item, suppress_errors=True)
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
        controls._validate_ticket(controls.ticket, request.screen_id, caret_scope=item.guard.region)
        if not self._post_event_evidence(controls, copied["copied_at"]):
            raise ValueError("observe a fresh frame after the copy ACK and mirror pipeline before pasting")
        controls.consume(request.screen_id, caret_scope=item.guard.region)
        item.guard()
        item.provider.stage_start_paste(item.nonce)
        validate_failed = False

        def validate():
            nonlocal validate_failed
            try:
                self._session(item)
                if fresh_frame(controls.screen, since=copied["copied_at"]) is None:
                    raise ValueError("fresh post-copy frame lost before paste; observe again")
                item.guard()
                item.provider.stage_validate_paste(item.nonce)
            except (RuntimeError, ValueError, HTTPException):
                validate_failed = True
                raise

        try:
            with item.provider.stage_write(item.nonce, "key cmd+v"):
                await item.phone.key("cmd+v", before_action=validate)
            item.provider.stage_paste_sent(item.nonce)
            item.completed_at = time.time()
            await feedback.wait_after_action(controls.screen, item.completed_at)
            # Retain the frames observed right after Cmd+V in this
            # transaction. The permission sheet appears here, but confirm
            # may run after the 4 s recent window has dropped it; the
            # triple check below can still verify a retained frame.
            item.post_paste_frames = tuple(getattr(controls.screen, "recent_frames", ()))
        except asyncio.CancelledError:
            self._abandon(item, suppress_errors=True)
            raise
        except (RuntimeError, ValueError, HTTPException) as exc:
            state = self._abandon(item, suppress_errors=True)
            # Only post-validation errors may have written bytes to HID;
            # a failure inside before_action rejected strictly before write.
            return {**self._evidence(), **state, "command_acknowledged": False, "effect_verified": False,
                    "action_may_have_executed": not validate_failed, "error": str(exc), "phase": "cancelled"}
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
        controls._validate_ticket(controls.ticket, request.screen_id, caret_scope=item.guard.region)
        if not self._post_event_evidence(controls, item.completed_at):
            raise ValueError("confirmation requires a fresh post-paste observation")
        evidence = {}
        try:
            # Allow text changes only inside the original observed input area.
            await asyncio.to_thread(item.guard, stage="accept")
        except RuntimeError:
            # A native paste permission sheet can re-render the original page.
            # This evidence only records the effect of a paste that already ran.
            # Accept only same-session frames from after this Cmd+V and before
            # the result frame. Each candidate carries its own timestamp and
            # generation, so a stale sheet from an earlier transaction can
            # never verify a new one, and a sheet is not pushed out of the
            # candidate list by later ordinary frames or by the 4 s recent
            # window expiring: frames retained at paste time, and a sheet
            # verified once in this transaction, stay candidates.
            from . import paste_confirmation

            verify = paste_confirmation.verify_after_paste_permission
            result = controls._observation
            after = result.frame
            original = item.guard.original
            if (verify is paste_confirmation._default_verify_after_paste_permission
                    and not await paste_confirmation.verify_before_after_for_permission(
                        original.frame, after, item.guard.region, request.observed_text)):
                raise
            seen = {original.frame, after}
            verified = None
            # A sheet this transaction already verified stays usable even
            # after the 4 s recent window has dropped it: the window and
            # session checks below already ran for it at verification time.
            if item.sheet_evidence is not None:
                frame, updated, generation = item.sheet_evidence
                if (frame and frame not in seen and generation == original.generation
                        and updated > item.completed_at and updated <= result.updated
                        and await verify(
                            original.frame, frame, after,
                            item.guard.region, request.observed_text)):
                    verified = item.sheet_evidence
            if verified is None:
                # Candidates are the frames retained right after Cmd+V (latest
                # settled frame first, before early transition frames), then the
                # live recent window; a sheet is not pushed out of the candidate
                # list by later ordinary frames, and a sheet that left the
                # window before confirm still verifies.
                candidates = tuple(reversed(item.post_paste_frames)) + tuple(
                    getattr(controls.screen, "recent_frames", ()))
                for snapshot in candidates:
                    frame = getattr(snapshot, "frame", None)
                    updated = getattr(snapshot, "updated", None)
                    generation = getattr(snapshot, "generation", None)
                    if (not frame or frame in seen or updated is None or generation is None
                            or generation != original.generation
                            or updated <= item.completed_at
                            or updated > result.updated):
                        continue
                    seen.add(frame)
                    # A permission sheet dims and covers the page, so it differs
                    # from both the original page and the result page. Skip
                    # plain pre-sheet or post-sheet frames before OCR (~1 s/frame).
                    if (not await asyncio.to_thread(screen_compare.observation_changed, after, frame)
                            or not await asyncio.to_thread(screen_compare.observation_changed, original.frame, frame)):
                        continue
                    if await verify(
                            original.frame, frame, after,
                            item.guard.region, request.observed_text):
                        verified = (frame, updated, generation)
                        item.sheet_evidence = verified
                        break
            if verified is None:
                raise
            _sheet_frame, sheet_updated, sheet_generation = verified
            evidence["confirmation_evidence"] = "paste_permission_dismissed"
            evidence["sheet_frame_updated"] = sheet_updated
            evidence["sheet_frame_generation"] = sheet_generation
        if evidence:
            if self._lookup(item.nonce) is not item:
                raise ValueError("clipboard transaction changed during confirmation")
            self._session(item)
            if not self._post_event_evidence(controls, item.completed_at):
                raise ValueError("confirmation requires a fresh post-paste observation")
        controls.consume(request.screen_id, caret_scope=item.guard.region)
        state = item.provider.stage_finish(item.nonce, confirmed=True)
        self.transaction = None
        if item.guard.region is not None and hasattr(controls, "remember_verified_caret_scope"):
            controls.remember_verified_caret_scope(
                item.guard.region, device=item.device, links=item.links, epoch=item.epoch,
                generation=item.guard.original.generation)
        return {**state, **self._evidence(), "phase": "completed", "effect_verified": True,
                "verified_by": "host_observation", "server_content_verified": False, **evidence}

    def cancel(self, nonce):
        item = self._lookup(nonce)
        state = item.provider.stage_finish(nonce)
        self.transaction = None
        controls = self.get_controls()
        if hasattr(controls, "clear_verified_caret_scope"):
            controls.clear_verified_caret_scope()
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
    async def status(body: StatusRequest, request: Request):
        return await run(request, lambda: manager.status(
            body.transaction_id, include_observation=body.include_observation), read_only=True)

    @router.post("/cancel")
    async def cancel(body: TransactionId, request: Request):
        return await run(request, lambda: manager.cancel(body.transaction_id), read_only=True)

    return router
