"""AssistiveTouch Home navigation, with bounded visual checks and HID fallbacks."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat

from . import assistive_vision
from .device import DeviceError

log = logging.getLogger(__name__)

Point = tuple[float, float]
FindText = Callable[[bytes, str], Point | None]
# Panel-relative icon centres; override "home" for a customized top-level menu.
DEFAULT_LAYOUT = {"home": (0.5, 0.82)}
MENU_WAIT = 3.0
RESULT_WAIT = 3.0
STABLE_QUIET = 0.2


def _point(value) -> Point:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("辅助触控坐标需要 [x, y]，范围为 0 到 1。")
    x, y = map(float, value)
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in (x, y)):
        raise ValueError("辅助触控坐标必须是 0 到 1 之间的有限数值。")
    return x, y


def _aspect(frame: bytes) -> float:
    with Image.open(io.BytesIO(frame)) as image:
        return image.width / image.height


def _changed(before: bytes, after: bytes, ignore=()) -> bool:
    """Ignore the clock, floating button and pointer when checking navigation."""
    images = []
    for frame in (before, after):
        with Image.open(io.BytesIO(frame)) as image:
            image = image.convert("L").resize((96, 208))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 96, 17), fill=0)
        for x, y in ignore:
            draw.ellipse((x * 96 - 10, y * 208 - 10, x * 96 + 10, y * 208 + 10), fill=0)
        images.append(image)
    return ImageStat.Stat(ImageChops.difference(*images)).mean[0] > 2.0


def _quiet_view(frame: bytes, result):
    """Compare menu content in panel coordinates; other checks use the screen."""
    panel = result if isinstance(result, tuple) and len(result) == 4 else None
    with Image.open(io.BytesIO(frame)) as image:
        image = image.convert("RGB")
    if panel is not None:
        left, top, right, bottom = panel
        image = image.crop((round(left*image.width), round(top*image.height),
                            round(right*image.width), round(bottom*image.height)))
    return panel, image.resize((96, 96) if panel else (96, 208))


def _same_view(before, after) -> bool:
    old_panel, old_image = before
    panel, image = after
    if old_panel != panel:
        if old_panel is None or panel is None or max(abs(a-b) for a, b in zip(old_panel, panel, strict=True)) > .003:
            return False
    return max(ImageStat.Stat(ImageChops.difference(old_image, image)).mean) <= 2.0


class AssistiveTouch:
    def __init__(self, phone, path: Path, *, position: Point | None = None,
                 layout: Mapping[str, Point] | None = None, find_text: FindText | None = None):
        self.phone, self.path, self.find_text = phone, path, find_text
        self.last_position = None
        self.last_aspect = None
        self._clock = None
        saved = {}
        try:
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    raise ValueError("expected an object")
                if saved.get("last_position") is not None:
                    self.last_position = _point(saved["last_position"])
                    self.last_aspect = float(saved["aspect"])
                    if not math.isfinite(self.last_aspect) or self.last_aspect <= 0:
                        raise ValueError("invalid aspect")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            log.warning("AssistiveTouch: 无法读取位置记录，重新检测：%s", exc)
            saved, self.last_position, self.last_aspect = {}, None, None
        override = os.environ.get("HIDAGENT_ASSISTIVE_TOUCH_POSITION", "").strip()
        configured = position if position is not None else override.split(",") if override else saved.get("position")
        self.position = _point(configured) if configured is not None else None
        self.layout = {**DEFAULT_LAYOUT, **(saved.get("layout") or {}), **(layout or {})}
        self.layout = {name: _point(point) for name, point in self.layout.items()}
        self._saved = saved

    def _check(self, epoch: int) -> None:
        if self.phone._stopped(epoch):
            raise DeviceError("已紧急停止，后面的内容没有发出。")

    def _frame(self) -> bytes | None:
        screen = self.phone.screen
        if screen is None or not screen.live:
            return None
        age = self.phone._frame_age(self._clock)
        return screen.frame if age is None or age <= 5 else None

    def _remember(self, position: Point, frame: bytes) -> None:
        self.last_position, self.last_aspect = position, _aspect(frame)
        try:
            self.path.write_text(json.dumps({**self._saved, "last_position": position,
                                             "aspect": self.last_aspect}), encoding="utf-8")
        except OSError as exc:
            log.warning("AssistiveTouch: 无法保存悬浮按钮位置：%s", exc)

    async def _wait(self, epoch: int, since: float, timeout: float, accept, *, stable=False):
        deadline = time.monotonic() + timeout
        candidate = reference = previous = None
        quiet_since = None
        while time.monotonic() < deadline:
            self._check(epoch)
            if self.phone.screen is None or not self.phone.screen.live:
                return None
            await self.phone.screen.fresh(since, min(0.05, max(0, deadline - time.monotonic())))
            self._check(epoch)
            updated = self.phone.screen.updated
            current = self._frame()
            if current is None:
                return None
            # A decoder update can race fresh()'s timeout; inspect the latest frame.
            if updated > since:
                frame = current
                since = updated
                if frame != previous:
                    previous = frame
                    result = accept(frame)
                    if not result:
                        candidate = reference = quiet_since = None
                    elif stable:
                        view = _quiet_view(frame, result)
                        if reference is None or not _same_view(reference, view):
                            reference, quiet_since = view, time.monotonic()
                        candidate = frame, result
                    else:
                        candidate = frame, result
            now = time.monotonic()
            if now >= deadline:
                return None
            if (candidate and current == candidate[0] and self._frame() == current
                    and (not stable or now - quiet_since >= STABLE_QUIET)):
                return candidate
            await asyncio.sleep(0.02)
        return None

    def _home_slot(self, frame: bytes, panel) -> Point:
        left, top, right, bottom = panel
        if self.find_text is not None:
            for label in ("主屏幕", "Home"):
                try:
                    found = self.find_text(frame, label)
                    if found is not None:
                        x, y = _point(found)
                        if left < x < right and top < y < bottom:
                            return x, y
                except Exception:
                    log.warning("AssistiveTouch: 文字定位失败，使用菜单布局")
        x, y = self.layout["home"]
        return left + x * (right - left), top + y * (bottom - top)

    async def _assistive_home(self, epoch: int) -> bool:
        before = self._frame()
        if before is None:
            log.info("go_home: 没有实时画面，跳过 AssistiveTouch")
            return False
        hint = self.position
        if hint is None and self.last_aspect and abs(_aspect(before) / self.last_aspect - 1) < 0.1:
            hint = self.last_position
        button = assistive_vision.find_button(before, near=hint)
        if self.position is not None:
            if button is None or math.dist(button, self.position) > .025:
                log.info("go_home: 配置位置没有可见的 AssistiveTouch 悬浮按钮")
                return False
            button = self.position
        if button is None:
            log.info("go_home: 没找到 AssistiveTouch 悬浮按钮")
            return False
        self._check(epoch)
        since = time.time()
        await self.phone.tap(*button)
        opened = await self._wait(epoch, since, MENU_WAIT,
                                  lambda frame: assistive_vision.find_panel(frame, before=before), stable=True)
        if opened is None:
            log.warning("go_home: AssistiveTouch 菜单未出现或未稳定")
            return False
        menu, panel = opened
        self._remember(button, before)
        target = self._home_slot(menu, panel)
        self._check(epoch)
        since = time.time()
        await self.phone.tap(*target)
        ignore = tuple(point for point in (button, self.last_position, target) if point is not None)

        def arrived(frame):
            return (assistive_vision.find_panel(frame) is None
                    and _changed(before, frame, ignore))

        verified = await self._wait(epoch, since, RESULT_WAIT, arrived, stable=True)
        if verified:
            log.info("go_home: AssistiveTouch 主屏幕已点击，菜单消失且画面已变化")
            return True
        log.warning("go_home: AssistiveTouch 返回主屏幕未确认，尝试 HID 退路")
        return False

    async def go_home(self) -> None:
        epoch = getattr(self.phone.device, "abort_epoch", 0)
        self._check(epoch)
        self._clock = None
        reference = None
        try:
            if self._frame() is not None:
                self._clock = await self.phone._catch_up(epoch)
            reference = self._frame()
            if await self._assistive_home(epoch):
                return
        except (RuntimeError, OSError, ValueError) as exc:
            self._check(epoch)
            log.warning("go_home: AssistiveTouch 失败，尝试 HID 退路：%s", exc)
        acknowledged = False
        last_error = None
        for route in ("swipe-up", "homescreen", "cmd+h"):
            self._check(epoch)
            before, since = reference or self._frame(), time.time()
            log.warning("go_home: HID 退路 %s", route)
            try:
                if route == "swipe-up":
                    await self.phone.swipe(0.5, 0.995, 0.5, 0.6, hold=False)
                else:
                    await self.phone._send(epoch, "homescreen" if route == "homescreen" else "key cmd+h")
                acknowledged = True
            except (RuntimeError, OSError) as exc:
                self._check(epoch)
                last_error = exc
                log.warning("go_home: %s 失败：%s", route, exc)
                continue
            self._check(epoch)
            if before is not None:
                ignore = tuple(point for point in (self.position, self.last_position, (0.5, 0.6)) if point)
                verified = await self._wait(
                    epoch, since, RESULT_WAIT,
                    lambda frame, before=before, ignore=ignore:
                        assistive_vision.find_panel(frame) is None and _changed(before, frame, ignore), stable=True)
                if verified:
                    log.info("go_home: %s 后画面已变化，HID 结果仍需观察确认主屏幕", route)
                    return
        if not acknowledged and last_error is not None:
            raise last_error
        if self._frame() is not None:
            raise RuntimeError("已尝试返回主屏幕，但画面未确认变化；请观察屏幕。")
        log.warning("go_home: HID 命令已发送；没有实时画面，无法确认主屏幕")
