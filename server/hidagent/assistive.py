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

from PIL import Image, ImageChops, ImageStat

from . import assistive_vision
from .device import DeviceError
from .freshness import fresh_frame, require_fresh, snapshot
from .navigation_context import NavigationContext, from_navigation
from .text_target import PointerLandingError, TargetChangedError

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
        self._generation = None
        self._home_link = self._home_epoch = None
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
        if self._home_link is not None and getattr(self.phone.device, "links", 0) != self._home_link:
            raise DeviceError("返回主屏幕期间设备连接已变化，未继续操作；请重新观察。")

    def _frame(self, *, since=None) -> bytes | None:
        observation = fresh_frame(self.phone.screen, clock=self._clock, since=since)
        return observation.frame if observation is not None else None

    def _require_frame(self):
        if self._home_epoch is not None:
            self._check(self._home_epoch)
        observation = require_fresh(self.phone.screen, clock=self._clock)
        if self._generation is not None and observation.generation != self._generation:
            raise RuntimeError("返回主屏幕期间画面连接已变化，请重新观察。")
        return observation

    def _write_profile(self) -> None:
        body = dict(self._saved)
        if self.last_position is not None:
            body["last_position"] = list(self.last_position)
            body["aspect"] = self.last_aspect
        self.path.write_text(json.dumps(body), encoding="utf-8")

    def _remember(self, position: Point, frame: bytes) -> None:
        self.last_position, self.last_aspect = position, _aspect(frame)
        try:
            self._write_profile()
        except OSError as exc:
            log.warning("AssistiveTouch: 无法保存悬浮按钮位置：%s", exc)

    def _set_verified_route(self, route: str) -> None:
        self._saved["verified_route"] = route
        try:
            self._write_profile()
        except OSError as exc:
            log.warning("AssistiveTouch: 无法保存回主屏幕方式：%s", exc)

    def reset_routes(self) -> None:
        """Forget the preferred Home route; every attempt still needs visual confirmation."""
        self._saved.pop("verified_route", None)
        try:
            self._write_profile()
        except OSError as exc:
            log.warning("AssistiveTouch: 无法清除回主屏幕方式：%s", exc)

    async def _wait(self, epoch: int, since: float, timeout: float, accept, *, stable=False):
        deadline = time.monotonic() + timeout
        action_since = since
        candidate = reference = previous = None
        candidate_observation = self._last_wait_observation = None
        quiet_since = None
        while time.monotonic() < deadline:
            self._check(epoch)
            if (self._generation is not None
                    and snapshot(self.phone.screen).generation != self._generation):
                raise RuntimeError("返回主屏幕期间画面连接已变化，请重新观察。")
            if self.phone.screen is None or not self.phone.screen.live:
                return None
            await self.phone.screen.fresh(since, min(0.05, max(0, deadline - time.monotonic())))
            self._check(epoch)
            observation = fresh_frame(self.phone.screen, clock=self._clock, since=action_since)
            if observation is None:
                candidate = reference = quiet_since = None
                await asyncio.sleep(0.02)
                continue
            current, updated = observation.frame, observation.updated
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
                    if candidate is not None:
                        candidate_observation = observation
            now = time.monotonic()
            if now >= deadline:
                return None
            if (candidate and current == candidate[0] and self._frame() == current
                    and (not stable or now - quiet_since >= STABLE_QUIET)):
                self._last_wait_observation = candidate_observation
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

    def make_context(self, observation):
        """Use stored layout only as a hint for an actually visible system control."""
        panel = assistive_vision.find_panel(observation.frame)
        slot = self._home_slot(observation.frame, panel) if panel is not None else None
        hint = self.position
        if hint is None and self.last_aspect and abs(_aspect(observation.frame) / self.last_aspect - 1) < .1:
            hint = self.last_position
        context = from_navigation("home", observation, home_slot=slot, button_hint=hint)
        if (context is not None and context.kind == "bubble" and self.position is not None
                and math.dist(context.target.center, self.position) > .025):
            return None
        return context

    async def _assistive_home(self, epoch: int, navigation_context=None) -> bool:
        if self._home_link is None:
            self._home_link, self._home_epoch = getattr(self.phone.device, "links", 0), epoch
        self._check(epoch)
        observation = (navigation_context.observation if navigation_context is not None else
                       fresh_frame(self.phone.screen, clock=self._clock))
        if observation is None:
            log.info("go_home: 没有实时画面，跳过 AssistiveTouch")
            return False
        before = observation.frame
        panel = assistive_vision.find_panel(before)
        if panel is not None:
            return await self._menu_home(epoch, before, panel, observation=observation,
                                         navigation_context=navigation_context)
        hint = self.position
        if hint is None and self.last_aspect and abs(_aspect(before) / self.last_aspect - 1) < 0.1:
            hint = self.last_position
        context = navigation_context or from_navigation("home", observation, button_hint=hint)
        if context is None or context.kind != "bubble":
            log.info("go_home: 没找到 AssistiveTouch 悬浮按钮")
            return False
        button, button_box = context.target.center, context.target.box
        if self.position is not None:
            if button is None or math.dist(button, self.position) > .025:
                log.info("go_home: 配置位置没有可见的 AssistiveTouch 悬浮按钮")
                return False
            button = self.position
        if button is None:
            log.info("go_home: 没找到 AssistiveTouch 悬浮按钮")
            return False
        self._check(epoch)
        self._require_frame()
        context.validate_current(self.phone.screen)
        since = await self.phone.tap(*button, target_box=button_box, target_frame=before,
                                     before_click=self._require_frame, navigation_context=context)
        opened = await self._wait(epoch, since, MENU_WAIT,
                                  lambda frame: assistive_vision.find_panel(frame, before=before), stable=True)
        if opened is None:
            log.warning("go_home: AssistiveTouch 菜单未出现或未稳定")
            return False
        menu, panel = opened
        self._remember(button, before)

        return await self._menu_home(epoch, menu, panel, observation=self._last_wait_observation)

    async def _menu_home(self, epoch: int, menu: bytes, panel, *, observation=None, navigation_context=None) -> bool:
        device = self.phone.device
        if self._home_link is None:
            self._home_link, self._home_epoch = getattr(self.phone.device, "links", 0), epoch
        self._check(epoch)
        current = self._require_frame()
        observation = observation or current
        if observation.frame != menu:
            log.warning("go_home: 菜单观察已更换，未重新绑定旧目标")
            return False
        context = navigation_context or from_navigation("home", observation, home_slot=self._home_slot(menu, panel))
        if context is None or context.kind != "menu":
            log.warning("go_home: 主屏幕目标没有可见图标边界，未发送点击")
            return False
        point, target_box = context.target.center, context.target.box
        context.validate_current(self.phone.screen)
        since = await self.phone.tap(*point, target_box=target_box, target_frame=menu,
                                     before_click=self._require_frame, navigation_context=context)

        def arrived(frame):
            return (assistive_vision.find_panel(frame) is None
                    and assistive_vision.is_home_screen(frame))

        verified = await self._wait(epoch, since, RESULT_WAIT, arrived, stable=True)
        if not verified and fresh_frame(self.phone.screen, clock=self._clock, since=since) is None:
            candidate = snapshot(self.phone.screen)
            ensure = getattr(self.phone, "ensure_observation", None)
            if ensure is not None and candidate.frame and arrived(candidate.frame):
                def home_candidate():
                    self.phone._check_operation()
                    self._check(epoch)
                    if self.phone.device is not device:
                        raise DeviceError("返回主屏幕期间设备连接已变化，未继续操作；请重新观察。")
                    # Only wake admission may use an old receipt. A new source
                    # frame must still satisfy the original click's causal gate.
                    current = require_fresh(self.phone.screen, clock=self._clock, max_age=float("inf"))
                    if current.generation != observation.generation:
                        raise RuntimeError("返回主屏幕期间画面连接已变化，请重新观察。")
                    if not arrived(current.frame):
                        raise TargetChangedError("等待主屏幕新画面时页面已变化，未继续唤醒；请重新观察。")

                # An early Home frame can arrive inside the mirror allowance
                # and then stop updating. Reacquire once; never click it again.
                await ensure(before_refresh=home_candidate)
                home_candidate()
                verified = await self._wait(epoch, since, RESULT_WAIT, arrived, stable=True)
                home_candidate()
        if verified:
            log.info("go_home: AssistiveTouch 主屏幕已确认：菜单消失、图标网格及 Dock 可见")
            return True
        log.warning("go_home: AssistiveTouch 返回主屏幕未确认，尝试 HID 退路")
        return False

    async def go_home(self, *, navigation_context=None) -> None:
        epoch = getattr(self.phone.device, "abort_epoch", 0)
        self._home_link, self._home_epoch = getattr(self.phone.device, "links", 0), epoch
        self._check(epoch)
        self._clock = None
        # Observation acquisition can await a board response or a fresh frame.
        # Pin the caller's mirror session before that await, not its replacement.
        self._generation = snapshot(self.phone.screen).generation
        ensure = getattr(self.phone, "ensure_observation", None)
        if ensure is not None:
            await ensure()
        self._require_frame()
        if navigation_context is not None:
            if not isinstance(navigation_context, NavigationContext) or navigation_context.action != "home":
                raise ValueError("home requires a detected AssistiveTouch navigation context")
            navigation_context.validate_current(self.phone.screen)
        await self.phone._catch_up(epoch)
        # Freshness compares board timestamps with wall-clock screen.updated.
        # Catch-up returns a monotonic offset for its own arrival snapshots.
        self._clock = self.phone._clock
        def arrived(frame):
            return (assistive_vision.find_panel(frame) is None
                    and assistive_vision.is_home_screen(frame))

        if arrived(self._require_frame().frame):
            return  # Home is idempotent; do not reopen its AssistiveTouch menu.
        context_kwargs = {} if navigation_context is None else {"navigation_context": navigation_context}
        if assistive_vision.find_panel(self._require_frame().frame) is not None:
            # The user/host may already have opened the menu. Act on its visible
            # Home icon; do not reopen a bubble or run unrelated key/swipe routes.
            if await self._assistive_home(epoch, **context_kwargs):
                self._set_verified_route("assistive-touch")
                return
            raise RuntimeError("辅助触控菜单已打开，但未确认主屏幕图标或返回结果；请重新观察。")
        routes = ["assistive-touch", "swipe-up", "homescreen", "cmd+h"]
        preferred = self._saved.get("verified_route")
        if preferred in routes:
            routes.remove(preferred)
            routes.insert(0, preferred)
        acknowledged = False
        last_error = None
        pointer_error = None
        pointer_rejected = False
        for route in routes:
            self._check(epoch)
            self._require_frame()
            if pointer_rejected and route in {"assistive-touch", "swipe-up"}:
                continue
            if route == "assistive-touch":
                try:
                    if await self._assistive_home(epoch, **context_kwargs):
                        self._set_verified_route(route)
                        return
                except (PointerLandingError, TargetChangedError) as exc:
                    # A pre-write rejection of the pointer path does not invalidate
                    # a position-independent key on a still-eligible mirror session.
                    self._check(epoch)
                    self._require_frame()
                    pointer_rejected = True
                    last_error = exc
                    pointer_error = exc
                    log.warning("go_home: %s 失败：%s", route, exc)
                continue
            if route == "swipe-up" and not self.phone.calibrated:
                log.info("go_home: 指针未校准，跳过上滑退路")
                continue
            log.warning("go_home: HID 退路 %s", route)
            try:
                # Start the effect window before the gesture. Release waits for its ACK,
                # and the mirror may publish the resulting frame during that wait.
                since = time.time()
                if route == "swipe-up":
                    await self.phone.swipe(0.5, 0.995, 0.5, 0.6, hold=False,
                                           before_action=self._require_frame)
                else:
                    await self.phone._send(epoch, "homescreen" if route == "homescreen" else "key cmd+h",
                                           before_write=self._require_frame)
                acknowledged = True
            except (DeviceError, OSError, PointerLandingError, TargetChangedError) as exc:
                self._check(epoch)
                last_error = exc
                if isinstance(exc, (PointerLandingError, TargetChangedError)):
                    pointer_rejected = True
                    pointer_error = exc
                log.warning("go_home: %s 失败：%s", route, exc)
                continue
            self._check(epoch)
            verified = await self._wait(
                epoch, since, RESULT_WAIT,
                arrived, stable=True)
            if verified:
                log.info("go_home: %s 后已确认主屏幕图标网格及 Dock", route)
                self._set_verified_route(route)
                return
        if not acknowledged and last_error is not None:
            raise last_error
        if self._frame() is not None:
            message = "已尝试返回主屏幕，但未确认主屏幕图标网格及 Dock；请观察屏幕。"
        else:
            message = "HID 命令已发送；没有实时画面，无法确认主屏幕。"
        if pointer_error is not None:
            # A fallback ACK is not Home evidence. Keep the original actionable
            # pointer rejection even if a later keyboard route failed as well.
            raise RuntimeError(f"{message} 指针路径此前被拦截：{pointer_error}") from pointer_error
        raise RuntimeError(message)
