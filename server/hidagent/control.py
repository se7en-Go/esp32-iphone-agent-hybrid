"""Observed, single-use device actions for external agents. No model calls.

`perform_batch` 在同一次观察上连续做多步，最后只回一张图。
选词、发送这种必须先看见结果的步骤不要放进同一次 batch。
"""

import base64
import io
import time
import uuid

from PIL import Image
from pypinyin import lazy_pinyin

from . import screen_compare, vision


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
        if ticket is None or ticket[0] != screen_id:
            raise ValueError("invalid or consumed screen_id; observe again before acting")
        if time.monotonic() - ticket[1] > 30:
            raise ValueError("screen_id expired; observe again")
        frame, width, height = self.capture()
        if (width, height) != ticket[3:] or screen_compare.observation_changed(ticket[2], frame):
            raise ValueError("screen changed since observation; observe again")
        return ticket[2], width, height

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
        if name in ("tap", "point", "scroll"):
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
            if (not request.text or not request.text.isascii()
                    or any(ord(c) < 32 or ord(c) == 127 for c in request.text)):
                raise ValueError("type requires printable ASCII; use compose_chinese for Chinese phrases")
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

    async def _execute(self, planned: dict) -> None:
        name = planned["name"]
        extra = planned["extra"]
        if name == "tap":
            result = await self.phone.tap_verified(*planned["xy"])
            if result is not None:
                extra.update(result)
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
            await self.phone.type(planned["text"])
        elif name == "compose_chinese":
            await self.phone.type(planned["text"])
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

    async def _respond(self, before, started, began, ready, acknowledged, error, extra):
        device_done = time.perf_counter()
        tap_checked = "screen_changed" in extra
        fresh = extra["fresh"] if tap_checked else await self.screen.fresh(started, 1.5)
        fresh_done = time.perf_counter()
        if not tap_checked:
            await self.screen.settled(least=0.3, most=2)
        settled_done = time.perf_counter()
        try:
            shot = self.observation()
        except ValueError:
            self.invalidate()
            shot = {"screen_id": None, "image_base64": None}
        captured = time.perf_counter()
        timings = {"validation": ready - began, "device_and_pointer": device_done - ready,
                   "wait_fresh": fresh_done - device_done, "wait_settled": settled_done - fresh_done,
                   "capture": captured - settled_done, "total": captured - began}
        return {**shot, "command_acknowledged": acknowledged, "fresh": bool(fresh),
                "changed": extra["screen_changed"] if tap_checked else (
                    vision.changed(before, self.screen.frame) if fresh else None),
                "effect_verified": False, "error": error, "action_may_have_executed": True,
                "verification_required": "Inspect the returned image. Never retry a send automatically.",
                "timing_ms": {key: round(value * 1000, 1) for key, value in timings.items()}, **extra}

    async def perform(self, request):
        began = time.perf_counter()
        before, width, height = self.consume(request.screen_id)
        plan = self._plan(request, width, height)
        ready = time.perf_counter()
        started, acknowledged, error = time.time(), False, None
        try:
            await self._execute(plan)
            acknowledged = True
        except (RuntimeError, OSError) as exc:
            error = str(exc)
        return await self._respond(before, started, began, ready, acknowledged, error, plan["extra"])

    async def perform_batch(self, screen_id, requests):
        if not 1 <= len(requests) <= 8:
            raise ValueError("batch needs 1..8 actions; split steps that depend on a new screenshot")
        began = time.perf_counter()
        before, width, height = self.consume(screen_id)
        plans = [self._plan(request, width, height) for request in requests]
        ready = time.perf_counter()
        started, acknowledged, error = time.time(), False, None
        done = []
        try:
            for plan in plans:
                await self._execute(plan)
                done.append({"action": plan["name"], "ok": True, **plan["extra"]})
            acknowledged = True
        except (RuntimeError, OSError) as exc:
            error = str(exc)
            if plans:
                done.append({"action": plans[len(done)]["name"], "ok": False, "error": error})
        result = await self._respond(before, started, began, ready, acknowledged, error,
                                     plans[-1]["extra"] if plans else {})
        result["steps"] = done
        result["completed"] = sum(1 for step in done if step.get("ok"))
        return result
