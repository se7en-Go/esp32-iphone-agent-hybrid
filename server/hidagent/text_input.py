"""文本输入模式与剪贴板提供者接口。HID 本身没有剪贴板通道。"""

from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

TypeMode = Literal["auto", "paste", "keys"]
PASTE_SETUP = "请先按 docs/PASTE_INPUT.md 配置 iOS 快捷指令或伴侣应用剪贴板通道。"


class ClipboardWriter(Protocol):
    async def paste(self, text: str, *, stopped: Callable[[], bool],
                    send: Callable[[str], Awaitable[None]]) -> None:
        """触发复制、确认焦点、发送粘贴键，并等待文本已插入的确认。"""
        ...


def select_type_mode(text: str, mode: TypeMode = "auto", *, clipboard_available: bool = False) -> TypeMode:
    if mode not in {"auto", "paste", "keys"}:
        raise ValueError("输入模式只能是 auto、paste 或 keys。")
    if not text or len(text) > 4000:
        raise ValueError("输入文本长度须为 1–4000 个字符。")
    if any(ord(c) < 32 or 127 <= ord(c) < 160 or 0xD800 <= ord(c) <= 0xDFFF
           or c in "\u2028\u2029" for c in text):
        raise ValueError("只接受单行文本，不能包含换行或控制字符；发送请单独操作。")
    if mode == "paste" or (mode == "auto" and clipboard_available):
        if not clipboard_available:
            raise ValueError("尚未配置粘贴输入。" + PASTE_SETUP)
        return "paste"
    if not text.isascii():
        raise ValueError("HID 键盘只能直接输入 ASCII（含数字），不能直接输入中文。" + PASTE_SETUP)
    return "keys"
