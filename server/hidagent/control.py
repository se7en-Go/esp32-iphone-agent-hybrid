"""Observed, single-use device actions for external agents. No model calls.

`perform_batch` 每步检查当前画面；不确定结果或上下文变化会停止后续步骤。
输入、选词、发送需要新观察，不能依赖同一次 batch 中前面的动作。
"""

import asyncio
import base64
import io
import math
import time
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from types import SimpleNamespace

from PIL import Image
from pypinyin import lazy_pinyin

from . import assistive_vision, feedback, freshness, ocr, screen_compare, screen_diff, vision
from .assistive import HomeUnconfirmed
from .device import InputAttempt, input_attempt
from .freshness import fresh_frame, require_fresh
from .input_context import InputContextGuard
from .navigation_context import NavigationContext, for_phone
from .operation import check_operation, remaining_operation_time
from .phone import OBSERVATION_ENTRY_MAX_AGE, TypingVerificationError, validate_app_query
from .pointer_diagnostics import PointerDiagnostics
from .send_context import is_send_target, send_target_scope
from .text_input import clipboard_mode_options, select_type_mode
from .text_target import TextTargetGuard


@dataclass(frozen=True)
class _ObservedInput:
    device: object
    links: int | None
    epoch: int | None
    sequence: int | None
    last_input_at: float | None
    session_started_at: float | None


@dataclass(frozen=True)
class _ResponseSession:
    device: object
    links: int | None
    epoch: int | None
    generation: int


class _ActionTargetGuard(TextTargetGuard):
    def __init__(self, *args, before_session, **kwargs):
        super().__init__(*args, **kwargs)
        self.before_session = before_session

    def __call__(self, **evidence):
        self.before_session()
        super().__call__(**evidence)
        self.before_session()


