"""系统级熔断。内容（说什么）只在 skill 文件里；这里管的是不许绕过的底线。

底线不能被 skill 放宽：停词至少包括「操作频繁 / 风控 / 验证码」，
发送间隔不短于 3 秒，每小时不超过 30 次。skill 只能更严。
只有声明看见了完整的新气泡，才把这一次计为发送成功。
"""

from __future__ import annotations

import json
import math
import os
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .paths import skill_roots

STOP_PHRASES = ("操作频繁", "风控", "验证码")
FLOOR_INTERVAL = 3.0
CEILING_PER_HOUR = 30
RESERVATION_TTL = 30.0


@dataclass
class _EnterEvidence:
    validate: object
    used: bool = False


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    found: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        found[key.strip()] = value.strip().strip('"').strip("'")
    return found


def _skill_text(skill: str) -> str:
    if not skill or "/" in skill or "\\" in skill or skill.startswith("."):
        return ""
    for root in skill_roots():
        path = root / skill / "SKILL.md"
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return ""


def _clock_hm(text: str) -> tuple[int, int]:
    hour, _, minute = text.partition(":")
    return int(hour), int(minute or "0")


def limits_for(skill: str) -> dict:
    interval, per_hour = 8.0, 20
    quiet = True
    start, end = (23, 0), (7, 0)
    extra: list[str] = []
    meta = _frontmatter(_skill_text(skill))
    if "min_interval_seconds" in meta:
        interval = float(meta["min_interval_seconds"])
    if "max_per_hour" in meta:
        per_hour = int(float(meta["max_per_hour"]))
    if meta.get("quiet_hours") == "off":
        quiet = False
    elif "quiet_hours" in meta and "-" in meta["quiet_hours"]:
        left, right = meta["quiet_hours"].split("-", 1)
        start, end = _clock_hm(left), _clock_hm(right)
    if meta.get("extra_stop"):
        extra = [part.strip() for part in meta["extra_stop"].split(",") if part.strip()]
    return {
        "min_interval": max(FLOOR_INTERVAL, interval),
        "max_per_hour": min(CEILING_PER_HOUR, max(1, per_hour)),
        "quiet": quiet,
        "quiet_start": start,
        "quiet_end": end,
        "extra_stop": extra,
    }


def in_quiet(moment: datetime, start: tuple[int, int], end: tuple[int, int]) -> bool:
    minutes = moment.hour * 60 + moment.minute
    begin = start[0] * 60 + start[1]
    stop = end[0] * 60 + end[1]
    if begin == stop:
        return False
    if begin < stop:
        return begin <= minutes < stop
    return minutes >= begin or minutes < stop


