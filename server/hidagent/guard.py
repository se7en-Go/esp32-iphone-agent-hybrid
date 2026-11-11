"""系统级熔断。内容（说什么）只在 skill 文件里；这里管的是不许绕过的底线。

底线不能被 skill 放宽：停词至少包括「操作频繁 / 风控 / 验证码」，
发送间隔不短于 3 秒，每小时不超过 30 次。skill 只能更严。
只有声明看见了完整的新气泡，才把这一次计为发送成功。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from .paths import skill_roots

STOP_PHRASES = ("操作频繁", "风控", "验证码")
FLOOR_INTERVAL = 3.0
CEILING_PER_HOUR = 30


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
    def __init__(self, clock=time.time):
        self._clock = clock
        self.tripped = False
        self.reason = ""
        self.sends: list[float] = []

    def phrases(self, skill: str) -> tuple[str, ...]:
        extra = limits_for(skill)["extra_stop"]
        return STOP_PHRASES + tuple(extra)

    def observe(self, text: str, skill: str = "") -> dict:
        for phrase in self.phrases(skill):
            if phrase and phrase in (text or ""):
                self.tripped = True
                self.reason = phrase
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
                "reason": f"熔断还在（{self.reason}）。不要重试。重启服务也不会让这次风险提示消失，请先人工看手机。",
            }
        return {"stop": False, "counted": False, "allowed": True, "reason": ""}

    def confirm_send(self, *, bubble_visible: bool, observed_text: str, skill: str = "") -> dict:
        seen = self.observe(observed_text, skill)
        if seen["stop"]:
            return seen
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
        if not bubble_visible:
            return {
                "stop": False, "counted": False, "allowed": False,
                "reason": "屏幕上还没有完整的新消息气泡。输入框清空和收到 ok 都不算发送成功。",
            }
        self.sends.append(now)
        return {"stop": False, "counted": True, "allowed": True, "reason": ""}


def skill_file(skill: str) -> Path | None:
    if not skill or "/" in skill or "\\" in skill or skill.startswith("."):
        return None
    for root in skill_roots():
        path = root / skill / "SKILL.md"
        if path.is_file():
            return path
    return None