class Controls:
    def __init__(self, phone, screen):
        self.phone, self.screen = phone, screen
        self.ticket = None
        self._input_observation = None
        self._host_observation = None
        self.job = None

    def invalidate(self):
        qualified = self._host_observation
        self._host_observation = None
        if qualified is not None:
            qualified.lease.release()
        self.ticket = None
        self._input_observation = None

    def capture(self):
        try:
            accepted = require_fresh(self.screen)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        return self._frame_size(accepted.frame)

    @staticmethod
    def _frame_size(frame):
        with Image.open(io.BytesIO(frame)) as image:
            width, height = image.size
        return frame, width, height

    def observation(self, accepted=None):
        self.invalidate()
        try:
            accepted = accepted or require_fresh(self.screen)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        frame = accepted.frame
        with Image.open(io.BytesIO(frame)) as image:
            width, height = image.size
        token = uuid.uuid4().hex
        self.ticket = (token, time.monotonic(), frame, width, height)
        self._host_observation = None
        self._generation = accepted.generation
        self._observation = accepted
        device = getattr(self.phone, "device", None)
        self._input_observation = _ObservedInput(
            device, getattr(device, "links", None), getattr(device, "abort_epoch", None),
            getattr(device, "input_sequence", None), getattr(device, "last_input_at", None),
            getattr(device, "session_started_at", None))
        return {"screen_id": token, "width": width, "height": height,
                "frame_received_at": accepted.updated, "expires_in_seconds": 30,
                "coordinate_spaces": {"normalized": "0..1", "pixels": "0..width-1, 0..height-1"},
                "image_base64": base64.b64encode(frame).decode()}

    async def observe_for_click(self):
        """Passively qualify pointer pixels without moving the device."""
        self.invalidate()
        qualified = await self.phone.observe_host_pointer()
        try:
            shot = self.observation(qualified.snapshot)
            self._host_observation = qualified
            self._input_observation = _ObservedInput(
                qualified.device, qualified.links, qualified.abort_epoch, qualified.input_sequence,
                qualified.last_input_at, qualified.session_started_at)
            self.ticket = (self.ticket[0], qualified.expires_monotonic - 30, *self.ticket[2:])
            shot.update(for_click=True, observation_mode="passive_host_pointer",
                        expires_in_seconds=max(0, qualified.expires_monotonic - time.monotonic()))
            return shot
        except BaseException:
            qualified.lease.release()
            self.invalidate()
            raise

    def consume(self, screen_id, *, navigation_context=None, caret_scope=None):
        ticket = self.ticket
        self.invalidate()
        return self._validate_ticket(ticket, screen_id, navigation_context=navigation_context,
                                     caret_scope=caret_scope)

    @staticmethod
    def _check_ticket(ticket, screen_id, *, admission: InputAttempt | None = None):
        if ticket is None or ticket[0] != screen_id:
            raise ValueError("invalid or consumed screen_id; observe again before acting")
        if not (admission is not None and admission.started) and time.monotonic() - ticket[1] > 30:
            raise ValueError("screen_id expired; observe again")

    def _validate_ticket(self, ticket, screen_id, *, navigation_context=None, observation=None,
                         on_reject=None, admission=None, caret_scope=None):
        def rejected(reason, current=None):
            if on_reject is not None:
                on_reject(reason, current if current is not None else freshness.snapshot(self.screen))

        try:
            self._check_ticket(ticket, screen_id, admission=admission)
        except ValueError:
            rejected("ticket")
            raise
        generation = observation.generation if observation is not None else self._generation
        try:
            current = require_fresh(self.screen)
        except RuntimeError as exc:
            rejected("freshness")
            raise ValueError(str(exc)) from exc
        frame, width, height = self._frame_size(current.frame)
        if current.generation != generation or (width, height) != ticket[3:]:
            rejected("generation_or_dimensions", current)
            raise ValueError("screen changed since observation; observe again")
        if navigation_context is not None:
            if not isinstance(navigation_context, NavigationContext):
                rejected("invalid_navigation_context", current)
                raise ValueError("invalid navigation context")
            try:
                navigation_context.validate_target(ticket[2], generation, navigation_context.target.center)
                navigation_context.validate_current(self.screen)
            except RuntimeError as exc:
                rejected("navigation_context", freshness.snapshot(self.screen))
                raise ValueError(str(exc)) from exc
        elif screen_compare.observation_changed(
                ticket[2], frame, caret_frames=getattr(self.screen, "recent_frames", ()),
                caret_scope=caret_scope):
            rejected("layout_changed", current)
            raise ValueError("screen changed since observation; observe again")
        return ticket[2], width, height

    @staticmethod
    def _record_admission_rejection(phase, reason, original, current):
        recorder = PointerDiagnostics.from_env(prefix="action-admission")
        if recorder is None:
            return

        def metadata(observed):
            if observed is None:
                return None
            dimensions = None
            if observed.frame:
                try:
                    with Image.open(io.BytesIO(observed.frame)) as image:
                        dimensions = list(image.size)
                except (OSError, ValueError):
                    pass
            return {"generation": observed.generation, "updated": observed.updated,
                    "dimensions": dimensions, "live": observed.live, "frame_ts": observed.frame_ts}

        for name, observed in (("original", original), ("current", current)):
            if observed is not None and observed.frame:
                recorder.capture(name, observed.frame, observed.updated)
        recorder.event("admission_rejected", rejection_phase=phase, reason=reason,
                       original=metadata(original), current=metadata(current))

    async def _consume_action(self, request, admission):
        """Refresh a silent source under the original action evidence, never a new ticket."""
        ticket, original, inputs = self.ticket, getattr(self, "_observation", None), self._input_observation
        self.invalidate()
        recorded = False

        def record(phase, reason, current=None):
            nonlocal recorded
            if not recorded:
                recorded = True
                self._record_admission_rejection(
                    phase, reason, original, current if current is not None else freshness.snapshot(self.screen))

        try:
            self._check_ticket(ticket, request.screen_id)
        except ValueError:
            record("entry", "ticket")
            raise
        try:
            plan = self._plan(request, ticket[3], ticket[4])  # reject invalid actions before any wake movement
        except ValueError:
            record("entry", "invalid_action")
            raise
        navigation = for_phone(self.phone, request.action, original) if request.action in {"back", "home"} else None

        def same_session():
            try:
                self._check_ticket(ticket, request.screen_id, admission=admission)
            except ValueError as exc:
                record("session", "ticket")
                raise RuntimeError(str(exc)) from exc
            device = getattr(self.phone, "device", None)
            if (inputs is not None and (device is not inputs.device
                    or getattr(device, "links", None) != inputs.links
                    or getattr(device, "abort_epoch", None) != inputs.epoch)):
                record("session", "device_session")
                raise RuntimeError("device session changed since observation; observe again")
            current = freshness.snapshot(self.screen)
            if current.generation != original.generation:
                record("session", "generation", current)
                raise RuntimeError("screen changed since observation; observe again")

        def before_refresh():
            same_session()
            # Only wake admission may accept an old receipt. Live/replay and the
            # original layout still hold before each physical nudge write.
            try:
                current = require_fresh(self.screen, max_age=float("inf"))
            except RuntimeError:
                record("before_refresh", "freshness")
                raise
            _, width, height = self._frame_size(current.frame)
            if (width, height) != ticket[3:]:
                record("before_refresh", "dimensions", current)
                raise ValueError("screen changed since observation; observe again")
            regions = None
            if navigation is not None:
                try:
                    navigation.validate_target(original.frame, current.generation, navigation.target.center)
                except RuntimeError:
                    record("before_refresh", "navigation_context", current)
                    raise
                regions = navigation.regions
            if screen_compare.observation_changed(
                    original.frame, current.frame, context_regions=regions,
                    caret_frames=getattr(self.screen, "recent_frames", ())):
                record("before_refresh", "layout_changed", current)
                raise ValueError("screen changed since observation; observe again")

        admission.before_write = same_session
        same_session()
        ensure = getattr(self.phone, "ensure_observation", None)
        if ensure is not None and fresh_frame(self.screen, max_age=OBSERVATION_ENTRY_MAX_AGE) is None:
            before_refresh()
            await ensure(before_refresh=before_refresh)
            same_session()
        before, _, _ = self._validate_ticket(
            ticket, request.screen_id, navigation_context=navigation, observation=original,
            on_reject=lambda reason, current: record("final_validation", reason, current), admission=admission)
        plan.update(navigation_context=navigation, target_frame=before, observation=original,
                    before_session=same_session)
        return plan, before, ticket[1]

    async def find_text(self, screen_id, text, nth=1, exact=False, region=None):
        """只读取观察中的文字，不消耗操作凭据。"""
        before, _, _ = self._validate_ticket(self.ticket, screen_id)
        self._check_text_query(text, nth)
        region = ocr.validate_region(region)
        matches = await ocr.find_matches_async(before, text, exact, region)
        self._validate_ticket(self.ticket, screen_id)
        return {"screen_id": screen_id, "query": text, "nth": nth, "exact": exact, "region": region,
                "coordinate_space": "normalized", "matches": [asdict(match) for match in matches],
                "match": asdict(matches[nth - 1]) if nth <= len(matches) else None}

    @staticmethod
    def _check_text_query(text, nth):
        if not ocr.normalize_text(text):
            raise ValueError("查找文字不能为空。")
        if nth is not None and (isinstance(nth, bool) or not isinstance(nth, int) or nth < 1):
            raise ValueError("nth 从 1 开始。")

    async def _resolve_text(self, plan, before, observed_at):
        if plan["name"] in {"tap", "long_press", "double_tap"}:
            target = SimpleNamespace(center=plan["xy"])
            if "target_box" in plan:
                target.box = plan["target_box"]
            options = {"before_session": plan["before_session"]} if "before_session" in plan else {}
            guard_type = _ActionTargetGuard if options else TextTargetGuard
            plan["before_click"] = guard_type(self.screen, before, target, observation=plan["observation"], **options)
        if plan["name"] != "tap_text":
            return
        matches = await ocr.find_matches_async(before, plan["text"], plan["exact"], plan["region"])
        try:
            current = require_fresh(self.screen)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        if "before_session" in plan:
            try:
                plan["before_session"]()  # ordinary action uses its original physical-admission state
            except RuntimeError as exc:
                raise ValueError(str(exc)) from exc
            expired = False
        else:
            expired = time.monotonic() - observed_at > 30  # batch retains its existing admission policy
        if (expired or current.generation != plan["observation"].generation
                or screen_compare.observation_changed(
                    before, current.frame,
                    caret_frames=getattr(self.screen, "recent_frames", ()))):
            raise ValueError("OCR 期间屏幕已变化或观察已过期，请重新观察再点击。")
        match = ocr.select_match(matches, plan["text"], plan["nth"], plan["region"])
        if match is None:
            raise ValueError("未找到匹配文字，请重新观察。")
        plan["xy"] = match.center
        plan["is_send"] = is_send_target(plan["text"], match.text)
        options = {"before_session": plan["before_session"]} if "before_session" in plan else {}
        guard_type = _ActionTargetGuard if options else TextTargetGuard
        plan["before_click"] = guard_type(self.screen, before, match, observation=plan["observation"], **options)
        plan["target_box"], plan["target_frame"] = match.box, before
        plan["extra"]["text_match"] = asdict(match)

    def _plan(self, request, width, height) -> dict:
        """校验并归一化。返回之前不会发出任何 HID。"""
        data = request.model_dump()
        name = request.action
        extra: dict = {}
        if request.repeat != 1 and (name != "key" or request.text.lower() not in ("backspace", "left", "right")):
            raise ValueError("repeat>1 only supports editing keys backspace/left/right; never repeat Enter")

        def point(x, y):
            if not all(math.isfinite(v) and v >= 0 for v in (x, y)):
                raise ValueError("coordinates must be finite and non-negative")
            if request.coordinate_space == "pixels":
                if x > width - 1 or y > height - 1:
                    raise ValueError("pixel coordinate outside observed image")
                return x / (width - 1), y / (height - 1)
            if x > 1 or y > 1:
                raise ValueError("normalized coordinates must be 0..1, not 0..1000")
            return x, y

        planned = {"name": name, "extra": extra, "repeat": request.repeat, "text": request.text}
        if name == "tap_text":
            self._check_text_query(request.text, request.nth)
            planned.update(nth=request.nth, exact=request.exact, region=ocr.validate_region(request.region))
        elif name in ("tap", "point", "scroll", "long_press", "double_tap"):
            if request.x is None or request.y is None:
                raise ValueError("x and y required")
            planned["xy"] = point(request.x, request.y)
            box = getattr(request, "target_box", None)
            if name in {"tap", "long_press", "double_tap"} and box is not None:
                x, y = planned["xy"]
                if (not all(math.isfinite(v) for v in box)
                        or not (0 <= box[0] < x < box[2] <= 1 and 0 <= box[1] < y < box[3] <= 1)):
                    raise ValueError("target_box must contain the tap in normalized observed coordinates")
                planned["target_box"] = box
            if name == "scroll":
                planned["lines"] = request.lines
            if name == "long_press":
                planned["duration"] = request.duration
        elif name == "swipe":
            if any(data[key] is None for key in ("x1", "y1", "x2", "y2")):
                raise ValueError("both swipe points required")
            planned["swipe"] = (*point(request.x1, request.y1), *point(request.x2, request.y2))
            planned["hold"] = request.hold
        elif name == "path":
            if request.points is None:
                raise ValueError("path requires 2..256 points")
            planned["points"] = [point(x, y) for x, y in request.points]
            planned["hold"] = request.hold
        elif name == "circle":
            if any(data[key] is None for key in ("x", "y", "radius_x", "radius_y")):
                raise ValueError("circle requires center x/y and radius_x/radius_y")
            cx, cy = point(request.x, request.y)
            rx, ry = request.radius_x, request.radius_y
            if request.coordinate_space == "pixels":
                rx, ry = rx / (width - 1), ry / (height - 1)
            if not (0 <= cx - rx < cx + rx <= 1 and 0 <= cy - ry < cy + ry <= 1):
                raise ValueError("circle extends outside observed image")
            points = [(cx + rx * math.cos(2 * math.pi * i / request.segments),
                       cy + ry * math.sin(2 * math.pi * i / request.segments))
                      for i in range(request.segments)]
            planned.update(points=points + [points[0]], hold=request.hold)
        elif name == "open_app":
            validate_app_query(request.text)
            if not getattr(self.phone, "app_search_available", False):
                raise ValueError("App search is unavailable on this device; inspect capabilities")
            planned["text"] = request.text
            extra.update(app_opened=False, requires_app_selection=True,
                         next_step="Inspect Spotlight results and select the observed application.")
        elif name == "type":
            planned["selected_mode"] = select_type_mode(
                request.text, request.mode, **clipboard_mode_options(getattr(self.phone, "clipboard", None)))
            planned["mode"] = request.mode
        elif name == "compose_chinese":
            if not request.text or not all("\u4e00" <= c <= "\u9fff" for c in request.text) or len(request.text) > 60:
                raise ValueError("compose_chinese needs 1..60 Chinese characters; type punctuation separately")
            if request.input_mode != "pinyin":
                raise ValueError("observe and confirm Chinese Pinyin input mode first; set input_mode=pinyin")
            extra.update({"expected_text": request.text, "typed_ascii": "".join(lazy_pinyin(request.text)),
                          "requires_candidate_selection": True, "submitted": False,
                          "next_step": "Select candidates from image; verify final Chinese before a separate send."})
            planned["text"] = extra["typed_ascii"]
        elif name == "key":
            keys = request.text.lower().split("+")
            known = {"enter", "esc", "tab", "backspace", "space", "up", "down", "left", "right",
                     "cmd", "ctrl", "alt", "shift"}
            if not request.text or not request.text.isascii() or any(
                    key not in known and not (len(key) == 1 and 32 <= ord(key) < 127) for key in keys):
                raise ValueError("unsupported keyboard combination")
            extra.update(repeat_requested=request.repeat, repeat_completed=0)
        elif name not in ("home", "back"):
            raise ValueError(f"unknown action {name}")
        return planned

    def _validate_context(self, planned, *, stage: str = "context", cursor=None, reference=None):
        if "before_session" in planned:
            planned["before_session"]()
        original = planned["observation"]
        current = require_fresh(self.screen)
        if current.generation != original.generation:
            raise RuntimeError("screen changed since the original observation; observe again before acting")
        navigation = planned.get("navigation_context")
        if navigation is not None:
            if (not isinstance(navigation, NavigationContext) or planned["name"] not in {"home", "back"}
                    or navigation.action != planned["name"] or navigation.observation != original):
                raise RuntimeError("invalid system navigation context")
            navigation.validate_current(self.screen, cursor=cursor, reference=reference)
            return
        if stage in {"accept", "chunk"}:
            if "_input_guard" not in planned:
                planned["_input_guard"] = InputContextGuard(self.screen, original)
            planned["_input_guard"](stage=stage)
            return
        if cursor is not None:
            # Gesture callers forward internally measured evidence, never a caller-supplied mask.
            # Reuse the same crop checks as the final pointer guard at the device write boundary.
            if "_gesture_context" not in planned:
                planned["_gesture_context"] = TextTargetGuard(
                    self.screen, original.frame, SimpleNamespace(center=cursor[0]), observation=original)
            planned["_gesture_context"](cursor=cursor, reference=reference)
            return
        if screen_compare.observation_changed(
            original.frame, current.frame,
            caret_frames=getattr(self.screen, "recent_frames", ())):
            raise RuntimeError("screen changed since the original observation; observe again before acting")

    async def _execute(self, planned: dict) -> float | None:
        if "before_session" in planned:
            planned["before_session"]()
        name = planned["name"]
        extra = planned["extra"]
        if name in ("tap", "tap_text", "long_press", "double_tap"):
            kwargs = {}
            if "target_box" in planned:
                kwargs.update(target_box=planned["target_box"], target_frame=planned["target_frame"])
            if "before_click" in planned:
                kwargs["before_click"] = planned["before_click"]
            if name == "long_press":
                kwargs["duration"] = planned["duration"]
            if name == "double_tap":
                kwargs["click_count"] = 2
            with send_target_scope(planned.get("is_send", False)):
                result = await self.phone.tap_verified(*planned["xy"], **kwargs)
            if isinstance(result, dict):
                result = dict(result)
                completed_at = result.pop("completed_at", None)
                extra.update(result)
                return completed_at
            return result
        elif name == "point":
            located = await self.phone.point_verified(*planned["xy"])
            extra["pointer"] = {"x": located[0], "y": located[1]}
            # point_verified returns the request when its final correction is
            # unconfirmed, even if it saw a pointer earlier in the movement.
            extra["pointer_seen"] = getattr(self.phone, "_pointer_confirmed", False)
        elif name == "swipe":
            x1, y1, x2, y2 = planned["swipe"]
            await self.phone.swipe(x1, y1, x2, y2, hold=planned["hold"],
                                   before_action=lambda **evidence: self._validate_context(planned, **evidence))
        elif name in {"path", "circle"}:
            await self.phone.drag(planned["points"], hold=planned["hold"], verify_start=True,
                                  before_action=lambda **evidence: self._validate_context(planned, **evidence))
        elif name == "open_app":
            extra["search_field"] = await self.phone.open_app_search(
                planned["text"], before_action=lambda: self._validate_context(planned))
        elif name == "scroll":
            await self.phone.scroll(*planned["xy"], planned["lines"],
                                    before_action=lambda **evidence: self._validate_context(planned, **evidence))
        elif name == "type":
            await self.phone.type(
                planned["text"], mode=planned["mode"],
                before_action=lambda stage="context": self._validate_context(planned, stage=stage))
        elif name == "compose_chinese":
            await self.phone.type(
                planned["text"], mode="keys",
                before_action=lambda stage="context": self._validate_context(planned, stage=stage))
        elif name == "key":
            for _ in range(planned["repeat"]):
                await self.phone.key(planned["text"].lower(),
                                     before_action=lambda: self._validate_context(planned))
                extra["repeat_completed"] += 1
        elif name == "home":
            options = {"navigation_context": planned["navigation_context"]} if planned.get("navigation_context") else {}
            if planned.get("_reconcile_home"):
                options["report_unconfirmed"] = True
            self._validate_context(planned)
            await self.phone.go_home(**options)
        elif name == "back":
            options = {"navigation_context": planned["navigation_context"]} if planned.get("navigation_context") else {}
            self._validate_context(planned)
            await self.phone.go_back(before_action=lambda **evidence: self._validate_context(planned, **evidence),
                                     **options)
        else:
            raise ValueError(f"unknown action {name}")

    def _response_session(self):
        original = getattr(self, "_observation", None) or freshness.snapshot(self.screen)
        inputs = self._input_observation
        device = inputs.device if inputs is not None else getattr(self.phone, "device", None)
        return _ResponseSession(device, inputs.links if inputs is not None else getattr(device, "links", None),
                                inputs.epoch if inputs is not None else getattr(device, "abort_epoch", None),
                                original.generation)

    def _response_session_valid(self, session):
        check_operation()
        device = getattr(self.phone, "device", None)
        current = freshness.snapshot(self.screen)
        return (device is session.device and getattr(device, "links", None) == session.links
                and getattr(device, "abort_epoch", None) == session.epoch
                and self.screen.live and current.live and current.generation == session.generation)

    async def _response_frame(self, completed_at, session, *, deadline=None, expected_size=None):
        """Passively obtain a current causal frame and a real quiet interval.

        Screen.settled also returns on timeout, so it cannot alone authorize a
        continuation ticket. In particular, a tap's first effect frame may be
        only the first frame of a page transition.
        """
        quiet, reserve = .35, .05
        if (not self._response_session_valid(session) or remaining_operation_time() < quiet + reserve
                or (deadline is not None and deadline - time.monotonic() < quiet)):
            return None, time.perf_counter(), deadline
        current = fresh_frame(self.screen, since=completed_at)
        if current is None:
            # A retry only restabilizes an eligible replacement. It cannot start
            # another freshness wait or grant a new response window.
            if deadline is not None:
                return None, time.perf_counter(), deadline
            wait_budget = min(1.5 + freshness.MIRROR_LAG_SECONDS,
                              remaining_operation_time() - quiet - reserve)
            try:
                await asyncio.wait_for(feedback.wait_after_action(self.screen, completed_at), wait_budget)
            except asyncio.TimeoutError:
                return None, time.perf_counter(), deadline
        fresh_done = time.perf_counter()
        if not self._response_session_valid(session):
            return None, fresh_done, deadline
        current = fresh_frame(self.screen, since=completed_at)
        if current is None:
            return None, fresh_done, deadline
        now = last_change = time.monotonic()
        if deadline is None:
            deadline = now + min(2.0, max(0, remaining_operation_time() - reserve))
        anchor = current.frame
        while True:
            if not self._response_session_valid(session):
                return None, fresh_done, deadline
            current = fresh_frame(self.screen, since=completed_at)
            if (current is None or (expected_size is not None
                                   and self._frame_size(current.frame)[1:] != expected_size)):
                return None, fresh_done, deadline
            now = time.monotonic()
            if current.frame != anchor and vision.changed(anchor, current.frame):
                anchor, last_change = current.frame, now
            if now >= deadline:
                return None, fresh_done, deadline
            if now - last_change >= quiet:
                return current, fresh_done, deadline
            await asyncio.sleep(min(.05, deadline - now))

    async def _respond(self, before, completed_at, began, ready, acknowledged, error, extra,
                       include_diff=True, ocr_diff=False, response_session=None, home_unconfirmed=None):
        device_done = time.perf_counter()
        session = response_session or self._response_session()
        self.invalidate()
        # Preserve the action's historical effect facts, but never issue its
        # first effect snapshot as though it were the current settled source.
        effect_snapshot = extra.pop("_after_snapshot", None)
        extra.pop("_after_frame", None)
        extra.pop("fresh", None)
        if effect_snapshot is not None:
            extra["effect_frame_received_at"] = effect_snapshot.updated
        accepted, fresh_done, deadline = await self._response_frame(completed_at, session)
        after, diff, usable = None, None, False
        diff_started = time.perf_counter()
        while accepted is not None:
            after = accepted.frame
            if include_diff:
                budget = min(remaining_operation_time() - .05, deadline - time.monotonic())
                if budget <= 0:
                    break
                if ocr_diff and budget > screen_diff.OCR_BUDGET_SECONDS:
                    try:
                        diff = await asyncio.wait_for(screen_diff.summarize(
                            before, after, fresh=True, ocr_enabled=True), budget)
                    except asyncio.TimeoutError:
                        break
                else:
                    # Pixel-only feedback does not yield to another task.
                    diff = await screen_diff.summarize(before, after, fresh=True, ocr_enabled=False)
            # The receiver thread can publish another frame even during pixel
            # work. Recompute only after a new quiet interval, within the same
            # original window; never silently attach A's diff to frame B.
            current = fresh_frame(self.screen, since=completed_at)
            if (not self._response_session_valid(session) or current is None
                    or time.monotonic() >= deadline):
                break
            if current.frame == accepted.frame:
                shot = self.observation(current)
                latest = fresh_frame(self.screen, since=completed_at)
                if (not self._response_session_valid(session) or latest is None
                        or time.monotonic() >= deadline):
                    break
                if latest.frame == current.frame:
                    home_verified = (home_unconfirmed is not None
                                     and home_unconfirmed.clicked_at <= completed_at
                                     and home_unconfirmed.same_contact(self.phone, self.screen)
                                     and assistive_vision.is_home_visible(current.frame))
                    if home_verified:
                        # Classification may run while the receiver publishes. A
                        # late endpoint must still be the current settled source.
                        latest = fresh_frame(self.screen, since=completed_at)
                        if (not self._response_session_valid(session) or latest is None
                                or latest.frame != current.frame or time.monotonic() >= deadline):
                            break
                        if home_unconfirmed.same_contact(self.phone, self.screen):
                            error = None
                            extra["effect_verified"] = True
                    usable = True
                    break
                current = latest
            self.invalidate()
            size = self._frame_size(accepted.frame)[1:]
            if self._frame_size(current.frame)[1:] != size:
                break
            accepted, _, _ = await self._response_frame(completed_at, session, deadline=deadline,
                                                         expected_size=size)
        settled_done = time.perf_counter()
        if not usable:
            self.invalidate()
            shot = {"screen_id": None, "image_base64": base64.b64encode(after).decode() if after else None}
            extra["observation_error"] = "No current settled post-action frame; observe again before continuing."
            if include_diff:
                diff = await screen_diff.summarize(before, after, fresh=False, ocr_enabled=False)
        captured = time.perf_counter()
        timings = {"validation": ready - began, "device_and_pointer": device_done - ready,
                   "wait_fresh": fresh_done - device_done, "wait_settled": settled_done - fresh_done,
                   "capture": captured - settled_done, "total": captured - began}
        tap_timing = extra.pop("timing_ms", {})
        if tap_timing:
            tap_timing["diff"] = round(tap_timing.get("diff", 0) + (captured - diff_started) * 1000, 2)
        return {**shot, "command_acknowledged": acknowledged, "fresh": usable,
                "changed": vision.changed(before, after) if usable else None,
                "screen_diff": diff,
                "effect_verified": False, "error": error, "action_may_have_executed": True,
                "verification_required": "Inspect the returned image. Never retry a send automatically.",
                **extra,
                "timing_ms": {**{key: round(value * 1000, 1) for key, value in timings.items()},
                              **{key: value for key, value in tap_timing.items() if key != "total"},
                              **({"tap_total": tap_timing["total"]} if "total" in tap_timing else {})}}

    async def perform(self, request):
        began = time.perf_counter()
        response_session = self._response_session()
        # The same private observer covers wake motion, OCR and the action.
        # Only Device's actual write boundary admits the already-consumed ticket.
        with input_attempt() as admission:
            plan, before, observed_at = await self._consume_action(request, admission)
            if plan["name"] == "home":
                plan["_reconcile_home"] = True
            await self._resolve_text(plan, before, observed_at)
            ready = time.perf_counter()
            completed_at, acknowledged, error, home_unconfirmed = None, False, None, None
            try:
                completed_at = await self._execute(plan)
                acknowledged = True
            except TypingVerificationError as exc:
                completed_at, acknowledged, error = exc.completed_at, True, str(exc)
                plan["extra"].update(exc.outcome)
            except HomeUnconfirmed as exc:
                error = str(exc)
                if plan["name"] == "home" and plan.get("_reconcile_home"):
                    acknowledged, home_unconfirmed = True, exc
                # Preserve the existing failure-time response window. The
                # original click timestamp is provenance, not an earlier cutoff.
                completed_at = time.time()
            except (RuntimeError, OSError) as exc:
                error = str(exc)
        options = {"home_unconfirmed": home_unconfirmed} if home_unconfirmed is not None else {}
        return await self._respond(before, completed_at or time.time(), began, ready, acknowledged, error,
                                   plan["extra"], request.include_diff, request.ocr_diff, response_session, **options)

    def middle_click_home_status(self) -> dict:
        return self.phone.assistive_touch.middle_click_home_status()

    def revoke_middle_click_home(self, *, all_devices: bool = False) -> dict:
        return self.phone.assistive_touch.revoke_middle_click_home(all_devices=all_devices)

    async def enable_middle_click_home(
        self, screen_id: str, *, include_diff: bool = True, ocr_diff: bool = False
    ) -> dict:
        """Verify live BLE `middleclick` -> Home from a clean observed non-Home screen and persist per device."""
        began = time.perf_counter()
        response_session = self._response_session()
        with input_attempt() as admission:
            ticket, original, inputs = self.ticket, getattr(self, "_observation", None), self._input_observation
            self.invalidate()
            self._check_ticket(ticket, screen_id)
            if original is None:
                original = freshness.snapshot(self.screen)
            assistive = self.phone.assistive_touch
            await assistive.check_middle_click_enable_preconditions(ticket[2])

            def same_session():
                self._check_ticket(ticket, screen_id, admission=admission)
                device = getattr(self.phone, "device", None)
                if inputs is not None and (
                        device is not inputs.device
                        or getattr(device, "links", None) != inputs.links
                        or getattr(device, "abort_epoch", None) != inputs.epoch):
                    raise RuntimeError("device session changed since observation; observe again")
                if freshness.snapshot(self.screen).generation != original.generation:
                    raise RuntimeError("screen changed since observation; observe again")

            def before_refresh():
                same_session()
                current = require_fresh(self.screen, max_age=float("inf"))
                if self._frame_size(current.frame)[1:] != ticket[3:]:
                    raise ValueError("screen changed since observation; observe again")
                if screen_compare.observation_changed(
                        original.frame, current.frame,
                        caret_frames=getattr(self.screen, "recent_frames", ())):
                    raise ValueError("screen changed since observation; observe again")

            admission.before_write = same_session
            same_session()
            ensure = getattr(self.phone, "ensure_observation", None)
            if ensure is not None and fresh_frame(self.screen, max_age=OBSERVATION_ENTRY_MAX_AGE) is None:
                before_refresh()
                await ensure(before_refresh=before_refresh)
                same_session()
            before, _, _ = self._validate_ticket(ticket, screen_id, observation=original, admission=admission)
            await assistive.check_middle_click_enable_preconditions(before)
            ready = time.perf_counter()
            completed_at = await assistive.verify_and_enable_middle_click_home(
                observation=original, before_write=same_session
            )
        result = await self._respond(
            before, completed_at, began, ready, True, None,
            {"middle_click_home": self.middle_click_home_status()},
            include_diff, ocr_diff, response_session,
        )
        result["effect_verified"] = True
        return result

    async def click_observed(self, request):
        """Consume a host's explicit pointer observation for one stationary click."""
        qualified = self._host_observation
        self._host_observation = None  # transfer lease ownership to this one operation
        try:
            return await self._click_observed(request, qualified)
        finally:
            if qualified is not None:
                qualified.lease.release()

    async def _click_observed(self, request, qualified):
        began = time.perf_counter()
        response_session = self._response_session()
        ticket, inputs = self.ticket, self._input_observation
        observation = getattr(self, "_observation", None)
        if qualified is None:
            before, _, _ = self.consume(request.screen_id)
        else:
            self.invalidate()
            self._check_ticket(ticket, request.screen_id)
            before = ticket[2]
        pointer = request.pointer_x, request.pointer_y
        box = tuple(request.target_box)
        if (len(box) != 4 or not all(math.isfinite(value) and 0 <= value <= 1 for value in (*pointer, *box))
                or not (box[0] < pointer[0] < box[2] and box[1] < pointer[1] < box[3])):
            raise ValueError("host-observed pointer must be strictly inside the normalized target_box")
        if (inputs is None or inputs.device is None or inputs.links is None or inputs.epoch is None
                or type(inputs.sequence) is not int or inputs.last_input_at is None
                or not math.isfinite(inputs.last_input_at) or inputs.session_started_at is None
                or not math.isfinite(inputs.session_started_at)):
            raise ValueError("host-observed click requires device input history; observe again")

        def validate_original():
            device = self.phone.device
            if (device is not inputs.device or device.links != inputs.links or device.abort_epoch != inputs.epoch
                    or device.input_sequence != inputs.sequence or device.last_input_at != inputs.last_input_at
                    or device.session_started_at != inputs.session_started_at):
                raise ValueError("input or device session changed since observation; observe again")
            boundary = max(inputs.last_input_at, inputs.session_started_at)
            if observation.updated <= boundary + freshness.MIRROR_LAG_SECONDS:
                raise ValueError(
                    "observe a new frame after connection/last input and mirror pipeline before host-click")
            if qualified is None:
                self._validate_ticket(ticket, request.screen_id)
            else:
                self._check_ticket(ticket, request.screen_id)

        validate_original()
        ready = time.perf_counter()
        completed_at, acknowledged, error = None, False, None
        extra = {"evidence_source": "host_observed", "pointer_seen": False, "effect_verified": False}
        duration = getattr(request, "duration", None)
        label = getattr(request, "target_label", "")
        if duration is not None and is_send_target(label, label):
            raise ValueError("host-observed hold cannot target a Send control")
        try:
            with send_target_scope(is_send_target(label, label)) if duration is None else nullcontext():
                result = await self.phone.click_observed(
                    observation=observation, pointer_xy=pointer, target_box=box,
                    expected_device=inputs.device, expected_links=inputs.links,
                    expected_abort_epoch=inputs.epoch, expected_input_seq=inputs.sequence,
                    before_click=validate_original,
                    **({"duration": duration} if duration is not None else {}),
                    **({"host_observation": qualified} if qualified is not None else {}))
            if isinstance(result, dict):
                result = dict(result)
                completed_at = result.pop("completed_at", None)
                extra.update(result)
            acknowledged = True
        except (RuntimeError, OSError) as exc:
            error = str(exc)
        return await self._respond(before, completed_at or time.time(), began, ready, acknowledged, error, extra,
                                   True, False, response_session)

    async def perform_batch(self, screen_id, requests, *, include_diff=True, ocr_diff=False):
        if not 1 <= len(requests) <= 8:
            raise ValueError("batch needs 1..8 actions; split steps that depend on a new screenshot")
        began = time.perf_counter()
        response_session = self._response_session()
        observed_at = self.ticket[1] if self.ticket else 0
        before, width, height = self.consume(screen_id)
        plans = [self._plan(request, width, height) for request in requests]
        for plan in plans:
            plan["target_frame"] = before
            plan["observation"] = self._observation
        if sum(plan.get("selected_mode") == "paste" for plan in plans) > 1:
            raise ValueError("一次 batch 只允许一次粘贴；请确认输入完成后重新观察再输入。")
        if len(plans) > 1 and any(plan["name"] == "tap_text" for plan in plans):
            raise ValueError("tap_text 需要单步执行；查看点击结果后再继续。")
        for plan in plans:
            await self._resolve_text(plan, before, observed_at)
        ready = time.perf_counter()
        completed_at, acknowledged, error = None, False, None
        done = []
        last_extra = {}
        stopped_before = None
        type_acknowledged = False
        for index, plan in enumerate(plans):
            current = fresh_frame(self.screen)
            if (current is None or current.generation != plan["observation"].generation
                    or screen_compare.observation_changed(
                        before, current.frame,
                        caret_frames=getattr(self.screen, "recent_frames", ()))):
                error = "batch context changed or is stale; observe again before continuing"
                stopped_before = index + 1
                break
            try:
                last_extra = plan["extra"]
                completed_at = await self._execute(plan) or time.time()
                done.append({"action": plan["name"], "ok": True,
                             **{key: value for key, value in plan["extra"].items() if not key.startswith("_")}})
                if index + 1 < len(plans):
                    error = await self._batch_boundary(plan, plans[index + 1], before, completed_at)
                    if error:
                        stopped_before = index + 2
                        break
            except TypingVerificationError as exc:
                completed_at, error, type_acknowledged = exc.completed_at, str(exc), True
                last_extra.update(exc.outcome)
                done.append({"action": plan["name"], "ok": False, "command_acknowledged": True,
                             "error": error, **exc.outcome})
                if index + 1 < len(plans):
                    stopped_before = index + 2
                break
            except (RuntimeError, OSError) as exc:
                error = str(exc)
                if len(done) == index:
                    done.append({"action": plan["name"], "ok": False, "error": error})
                else:
                    # The command was acknowledged; only the between-step check failed.
                    stopped_before = index + 2
                break
        acknowledged = type_acknowledged or (error is None and len(done) == len(plans))
        result = await self._respond(before, completed_at or time.time(), began, ready, acknowledged, error,
                                     last_extra, include_diff, ocr_diff, response_session)
        result["steps"] = done
        result["completed"] = sum(1 for step in done if step.get("ok"))
        result["stopped_before_step"] = stopped_before
        return result

    async def _batch_boundary(self, previous, following, before, completed_at):
        # Pixel changes cannot prove a tap focused the intended control.
        if previous["name"] in {"tap", "tap_text", "long_press", "double_tap", "open_app"} and not previous[
                "extra"].get("effect_verified"):
            return "tap effect is unverified; observe again before continuing the batch"
        if following["name"] in {"type", "compose_chinese", "key"}:
            return "typing and keyboard actions require a new observation; observe again before continuing"
        if not await feedback.wait_after_action(self.screen, completed_at):
            return "batch outcome has no fresh evidence; observe again before continuing"
        current = fresh_frame(self.screen, since=completed_at)
        if (current is None or current.generation != previous["observation"].generation
                or screen_compare.observation_changed(
                    before, current.frame,
                    caret_frames=getattr(self.screen, "recent_frames", ()))):
            return "batch context changed or is stale; observe again before continuing"
        return None
