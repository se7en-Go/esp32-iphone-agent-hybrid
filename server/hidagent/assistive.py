"""AssistiveTouch Home navigation, with bounded visual checks and HID fallbacks."""

from __future__ import annotations

import asyncio
import inspect
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
from .freshness import FrameSnapshot, fresh_frame, require_fresh, snapshot
from .navigation_context import NavigationContext, from_navigation
from .operation import remaining_operation_time
from .text_target import PointerLandingError, TargetChangedError

log = logging.getLogger(__name__)

Point = tuple[float, float]
FindText = Callable[[bytes, str], Point | None]
# Panel-relative icon centres; override "home" for a customized top-level menu.
DEFAULT_LAYOUT = {"home": (0.5, 0.82)}
MENU_WAIT = 3.0
RESULT_WAIT = 3.0
STABLE_QUIET = 0.2
LATE_HOME_WAIT = 3.2


class HomeUnconfirmed(RuntimeError):
    """An opted-in terminal menu contact was ACKed but Home is unconfirmed."""

    def __init__(self, *, clicked_at, device, link, epoch, generation, sequence, last_input_at):
        super().__init__("辅助触控菜单已打开，但未确认主屏幕图标或返回结果；请重新观察。")
        self.clicked_at = clicked_at
        self.device, self.link, self.epoch = device, link, epoch
        self.generation, self.sequence, self.last_input_at = generation, sequence, last_input_at

    def same_contact(self, phone, screen) -> bool:
        current = snapshot(screen)
        return (type(self.sequence) is int and self.sequence > 0
                and isinstance(self.last_input_at, (int, float))
                and math.isfinite(self.last_input_at) and self.last_input_at <= self.clicked_at
                and phone.device is self.device and getattr(self.device, "links", None) == self.link
                and getattr(self.device, "abort_epoch", None) == self.epoch
                and getattr(self.device, "input_sequence", None) == self.sequence
                and getattr(self.device, "last_input_at", None) == self.last_input_at
                and current.live and current.generation == self.generation)


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


