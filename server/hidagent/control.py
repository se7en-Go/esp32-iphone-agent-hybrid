"""Observed, single-use device actions for external agents. No model calls.

`perform_batch` 在同一次观察上连续做多步，最后只回一张图。
选词、发送这种必须先看见结果的步骤不要放进同一次 batch。
"""

import base64
import io
import time
import uuid
from dataclasses import asdict

from PIL import Image
from pypinyin import lazy_pinyin

from . import feedback, ocr, screen_compare, screen_diff, vision
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
        if not self.screen.live or not self.screen.frame:
            raise ValueError("no live screen; reconnect iPhone Screen Mirroring")
        frame = self.screen.frame
        with Image.open(io.BytesIO(frame)) as image:
            width, height = image.size
        return frame, width, height

    def observation(self):
        frame, width, height = self.capture()
        token = uuid.uuid4().hex
        self.ticket = (token, time.monotonic(), frame, width, height)
        return {"screen_id": token, "width": width, "height": height,
                "frame_received_at": self.screen.updated, "expires_in_seconds": 30,
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
        frame, width, height = self.capture()
        if (width, height) != ticket[3:] or screen_compare.observation_changed(ticket[2], frame):
            raise ValueError("screen changed since observation; observe again")
        return ticket[2], width, height

    async def find_text(self, screen_id, text, nth=1, exact=False):
        """只读取观察中的文字，不消耗操作凭据。"""
        before, _, _ = self._validate_ticket(self.ticket, screen_id)
        self._check_text_query(text, nth)
        matches = await ocr.find_matches_async(before, text, exact)
        self._validate_ticket(self.ticket, screen_id)
        return {"screen_id": screen_id, "query": text, "nth": nth, "exact": exact,
                "coordinate_space": "normalized", "matches": [asdict(match) for match in matches],
                "match": asdict(matches[nth - 1]) if nth <= len(matches) else None}

    @staticmethod
    def _check_text_query(text, nth):
        if not ocr.normalize_text(text):
            raise ValueError("查找文字不能为空。")
        if isinstance(nth, bool) or not isinstance(nth, int) or nth < 1:
            raise ValueError("nth 从 1 开始。")

    async def _resolve_text(self, plan, before, observed_at):
        if plan["name"] != "tap_text":
            return
        matches = await ocr.find_matches_async(before, plan["text"], plan["exact"])
        current, _, _ = self.capture()
        if time.monotonic() - observed_at > 30 or vision.changed(before, current):
            raise ValueError("OCR 期间屏幕已变化或观察已过期，请重新观察再点击。")
        if plan["nth"] > len(matches):
            raise ValueError(f"未找到第 {plan['nth']} 个匹配文字：{plan['text']}")
        match = matches[plan["nth"] - 1]
        plan["xy"] = match.center
        plan["before_click"] = TextTargetGuard(self.screen, before, match)
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
            planned.update(nth=request.nth, exact=request.exact)
        elif name in ("tap", "point", "scroll"):
            if request.x is None or request.y is None:
                raise ValueError("x and y required")
            planned["xy"] = point(request.x, request.y)
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

    async def _execute(self, planned: dict) -> float | None:
        name = planned["name"]
        extra = planned["extra"]
        if name in ("tap", "tap_text"):
            if name == "tap_text":
                result = await self.phone.tap_verified(*planned["xy"], before_click=planned["before_click"])
            else:
                result = await self.phone.tap_verified(*planned["xy"])
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
            await self.phone.swipe(x1, y1, x2, y2)
        elif name == "scroll":
            await self.phone.scroll(*planned["xy"], planned["lines"])
        elif name == "type":
            if planned["mode"] == "auto":
                await self.phone.type(planned["text"])
            else:
                await self.phone.type(planned["text"], mode=planned["mode"])
        elif name == "compose_chinese":
            await self.phone.type(planned["text"], mode="keys")
        elif name == "key":
            for _ in range(planned["repeat"]):
                await self.phone.key(planned["text"].lower())
                extra["repeat_completed"] += 1
        elif name == "home":
            await self.phone.go_home()
        elif name == "back":
            await self.phone.go_back()
        else:
            raise ValueError(f"unknown action {name}")

    async def _respond(self, before, completed_at, began, ready, acknowledged, error, extra,
                       include_diff=True, ocr_diff=True):
        device_done = time.perf_counter()
        tap_checked = "screen_changed" in extra
        fresh = extra["fresh"] if tap_checked else await feedback.wait_after_action(self.screen, completed_at)
        fresh_done = time.perf_counter()
        if not tap_checked:
            await self.screen.settled(least=0.3, most=2)
        settled_done = time.perf_counter()
        try:
            shot = self.observation()
        except ValueError:
            self.invalidate()
            shot = {"screen_id": None, "image_base64": None}
        after = base64.b64decode(shot["image_base64"]) if shot["image_base64"] else None
        usable = bool(fresh) and self.screen.live and after is not None
        diff = None
        if include_diff:
            diff = await screen_diff.summarize(before, after, fresh=usable, ocr_enabled=ocr_diff)
        captured = time.perf_counter()
        timings = {"validation": ready - began, "device_and_pointer": device_done - ready,
                   "wait_fresh": fresh_done - device_done, "wait_settled": settled_done - fresh_done,
                   "capture": captured - settled_done, "total": captured - began}
        return {**shot, "command_acknowledged": acknowledged, "fresh": usable,
                "changed": (extra["screen_changed"] if fresh else None) if tap_checked else (
                    vision.changed(before, after) if usable else None),
                "screen_diff": diff,
                "effect_verified": False, "error": error, "action_may_have_executed": True,
                "verification_required": "Inspect the returned image. Never retry a send automatically.",
                "timing_ms": {key: round(value * 1000, 1) for key, value in timings.items()}, **extra}

    async def perform(self, request):
        began = time.perf_counter()
        observed_at = self.ticket[1] if self.ticket else 0
        before, width, height = self.consume(request.screen_id)
        plan = self._plan(request, width, height)
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

    async def perform_batch(self, screen_id, requests, *, include_diff=True, ocr_diff=True):
        if not 1 <= len(requests) <= 8:
            raise ValueError("batch needs 1..8 actions; split steps that depend on a new screenshot")
        began = time.perf_counter()
        observed_at = self.ticket[1] if self.ticket else 0
        before, width, height = self.consume(screen_id)
        plans = [self._plan(request, width, height) for request in requests]
        if sum(plan.get("selected_mode") == "paste" for plan in plans) > 1:
            raise ValueError("一次 batch 只允许一次粘贴；请确认输入完成后重新观察再输入。")
        if len(plans) > 1 and any(plan["name"] == "tap_text" for plan in plans):
            raise ValueError("tap_text 需要单步执行；查看点击结果后再继续。")
        for plan in plans:
            await self._resolve_text(plan, before, observed_at)
        ready = time.perf_counter()
        completed_at, acknowledged, error = None, False, None
        done = []
        try:
            for plan in plans:
                completed_at = None
                completed_at = await self._execute(plan)
                done.append({"action": plan["name"], "ok": True, **plan["extra"]})
            acknowledged = True
        except (RuntimeError, OSError) as exc:
            error = str(exc)
            if plans:
                done.append({"action": plans[len(done)]["name"], "ok": False, "error": error})
        result = await self._respond(before, completed_at or time.time(), began, ready, acknowledged, error,
                                     plans[-1]["extra"] if plans else {}, include_diff, ocr_diff)
        result["steps"] = done
        result["completed"] = sum(1 for step in done if step.get("ok"))
        return result
