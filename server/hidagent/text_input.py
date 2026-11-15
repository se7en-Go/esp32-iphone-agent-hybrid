"""文本输入模式与剪贴板提供者接口。HID 本身没有剪贴板通道。"""

from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

TypeMode = Literal["auto", "paste", "keys"]
PASTE_SETUP = "请先按 docs/PASTE_INPUT.md 配置 iOS 快捷指令或伴侣应用剪贴板通道。"
STAGED_PASTE_REQUIRED = ("当前剪贴板仅支持分阶段粘贴；请使用 "
                         "paste-prepare → paste-status/observe → paste → paste-confirm"
                         "（MCP 对应 iphone_paste_*），不能使用 type 的同步 paste 模式。详见 docs/PASTE_INPUT.md。")


class ClipboardWriter(Protocol):
    async def paste(self, text: str, *, stopped: Callable[[], bool],
                    send: Callable[[str], Awaitable[None]]) -> None:
        """触发复制、确认焦点、发送粘贴键，并等待文本已插入的确认。"""
        ...


def clipboard_mode_options(provider) -> dict[str, bool]:
    """A configured staged companion does not imply legacy synchronous support."""
    return {"clipboard_available": provider is not None and getattr(provider, "legacy_paste_available", True),
            "clipboard_staged_available": callable(getattr(provider, "stage_begin", None))}


def select_type_mode(text: str, mode: TypeMode = "auto", *, clipboard_available: bool = False,
                     clipboard_staged_available: bool = False) -> TypeMode:
    if mode not in {"auto", "paste", "keys"}:
        raise ValueError("输入模式只能是 auto、paste 或 keys。")
    if not text or len(text) > 4000:
        raise ValueError("输入文本长度须为 1–4000 个字符。")
    if mode == "paste" and not clipboard_available and clipboard_staged_available:
        raise ValueError(STAGED_PASTE_REQUIRED)
    paste = mode == "paste" or (mode == "auto" and clipboard_available)
    staged_auto = mode == "auto" and clipboard_staged_available and not clipboard_available
    if any((ord(c) < 32 and not ((paste or staged_auto) and c in "\n\r\t"))
           or 127 <= ord(c) < 160 or 0xD800 <= ord(c) <= 0xDFFF
           or c in "\u2028\u2029" for c in text):
        raise ValueError("不能包含控制字符；仅粘贴模式允许换行和制表符，发送请单独操作。")
    if paste:
        if not clipboard_available:
            raise ValueError("尚未配置粘贴输入。" + PASTE_SETUP)
        return "paste"
    if staged_auto and (not text.isascii() or any(c in "\n\r\t" for c in text)):
        raise ValueError(STAGED_PASTE_REQUIRED)
    if not text.isascii():
        raise ValueError("HID 键盘只能直接输入 ASCII（含数字），不能直接输入中文。" + PASTE_SETUP)
    return "keys"