def _positive_u32(value) -> int | None:
    return value if type(value) is int and 0 < value < 2**32 else None


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

    def _device_key(self) -> str:
        return (getattr(self.phone.device, "port_name", "") or "").strip()

    def _ble_pairing_facts(self) -> tuple[dict, dict, bool, int | None, int | None]:
        hid = (getattr(self.phone.device, "board_status", None) or {}).get("hid")
        hid = hid if isinstance(hid, dict) else {}
        ble = hid.get("ble")
        ble = ble if isinstance(ble, dict) else {}
        has_peer_fields = "peer" in ble and "pair_seq" in ble
        return hid, ble, has_peer_fields, _positive_u32(ble.get("peer")), _positive_u32(ble.get("pair_seq"))

    def _verified_middle_click_bindings(self) -> dict[str, dict[str, int]]:
        raw = self._saved.get("middle_click_home_bindings")
        if not isinstance(raw, dict):
            return {}
        cleaned: dict[str, dict[str, int]] = {}
        for key, entry in raw.items():
            name = str(key).strip()
            if not name or not isinstance(entry, Mapping):
                continue
            peer = _positive_u32(entry.get("peer"))
            pair_seq = _positive_u32(entry.get("pair_seq"))
            if peer is not None and pair_seq is not None:
                cleaned[name] = {"peer": peer, "pair_seq": pair_seq}
        return cleaned

    def _sync_middle_click_binding(self) -> None:
        """Drop legacy port-only records or bindings invalidated by firmware downgrade or BLE re-pairing."""
        changed = False
        for legacy in ("middle_click_home_devices", "middle_click_home_verified"):
            if legacy in self._saved:
                self._saved.pop(legacy, None)
                changed = True
        bindings = self._verified_middle_click_bindings()
        raw = self._saved.get("middle_click_home_bindings")
        if raw is not None and raw != bindings:
            if bindings:
                self._saved["middle_click_home_bindings"] = bindings
            else:
                self._saved.pop("middle_click_home_bindings", None)
            changed = True
        key = self._device_key()
        hid, ble, has_peer_fields, peer, pair_seq = self._ble_pairing_facts()
        if key in bindings and hid:
            saved = bindings[key]
            active_ble = ble.get("conn") == 1 or hid.get("path") == "ble"
            invalid = (
                hid.get("middle_click") != 1
                or not has_peer_fields
                or ble.get("bond") == 0
                or ble.get("pair") == 1
                or pair_seq != saved["pair_seq"]
                or (active_ble and peer != saved["peer"])
            )
            if invalid:
                bindings.pop(key, None)
                if bindings:
                    self._saved["middle_click_home_bindings"] = bindings
                else:
                    self._saved.pop("middle_click_home_bindings", None)
                if self._saved.get("verified_route") == "middle-click":
                    self._saved.pop("verified_route", None)
                changed = True
        if not bindings and self._saved.get("verified_route") == "middle-click":
            self._saved.pop("verified_route", None)
            changed = True
        if changed:
            try:
                self._write_profile()
            except OSError as exc:
                log.warning("AssistiveTouch: 无法更新中键绑定状态：%s", exc)

    def _middle_click_supported_by_firmware(self) -> bool:
        hid, _, has_peer_fields, _, _ = self._ble_pairing_facts()
        return hid.get("path") == "ble" and hid.get("middle_click") == 1 and has_peer_fields

    def _middle_click_verified_for_device(self) -> bool:
        self._sync_middle_click_binding()
        key = self._device_key()
        if not key:
            return False
        saved = self._verified_middle_click_bindings().get(key)
        if saved is None:
            return False
        hid, ble, has_peer_fields, peer, pair_seq = self._ble_pairing_facts()
        if (not hid or not has_peer_fields or hid.get("middle_click") != 1
                or hid.get("path") != "ble" or ble.get("conn") != 1
                or ble.get("bond") != 1 or ble.get("pair") != 0):
            return False
        return peer == saved["peer"] and pair_seq == saved["pair_seq"]

    def _middle_click_reserved_for_clipboard(self) -> bool:
        return getattr(getattr(self.phone, "clipboard", None), "trigger_key", None) == "middleclick"

    def _middle_click_home_enabled(self) -> bool:
        """Enable mouse middle-button Home only when firmware, BLE peer id, and pairing generation all match."""
        return (not self._middle_click_reserved_for_clipboard()
                and self._middle_click_supported_by_firmware()
                and self._middle_click_verified_for_device())

    def middle_click_home_status(self) -> dict:
        self._sync_middle_click_binding()
        hid, ble, has_peer_fields, peer, pair_seq = self._ble_pairing_facts()
        supported = hid.get("middle_click") == 1 and has_peer_fields
        ble_active = (
            hid.get("path") == "ble"
            and ble.get("conn") == 1
            and ble.get("bond") == 1
            and ble.get("pair") == 0
        )
        device_key = self._device_key()
        bindings = self._verified_middle_click_bindings()
        verified_for_device = bool(device_key) and device_key in bindings
        reserved = self._middle_click_reserved_for_clipboard()
        return {
            "supported_by_firmware": supported,
            "ble_active": ble_active,
            "device_key": device_key or None,
            "ble_peer": peer,
            "ble_pair_seq": pair_seq,
            "verified_for_device": verified_for_device,
            "verified_devices_count": len(bindings),
            "reserved_for_clipboard": reserved,
            "enabled": (not reserved) and supported and ble_active and self._middle_click_verified_for_device(),
            "command": "middleclick",
            "preferred_route": self._saved.get("verified_route"),
        }

    def set_middle_click_home_verified(self, verified: bool) -> None:
        """Persist or clear verified middle-button Home scoped by serial port, BLE peer ID, and pair_seq."""
        key = self._device_key()
        bindings = self._verified_middle_click_bindings()
        for legacy in ("middle_click_home_devices", "middle_click_home_verified"):
            self._saved.pop(legacy, None)
        if verified:
            if not key:
                raise ValueError("当前设备没有可识别的串口设备标识，无法启用中键回主屏幕。")
            _, _, has_peer_fields, peer, pair_seq = self._ble_pairing_facts()
            if not has_peer_fields or peer is None or pair_seq is None:
                raise ValueError("当前蓝牙连接缺少对端绑定标识（peer/pair_seq），无法启用中键回主屏幕。")
            bindings[key] = {"peer": peer, "pair_seq": pair_seq}
            self._saved["middle_click_home_bindings"] = bindings
            self._saved["verified_route"] = "middle-click"
        else:
            if key:
                bindings.pop(key, None)
            if bindings:
                self._saved["middle_click_home_bindings"] = bindings
            else:
                self._saved.pop("middle_click_home_bindings", None)
            if self._saved.get("verified_route") == "middle-click":
                self._saved.pop("verified_route", None)
        try:
            self._write_profile()
        except OSError as exc:
            log.warning("AssistiveTouch: 无法保存中键回主屏幕状态：%s", exc)

    def revoke_middle_click_home(self, *, all_devices: bool = False) -> dict:
        """Revoke verified middle-button Home for the current device or all devices."""
        for legacy in ("middle_click_home_devices", "middle_click_home_verified"):
            self._saved.pop(legacy, None)
        if all_devices:
            self._saved.pop("middle_click_home_bindings", None)
            if self._saved.get("verified_route") == "middle-click":
                self._saved.pop("verified_route", None)
            try:
                self._write_profile()
            except OSError as exc:
                log.warning("AssistiveTouch: 无法撤销所有设备的中键回主屏幕状态：%s", exc)
        else:
            self.set_middle_click_home_verified(False)
        return self.middle_click_home_status()

    async def _refresh_board_status(self) -> None:
        device = self.phone.device
        refresh = getattr(device, "refresh_hid_status", None)
        if callable(refresh):
            res = refresh()
            if inspect.isawaitable(res):
                await res

    async def check_middle_click_enable_preconditions(self, frame: bytes) -> None:
        """Verify device identity, firmware capability, bonded BLE peer identity, and open-app screen state."""
        await self._refresh_board_status()
        if not self._device_key():
            raise ValueError("当前设备未报告稳定的设备标识（port_name 为空），不能启用中键回主屏幕。")
        hid, ble, has_peer_fields, peer, pair_seq = self._ble_pairing_facts()
        if hid.get("middle_click") != 1 or not has_peer_fields:
            raise ValueError(
                "当前固件未声明支持独立中键命令及绑定标识（middle_click=1）；为防止旧固件误左键，拒绝启用。"
            )
        if hid.get("path") != "ble":
            raise ValueError("当前控制链路不是蓝牙 HID（path!=ble），iOS 辅助触控设备映射仅对蓝牙生效。")
        if ble.get("bond") != 1 or ble.get("conn") != 1 or ble.get("pair") != 0 or peer is None or pair_seq is None:
            raise ValueError("当前蓝牙 HID 未处于已绑定稳定连接状态（或配对窗口尚未关闭），不能启用中键回主屏幕。")
        if assistive_vision.is_home_visible(frame):
            raise ValueError("当前已在主屏幕；请先打开备忘录等普通 App 且关闭辅助触控菜单后再验证启用。")
        if assistive_vision.find_panel(frame) is not None:
            raise ValueError("辅助触控菜单当前已打开；请先关闭菜单后再验证中键回主屏幕。")

    async def _confirm_home_after_send(
        self,
        epoch: int,
        since: float,
        expected_device,
        expected_generation: int | None,
        *,
        link: int | None = None,
    ) -> bool:
        expected_link = getattr(expected_device, "links", 0) if link is None else link
        arrived = assistive_vision.is_home_visible
        verified = await self._wait(epoch, since, RESULT_WAIT, arrived, stable=True)
        refreshed = False
        if not verified and fresh_frame(self.phone.screen, clock=self._clock, since=since) is None:
            candidate = snapshot(self.phone.screen)
            ensure_obs = getattr(self.phone, "ensure_observation", None)
            if ensure_obs is not None and candidate.frame and arrived(candidate.frame):
                def home_candidate():
                    self.phone._check_operation()
                    self._check(epoch)
                    if self.phone.device is not expected_device:
                        raise DeviceError("返回主屏幕期间设备连接已变化，未继续操作；请重新观察。")
                    # Only wake admission may use an old receipt. A new source
                    # frame must still satisfy the original click's causal gate.
                    current = require_fresh(self.phone.screen, clock=self._clock, max_age=float("inf"))
                    if expected_generation is not None and current.generation != expected_generation:
                        raise RuntimeError("返回主屏幕期间画面连接已变化，请重新观察。")
                    if not arrived(current.frame):
                        raise RuntimeError("等待主屏幕新画面时页面已变化，未继续唤醒；请重新观察。")

                # An early Home frame can arrive inside the mirror allowance
                # and then stop updating. Reacquire once; never click it again.
                refreshed = True
                await ensure_obs(before_refresh=home_candidate)
                home_candidate()
                verified = await self._wait(epoch, since, RESULT_WAIT, arrived, stable=True)
                home_candidate()
        if not verified and not refreshed:
            def same_confirmation():
                self.phone._check_operation()
                self._check(epoch)
                if (self.phone.device is not expected_device
                        or getattr(expected_device, "links", 0) != expected_link
                        or getattr(expected_device, "abort_epoch", 0) != epoch):
                    raise DeviceError("等待主屏幕结果期间设备连接已变化，未确认返回；请重新观察。")
                current = snapshot(self.phone.screen)
                if (not current.live
                        or (expected_generation is not None and current.generation != expected_generation)):
                    raise RuntimeError("等待主屏幕结果期间画面连接已变化，未确认返回；请重新观察。")

            same_confirmation()
            # A late transition may arrive after the first window. Observe once
            # more within the original budget, without another click or wake.
            budget = min(LATE_HOME_WAIT, remaining_operation_time() - .05)
            if budget >= STABLE_QUIET + .05:
                verified = await self._wait(epoch, since, budget, arrived, stable=True)
                same_confirmation()
        return bool(verified)

    async def verify_and_enable_middle_click_home(self, *, observation=None, before_write=None) -> float:
        """Send one `middleclick` from a non-Home app, confirm Home via fresh video, then persist."""
        if self._middle_click_reserved_for_clipboard():
            raise ValueError("中键当前已分配给剪贴板触发，不能同时启用中键回主屏幕。")
        device = self.phone.device
        epoch = getattr(device, "abort_epoch", 0)
        self._home_link, self._home_epoch = getattr(device, "links", 0), epoch
        self._check(epoch)
        self._clock = None
        self._generation = (
            observation.generation if observation is not None else snapshot(self.phone.screen).generation
        )
        ensure = getattr(self.phone, "ensure_observation", None)
        if ensure is not None:
            await ensure()
        initial_frame = self._require_frame().frame
        await self.check_middle_click_enable_preconditions(initial_frame)
        await self.phone._catch_up(epoch)
        self._clock = self.phone._clock
        current = self._require_frame()
        if assistive_vision.is_home_visible(current.frame):
            raise ValueError("当前已在主屏幕；请先打开备忘录等普通 App 且关闭辅助触控菜单后再验证启用。")
        if assistive_vision.find_panel(current.frame) is not None:
            raise ValueError("辅助触控菜单当前已打开；请先关闭菜单后再验证中键回主屏幕。")
        _, _, _, expected_peer, expected_pair_seq = self._ble_pairing_facts()

        def write_guard():
            if before_write is not None:
                before_write()
            self._require_frame()
            _, ble, _, peer_now, seq_now = self._ble_pairing_facts()
            if (not self._middle_click_supported_by_firmware()
                    or ble.get("conn") != 1 or ble.get("bond") != 1 or ble.get("pair") != 0
                    or peer_now != expected_peer or seq_now != expected_pair_seq):
                raise ValueError("写入前固件中键能力或蓝牙绑定状态已变化，未发送命令。")

        since = time.time()
        try:
            await self.phone._send(epoch, "middleclick", before_write=write_guard)
        except (DeviceError, OSError) as exc:
            self.revoke_middle_click_home()
            raise RuntimeError(f"ESP32 中键命令发送被拒绝或失败，未启用：{exc}") from exc

        self._check(epoch)
        verified = await self._confirm_home_after_send(epoch, since, device, self._generation)
        if not verified:
            self.revoke_middle_click_home()
            raise RuntimeError(
                "已发送 middleclick，但动作后新画面未确认返回主屏幕；已拒绝启用。"
                "请检查 iPhone「设置 → 辅助功能 → 触控 → 辅助触控 → 设备 → 按钮 3」是否已绑定「主屏幕」。"
            )
        await self._refresh_board_status()
        _, ble_after, _, peer_after, seq_after = self._ble_pairing_facts()
        if (not self._middle_click_supported_by_firmware()
                or ble_after.get("conn") != 1 or ble_after.get("bond") != 1 or ble_after.get("pair") != 0
                or peer_after != expected_peer or seq_after != expected_pair_seq):
            self.revoke_middle_click_home()
            raise RuntimeError("验证期间蓝牙绑定状态已变化，未启用中键回主屏幕；请重新观察并验证。")
        self.set_middle_click_home_verified(True)
        return since

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

    async def _assistive_home(self, epoch: int, navigation_context=None, *, report_unconfirmed=False) -> bool:
        device = self.phone.device
        if self._home_link is None:
            self._home_link, self._home_epoch = getattr(device, "links", 0), epoch
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
                                         navigation_context=navigation_context,
                                         report_unconfirmed=report_unconfirmed)
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
        if self.phone.device is not device:
            raise DeviceError("打开辅助触控菜单期间设备连接已变化，未继续操作；请重新观察。")
        menu, panel = opened
        self._remember(button, before)

        return await self._menu_home(epoch, menu, panel, observation=self._last_wait_observation,
                                     report_unconfirmed=report_unconfirmed)

    async def _menu_home(self, epoch: int, menu: bytes, panel, *, observation=None,
                         navigation_context=None, report_unconfirmed=False) -> bool:
        device = self.phone.device
        link = getattr(device, "links", 0)
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
        contact_sequence = None
        before_click = self._require_frame
        if report_unconfirmed:
            def before_click():
                nonlocal contact_sequence
                self._require_frame()
                # validate_click invokes this inside Device's write lock. Its
                # next synchronous step increments the sequence for this click.
                sequence = getattr(device, "input_sequence", None)
                contact_sequence = sequence + 1 if type(sequence) is int else None

        since = await self.phone.tap(*point, target_box=target_box, target_frame=menu,
                                     before_click=before_click, navigation_context=context)
        contact_input_at = getattr(device, "last_input_at", None)

        verified = await self._confirm_home_after_send(
            epoch, since, device, observation.generation, link=link
        )
        if verified:
            log.info("go_home: AssistiveTouch 主屏幕已确认：菜单消失、图标网格及 Dock 可见")
            return True
        if report_unconfirmed:
            raise HomeUnconfirmed(clicked_at=since, device=device, link=link, epoch=epoch,
                                  generation=observation.generation, sequence=contact_sequence,
                                  last_input_at=contact_input_at)
        log.warning("go_home: AssistiveTouch 返回主屏幕未确认，尝试 HID 退路")
        return False

    async def confirm_late_home(self, contact: HomeUnconfirmed) -> FrameSnapshot | None:
        """Wait passively for the terminal contact before a dependent shortcut."""
        screen = self.phone.screen
        budget = min(LATE_HOME_WAIT, remaining_operation_time() - .05)
        if budget < STABLE_QUIET + .05 or not contact.same_contact(self.phone, screen):
            return None
        arrived = await self._wait(contact.epoch, contact.clicked_at, budget,
                                   assistive_vision.is_home_visible, stable=True)
        current = fresh_frame(screen, clock=self._clock, since=contact.clicked_at)
        if (arrived is not None and current is not None and current.frame == arrived[0]
                and contact.same_contact(self.phone, screen)):
            return current
        return None

    async def go_home(self, *, navigation_context=None, report_unconfirmed=False) -> None:
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
        if assistive_vision.is_home_visible(self._require_frame().frame):
            return  # Home is idempotent; do not reopen its AssistiveTouch menu.
        context_kwargs = {} if navigation_context is None else {"navigation_context": navigation_context}
        if report_unconfirmed:
            context_kwargs["report_unconfirmed"] = True
        if assistive_vision.find_panel(self._require_frame().frame) is not None:
            # The user/host may already have opened the menu. Act on its visible
            # Home icon; do not reopen a bubble or run unrelated key/swipe routes.
            if await self._assistive_home(epoch, **context_kwargs):
                self._set_verified_route("assistive-touch")
                return
            raise RuntimeError("辅助触控菜单已打开，但未确认主屏幕图标或返回结果；请重新观察。")
        if self._device_key() and self._device_key() in self._verified_middle_click_bindings():
            await self._refresh_board_status()
            self._check(epoch)
        middle_enabled = self._middle_click_home_enabled()
        routes = (["middle-click"] if middle_enabled else []) + ["assistive-touch", "swipe-up", "homescreen", "cmd+h"]
        preferred = self._saved.get("verified_route")
        if middle_enabled:
            routes.remove("middle-click")
            routes.insert(0, "middle-click")
        elif preferred in routes:
            routes.remove(preferred)
            routes.insert(0, preferred)
        acknowledged = False
        last_error = None
        pointer_error = None
        pointer_rejected = False
        command_routes = {
            "middle-click": "middleclick",
            "homescreen": "homescreen",
            "cmd+h": "key cmd+h",
        }
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
            if route == "middle-click":
                log.info("go_home: 尝试已启用的 BLE 鼠标中键 (%s)", command_routes[route])
            else:
                log.warning("go_home: HID 退路 %s", route)
            try:
                # Start the effect window before the gesture. Release waits for its ACK,
                # and the mirror may publish the resulting frame during that wait.
                since = time.time()
                if route == "swipe-up":
                    await self.phone.swipe(0.5, 0.995, 0.5, 0.6, hold=False,
                                           before_action=self._require_frame)
                elif route == "middle-click":
                    def middle_write_guard():
                        self._require_frame()
                        if not self._middle_click_home_enabled():
                            raise DeviceError("蓝牙绑定或固件状态已变化，中键回主屏幕已失效；请重新验证。")

                    await self.phone._send(epoch, command_routes[route], before_write=middle_write_guard)
                else:
                    await self.phone._send(epoch, command_routes[route],
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
            if route == "middle-click":
                verified = await self._confirm_home_after_send(
                    epoch, since, self.phone.device, self._generation
                )
            else:
                verified = await self._wait(
                    epoch, since, RESULT_WAIT,
                    assistive_vision.is_home_visible, stable=True)
            if verified:
                log.info("go_home: %s 后已确认主屏幕图标网格及 Dock", route)
                self._set_verified_route(route)
                return
            if route == "middle-click":
                log.warning("go_home: middleclick 已发送但未确认主屏幕，回退常规路径")
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
