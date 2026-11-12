"""Observed, single-use device actions for external agents. No model calls.

`perform_batch` 每步检查当前画面；不确定结果或上下文变化会停止后续步骤。
输入、选词、发送需要新观察，不能依赖同一次 batch 中前面的动作。
"""

import base64
import io
import math
import time
import uuid
from dataclasses import asdict
from types import SimpleNamespace

from PIL import Image
from pypinyin import lazy_pinyin

from . import feedback, ocr, screen_compare, screen_diff, vision
from .freshness import fresh_frame, require_fresh
from .send_context import is_send_target, send_target_scope
from .text_input import select_type_mode
from .text_target import TextTargetGuard


class Controls:
    def __init__(self, phone, screen):
        self.phone, self.screen = phone, screen
        self.ticket = None
        self.job = None

    def invalidate(self):
        self.ticket = None

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
        try:
            accepted = accepted or require_fresh(self.screen)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        frame = accepted.frame
        with Image.open(io.BytesIO(frame)) as image:
            width, height = image.size
        token = uuid.uuid4().hex
        self.ticket = (token, time.monotonic(), frame, width, height)
        self._generation = accepted.generation
        self._observation = accepted
        return {"screen_id": token, "width": width, "height": height,
                "frame_received_at": accepted.updated, "expires_in_seconds": 30,
                "coordinate_spaces": {"normalized": "0..1", "pixels": "0..width-1, 0..height-1"},
                "image_base64": base64.b64encode(frame).decode()}

    def consume(self, screen_id):
        ticket = self.ticket
        self.invalidate()
        return self._validate_ticket(ticket, screen_id)

    def _validate_ticket(self, ticket, screen_id):
        if ticket is None or ticket[0] != screen_id:
            raise ValueError("invalid or consumed screen_id; observe again before acting")
        if time.monotonic() - ticket[1] > 30:
            raise ValueError("screen_id expired; observe again")
        try:
            current = require_fresh(self.screen)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        frame, width, height = self._frame_size(current.frame)
        if (current.generation != self._generation or (width, height) != ticket[3:]
                or screen_compare.observation_changed(ticket[2], frame)):
            raise ValueError("screen changed since observation; observe again")
        return ticket[2], width, height

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
        if plan["name"] == "tap":
            target = SimpleNamespace(center=plan["xy"])
            if "target_box" in plan:
                target.box = plan["target_box"]
            plan["before_click"] = TextTargetGuard(self.screen, before, target, observation=plan["observation"])
        if plan["name"] != "tap_text":
            return
        matches = await ocr.find_matches_async(before, plan["text"], plan["exact"], plan["region"])
        try:
            current = require_fresh(self.screen)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        if (time.monotonic() - observed_at > 30 or current.generation != plan["observation"].generation
                or screen_compare.observation_changed(before, current.frame)):
            raise ValueError("OCR 期间屏幕已变化或观察已过期，请重新观察再点击。")
        match = ocr.select_match(matches, plan["text"], plan["nth"], plan["region"])
        if match is None:
            raise ValueError("未找到匹配文字，请重新观察。")
        plan["xy"] = match.center
        plan["is_send"] = is_send_target(plan["text"], match.text)
        plan["before_click"] = TextTargetGuard(self.screen, before, match, observation=plan["observation"])
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
        elif name in ("tap", "point", "scroll"):
            if request.x is None or request.y is None:
                raise ValueError("x and y required")
            planned["xy"] = point(request.x, request.y)
            box = getattr(request, "target_box", None)
            if name == "tap" and box is not None:
                x, y = planned["xy"]
                if (not all(math.isfinite(v) for v in box)
                        or not (0 <= box[0] < x < box[2] <= 1 and 0 <= box[1] < y < box[3] <= 1)):
                    raise ValueError("target_box must contain the tap in normalized observed coordinates")
                planned["target_box"] = box
            if name == "scroll":
                planned["lines"] = request.lines
        elif name == "swipe":
            if any(data[key] is None for key in ("x1", "y1", "x2", "y2")):
                raise ValueError("both swipe points required")
            planned["swipe"] = (*point(request.x1, request.y1), *point(request.x2, request.y2))
        elif name == "type":
            planned["selected_mode"] = select_type_mode(
                request.text, request.mode, clipboard_available=getattr(self.phone, "clipboard", None) is not None)
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

    def _validate_context(self, planned):
        original = planned["observation"]
        current = require_fresh(self.screen)
        if (current.generation != original.generation
                or screen_compare.observation_changed(original.frame, current.frame)):
            raise RuntimeError("screen changed since the original observation; observe again before acting")

    async def _execute(self, planned: dict) -> float | None:
        name = planned["name"]
        extra = planned["extra"]
        if name in ("tap", "tap_text"):
            kwargs = {}
            if "target_box" in planned:
                kwargs.update(target_box=planned["target_box"], target_frame=planned["target_frame"])
            if "before_click" in planned:
                kwargs["before_click"] = planned["before_click"]
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
            extra["pointer_seen"] = getattr(self.phone, "_pointer_seen", False)
        elif name == "swipe":
            x1, y1, x2, y2 = planned["swipe"]
            await self.phone.swipe(x1, y1, x2, y2, before_action=lambda: self._validate_context(planned))
        elif name == "scroll":
            await self.phone.scroll(*planned["xy"], planned["lines"],
                                    before_action=lambda: self._validate_context(planned))
        elif name == "type":
            await self.phone.type(planned["text"], mode=planned["mode"],
                                  before_action=lambda: self._validate_context(planned))
        elif name == "compose_chinese":
            await self.phone.type(planned["text"], mode="keys",
                                  before_action=lambda: self._validate_context(planned))
        elif name == "key":
            for _ in range(planned["repeat"]):
                await self.phone.key(planned["text"].lower(),
                                     before_action=lambda: self._validate_context(planned))
                extra["repeat_completed"] += 1
        elif name == "home":
            await self.phone.go_home()
        elif name == "back":
            await self.phone.go_back(before_action=lambda: self._validate_context(planned))
        else:
            raise ValueError(f"unknown action {name}")

    async def _respond(self, before, completed_at, began, ready, acknowledged, error, extra,
                       include_diff=True, ocr_diff=False):
        device_done = time.perf_counter()
        tap_checked = "screen_changed" in extra
        fresh = extra.pop("fresh") if tap_checked else await feedback.wait_after_action(self.screen, completed_at)
        fresh_done = time.perf_counter()
        if not tap_checked:
            await self.screen.settled(least=0.3, most=2)
        settled_done = time.perf_counter()
        accepted = extra.pop("_after_snapshot", None)
        accepted_frame = extra.pop("_after_frame", None)
        if not tap_checked:
            accepted = fresh_frame(self.screen, since=completed_at)
            fresh = bool(fresh) and accepted is not None
        try:
            shot = self.observation(accepted)
        except (ValueError, RuntimeError):
            self.invalidate()
            shot = {"screen_id": None, "image_base64": None}
        after = base64.b64decode(shot["image_base64"]) if shot["image_base64"] else None
        # Tap's pixel check and this diff must describe the same accepted frame.
        if accepted_frame is not None and after != accepted_frame:
            fresh = False
        usable = bool(fresh) and self.screen.live and after is not None
        if not usable:
            # An old screenshot may explain uncertainty, but cannot authorize the next input.
            self.invalidate()
            shot["screen_id"] = None
        diff = None
        diff_started = time.perf_counter()
        if include_diff:
            diff = await screen_diff.summarize(before, after, fresh=usable, ocr_enabled=ocr_diff)
        captured = time.perf_counter()
        timings = {"validation": ready - began, "device_and_pointer": device_done - ready,
                   "wait_fresh": fresh_done - device_done, "wait_settled": settled_done - fresh_done,
                   "capture": captured - settled_done, "total": captured - began}
        tap_timing = extra.pop("timing_ms", {})
        if tap_timing:
            tap_timing["diff"] = round(tap_timing.get("diff", 0) + (captured - diff_started) * 1000, 2)
        return {**shot, "command_acknowledged": acknowledged, "fresh": usable,
                "changed": (extra["screen_changed"] if usable else None) if tap_checked else (
                    vision.changed(before, after) if usable else None),
                "screen_diff": diff,
                "effect_verified": False, "error": error, "action_may_have_executed": True,
                "verification_required": "Inspect the returned image. Never retry a send automatically.",
                **extra,
                "timing_ms": {**{key: round(value * 1000, 1) for key, value in timings.items()},
                              **{key: value for key, value in tap_timing.items() if key != "total"},
                              **({"tap_total": tap_timing["total"]} if "total" in tap_timing else {})}}

    async def perform(self, request):
        began = time.perf_counter()
        observed_at = self.ticket[1] if self.ticket else 0
        before, width, height = self.consume(request.screen_id)
        plan = self._plan(request, width, height)
        plan["target_frame"] = before
        plan["observation"] = self._observation
        await self._resolve_text(plan, before, observed_at)
        ready = time.perf_counter()
        completed_at, acknowledged, error = None, False, None
        try:
            completed_at = await self._execute(plan)
            acknowledged = True
        except (RuntimeError, OSError) as exc:
            error = str(exc)
        return await self._respond(before, completed_at or time.time(), began, ready, acknowledged, error,
                                   plan["extra"], request.include_diff, request.ocr_diff)

    async def perform_batch(self, screen_id, requests, *, include_diff=True, ocr_diff=False):
        if not 1 <= len(requests) <= 8:
            raise ValueError("batch needs 1..8 actions; split steps that depend on a new screenshot")
        began = time.perf_counter()
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
        for index, plan in enumerate(plans):
            current = fresh_frame(self.screen)
            if (current is None or current.generation != plan["observation"].generation
                    or screen_compare.observation_changed(before, current.frame)):
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
            except (RuntimeError, OSError) as exc:
                error = str(exc)
                if len(done) == index:
                    done.append({"action": plan["name"], "ok": False, "error": error})
                else:
                    # The command was acknowledged; only the between-step check failed.
                    stopped_before = index + 2
                break
        acknowledged = error is None and len(done) == len(plans)
        result = await self._respond(before, completed_at or time.time(), began, ready, acknowledged, error,
                                     last_extra, include_diff, ocr_diff)
        result["steps"] = done
        result["completed"] = sum(1 for step in done if step.get("ok"))
        result["stopped_before_step"] = stopped_before
        return result

    async def _batch_boundary(self, previous, following, before, completed_at):
        # Pixel changes cannot prove a tap focused the intended control.
        if previous["name"] in {"tap", "tap_text"} and not previous["extra"].get("effect_verified"):
            return "tap effect is unverified; observe again before continuing the batch"
        if following["name"] in {"type", "compose_chinese", "key"}:
            return "typing and keyboard actions require a new observation; observe again before continuing"
        if not await feedback.wait_after_action(self.screen, completed_at):
            return "batch outcome has no fresh evidence; observe again before continuing"
        current = fresh_frame(self.screen, since=completed_at)
        if (current is None or current.generation != previous["observation"].generation
                or screen_compare.observation_changed(before, current.frame)):
            return "batch context changed or is stale; observe again before continuing"
        return None
