"""Conservative, observed keyboard evidence for non-send Enter actions."""

import time

from . import ocr
from .freshness import require_fresh

ENTER_EVIDENCE_TTL = 3.0
NAVIGATION_LABELS = {"搜索", "前往", "下一项", "下一步", "完成", "search", "go", "next", "done"}
EDITING_LABELS = {"换行", "newline"}


def _keyboard_return(boxes, intent):
    labels = NAVIGATION_LABELS if intent == "navigation" else EDITING_LABELS
    keys = [box for box in boxes if box.conf >= .9 and .65 <= box.center[1] <= .94
            and box.box[2] - box.box[0] <= .22 and box.box[3] - box.box[1] <= .1]
    returns = [box for box in keys if box.center[0] >= .78 and ocr.normalize_text(box.text) in labels]
    if len(returns) != 1:
        return False
    row = returns[0].center[1]
    aligned = [box for box in keys if abs(box.center[1] - row) <= .035]
    space = any(.25 <= box.center[0] <= .75 and ocr.normalize_text(box.text) in {"空格", "space"}
                for box in aligned)
    mode = any(box.center[0] <= .22 and ocr.normalize_text(box.text) in {"123", "abc", "#+="}
               for box in aligned)
    send = any(box.center[0] >= .78 and ocr.normalize_text(box.text) in {"发送", "send"}
               for box in aligned)
    return space and mode and not send


async def verify_enter_context(screen, before: bytes, intent: str, combo: str = "enter"):
    """Return a synchronous final-write validator; an intent string alone proves nothing."""
    if intent not in {"navigation", "editing"} or combo.strip().lower() != "enter":
        raise ValueError("非发送 Enter 只支持单独的 enter 和已观察的导航/换行键。")
    observed = require_fresh(screen)
    expires = time.monotonic() + ENTER_EVIDENCE_TTL
    if observed.frame != before:
        raise ValueError("键盘画面已变化，请重新观察后再按 Enter。")

    def validate():
        current = require_fresh(screen)
        if (time.monotonic() > expires or current.generation != observed.generation
                or current.frame != before):
            raise RuntimeError("Enter 的键盘或焦点证据已变化/过期，未发送按键。")

    boxes = await ocr.read_text_async(before)
    validate()
    if not _keyboard_return(boxes, intent):
        raise ValueError("未识别到完整键盘及对应的搜索/导航/换行键；不能把 Enter 当作非发送键。")
    return validate