class Guard:
    def __init__(self, clock=time.time, path: Path | None = None):
        self._clock = clock
        self._path = path
        # 同步路由/串口线程和 asyncio 共用；临界区内绝不能 await。
        # observe/confirm/save 及最终 HID 写入会嵌套进入，所以必须可重入。
        self._lock = threading.RLock()
        self._enter_evidence = ContextVar("verified_non_send_enter", default=None)
        with self._lock:
            self.tripped = False
            self.reason = ""
            self.sends: list[float] = []
            self._pending = 0
            self._reservation: tuple[str, float] | None = None
            self._load()

    @contextmanager
    def verified_enter(self, intent: str, validate):
        """Internal verifier scope, never a caller-supplied permission flag or token."""
        if intent not in {"navigation", "editing"}:
            raise ValueError("unknown Enter intent")
        validate()
        token = self._enter_evidence.set(_EnterEvidence(validate))
        try:
            yield
        finally:
            self._enter_evidence.reset(token)

    def enter_evidence(self):
        evidence = self._enter_evidence.get()
        if evidence is not None:
            self.check_enter_evidence(evidence)
        return evidence

    def check_enter_evidence(self, evidence, *, consume=False):
        """Recheck under the HID write lock, and allow exactly one proven Enter."""
        if evidence.used:
            raise RuntimeError("Enter 的观察已使用，请重新观察。")
        evidence.validate()
        if consume:
            evidence.used = True

    def _fail_closed(self, reason: str) -> None:
        self.tripped = True
        self.reason = reason
        self.sends = []

    def _load(self) -> None:
        with self._lock:
            if self._path is None or not self._path.is_file():
                return
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                self._fail_closed("guard.json 读不出来，按已熔断处理。")
                return
            sends = data.get("sends", []) if isinstance(data, dict) else None
            tripped = data.get("tripped") if isinstance(data, dict) else None
            if (
                not isinstance(data, dict)
                or "tripped" not in data
                or type(tripped) is not bool
                or not isinstance(sends, list)
            ):
                self._fail_closed("guard.json 内容无法核对，按已熔断处理。")
                return
            parsed: list[float] = []
            for item in sends:
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    self._fail_closed("guard.json 的发送时间损坏，按已熔断处理。")
                    return
                value = float(item)
                if not math.isfinite(value):
                    self._fail_closed("guard.json 的发送时间损坏，按已熔断处理。")
                    return
                parsed.append(value)
            self.sends = parsed
            # 重新加载也不能撤销内存中的熔断；只有 reset 能解除。
            if tripped:
                self.tripped = True
                self.reason = str(data.get("reason") or "")

    def _save(self) -> None:
        with self._lock:
            if self._path is None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"tripped": self.tripped, "reason": self.reason, "sends": self.sends},
                ensure_ascii=False,
            )
            temporary = self._path.with_suffix(".json.tmp")
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self._path)

    def reset(self) -> None:
        """只解除熔断。这一小时已经发出去的次数还在，重启服务也不会清掉。"""
        with self._lock:
            previous = self.tripped, self.reason
            self.tripped = False
            self.reason = ""
            try:
                self._save()
            except OSError:
                # 磁盘仍可能处于熔断状态；保存失败不能在内存里放行。
                self.tripped, self.reason = previous
                raise

    def phrases(self, skill: str) -> tuple[str, ...]:
        extra = limits_for(skill)["extra_stop"]
        return STOP_PHRASES + tuple(extra)

    def observe(self, text: str, skill: str = "") -> dict:
        with self._lock:
            for phrase in self.phrases(skill):
                if phrase and phrase in (text or ""):
                    self.tripped = True
                    self.reason = phrase
                    self._save()
                    return {
                        "stop": True,
                        "counted": False,
                        "allowed": False,
                        "reason": f"看到「{phrase}」，已熔断。不要重试，不要换一种说法再发。",
                    }
            if self.tripped:
                return {
                    "stop": True,
                    "counted": False,
                    "allowed": False,
                    "reason": f"熔断还在（{self.reason}）。不要重试。"
                              "重启服务也不会让这次风险提示消失，请先人工看手机。",
                }
            return {"stop": False, "counted": False, "allowed": False,
                    "reason": "这只是观察，还不是发送许可。发送前要先做频率和静默检查。"}

    def _rate_block(self, skill: str, *, consuming: bool = False) -> dict | None:
        """调用方持有 _lock。频率和静默不计入发送次数；None 表示这一刻可以发。"""
        if self.tripped:
            return {
                "stop": True, "counted": False, "allowed": False,
                "reason": f"熔断还在（{self.reason}）。不要重试。",
            }
        limits = limits_for(skill)
        now = self._clock()
        moment = datetime.fromtimestamp(now)
        if limits["quiet"] and in_quiet(moment, limits["quiet_start"], limits["quiet_end"]):
            return {
                "stop": False, "counted": False, "allowed": False,
                "reason": "现在是静默时段，这一次不发送。",
            }
        window = [stamp for stamp in self.sends if now - stamp < 3600]
        self.sends = window
        if consuming and self._reservation is not None:
            # prepare 的名额始终是最后一条；消费时不把自己的预留当作上一次发送。
            window = window[:-1]
        interval = limits["min_interval"]
        if window and now - window[-1] < interval:
            return {
                "stop": False, "counted": False, "allowed": False,
                "reason": f"距上一次成功发送不足 {interval:.0f} 秒。",
            }
        if len(window) >= limits["max_per_hour"]:
            return {
                "stop": False, "counted": False, "allowed": False,
                "reason": f"这一小时已经发送 {limits['max_per_hour']} 次。",
            }
        return None

    def _drop_expired_reservation(self) -> None:
        """调用方持有 _lock；过期名额仍保守留在 sends 中。"""
        if self._reservation is None:
            return
        _token, stamp = self._reservation
        if self._clock() - stamp > RESERVATION_TTL:
            self._reservation = None

    def prepare_send(self, skill: str = "") -> dict:
        """保守占一个一次性名额。Enter 或命名发送按钮消费时更新为实际发送时间。"""
        with self._lock:
            self._drop_expired_reservation()
            if self._reservation is not None:
                return {
                    "stop": False, "counted": False, "allowed": False,
                    "reason": "这一次已经占过名额，直接按发送键。",
                }
            blocked = self._rate_block(skill)
            if blocked:
                return blocked
            now = self._clock()
            token = secrets.token_hex(8)
            self.sends.append(now)
            self._reservation = (token, now)
            self._save()
            return {
                "stop": False, "counted": False, "allowed": True,
                "reason": "", "reservation": token,
            }

    def consume_reservation(self, skill: str = "") -> dict:
        """真正发送前原子结算：复用未过期的预留，或直接占用并消费一个名额。"""
        with self._lock:
            return self._consume_reservation(skill)

    def _consume_reservation(self, skill: str) -> dict:
        """调用方持有 _lock；检查、更新时间和销毁预留不能被另一发送插入。"""
        self._drop_expired_reservation()
        blocked = self._rate_block(skill, consuming=True)
        if blocked:
            return blocked
        now = self._clock()
        if self._reservation is not None:
            self.sends[-1] = now
            self._reservation = None
        else:
            self.sends.append(now)
        self._pending += 1
        self._save()
        return {"stop": False, "counted": False, "allowed": True, "reason": ""}

    def confirm_send(self, *, bubble_visible: bool, observed_text: str, skill: str = "") -> dict:
        with self._lock:
            seen = self.observe(observed_text, skill)
            if seen["stop"]:
                return seen
            if not bubble_visible:
                return {
                    "stop": False, "counted": False, "allowed": False,
                    "reason": "屏幕上还没有完整的新消息气泡。输入框清空和收到 ok 都不算发送成功。",
                }
            if not self._pending:
                # 兼容用户直接在手机上发送后确认；也消费之前的 prepare，不能再次复用。
                decision = self._consume_reservation(skill)
                if not decision["allowed"]:
                    return decision
            self._pending -= 1
            return {"stop": False, "counted": True, "allowed": True, "reason": ""}


def skill_file(skill: str) -> Path | None:
    if not skill or "/" in skill or "\\" in skill or skill.startswith("."):
        return None
    for root in skill_roots():
        path = root / skill / "SKILL.md"
        if path.is_file():
            return path
    return None
