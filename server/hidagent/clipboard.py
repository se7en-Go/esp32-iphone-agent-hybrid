"""一次性剪贴板交接；复制、焦点及粘贴完成都须明确确认。"""

import asyncio
import math
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, StrictBool

from .device import DeviceError

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class _CopyConfirmation(BaseModel):
    copied: StrictBool = False
    focused: StrictBool = False


class _PasteConfirmation(BaseModel):
    pasted: StrictBool = False


class _ResolveConfirmation(BaseModel):
    paste_finished: StrictBool = False


@dataclass(repr=False)
class _Pending:
    token: str
    text: str | None
    deadline: float
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    fetched: bool = False
    acknowledged: bool = False
    paste_started: bool = False
    paste_sent: bool = False
    completed: bool = False
    expired: bool = False


class CompanionClipboard:
    """单个待办；板子确认按键不等于 iPhone 已读取剪贴板。"""

    def __init__(self, base_url: str, key: str, ttl: float = 60, *, trigger_key: str | None = None):
        try:
            parsed = urlsplit(base_url)
            valid_url = (parsed.scheme in {"http", "https"} and parsed.hostname
                         and not parsed.username and not parsed.password
                         and not parsed.query and not parsed.fragment
                         and not any(c.isspace() for c in base_url))
            _port = parsed.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("HIDAGENT_CLIPBOARD_BASE_URL 需要手机可访问的 HTTP(S) 服务地址。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", key):
            raise ValueError("HIDAGENT_CLIPBOARD_KEY 需要 32..256 位随机字母、数字、下划线或减号。")
        if not math.isfinite(ttl) or not 1 <= ttl <= 300:
            raise ValueError("HIDAGENT_CLIPBOARD_TTL 需要在 1..300 秒之间。")
        if trigger_key is not None:
            parts = trigger_key.split("+")
            if (not re.fullmatch(r"(?:(?:ctrl|alt|shift|cmd)\+){1,3}[a-z0-9]", trigger_key)
                    or len(set(parts)) != len(parts) or trigger_key == "cmd+v"):
                raise ValueError("HIDAGENT_CLIPBOARD_TRIGGER_KEY 需要绑定快捷指令的组合键，例如 ctrl+alt+p。")
        self.base_url = base_url.rstrip("/")
        self._key = key
        self.ttl = ttl
        self.trigger_key = trigger_key
        self._pending: _Pending | None = None
        self._operation_active = False
        self._needs_resolution = False

    @classmethod
    def from_env(cls):
        base_url = os.environ.get("HIDAGENT_CLIPBOARD_BASE_URL", "").strip()
        key = os.environ.get("HIDAGENT_CLIPBOARD_KEY", "").strip()
        trigger_key = os.environ.get("HIDAGENT_CLIPBOARD_TRIGGER_KEY", "").strip() or None
        if not base_url and not key and trigger_key is None:
            return None
        if not base_url or not key:
            raise ValueError("请同时设置 HIDAGENT_CLIPBOARD_BASE_URL 和 HIDAGENT_CLIPBOARD_KEY。")
        try:
            ttl = float(os.environ.get("HIDAGENT_CLIPBOARD_TTL", "60"))
        except ValueError as exc:
            raise ValueError("HIDAGENT_CLIPBOARD_TTL 需要在 1..300 秒之间。") from exc
        return cls(base_url, key, ttl, trigger_key=trigger_key)

    def authorized(self, authorization: str) -> bool:
        return secrets.compare_digest(authorization.encode(), f"Bearer {self._key}".encode())

    def _expire(self, item: _Pending) -> None:
        item.text = None
        item.expired = True
        if (item.fetched or item.paste_started) and not item.completed:
            self._needs_resolution = True
        item.changed.set()
        if self._pending is item:
            self._pending = None

    def _active(self) -> _Pending | None:
        item = self._pending
        if item is not None and time.monotonic() >= item.deadline:
            self._expire(item)
            return None
        return item

    def _urls(self, item: _Pending) -> dict:
        url = f"{self.base_url}/api/clipboard/{item.token}"
        return {"status_url": f"{url}/status", "complete_url": f"{url}/complete",
                "expires_in_seconds": max(0, item.deadline - time.monotonic())}

    def pending(self) -> dict | None:
        item = self._active()
        if item is None or item.fetched:
            return None
        url = f"{self.base_url}/api/clipboard/{item.token}"
        return {"pending": True, "text_url": url, "ack_url": f"{url}/ack", **self._urls(item)}

    def current(self) -> dict:
        item = self._active()
        if item is None:
            return {"active": False, "needs_resolution": self._needs_resolution}
        return {"active": True, "paste_sent": item.paste_sent, **self._urls(item)}

    def _lookup(self, token: str) -> _Pending:
        item = self._active()
        if (item is None or not token.isascii()
                or not secrets.compare_digest(token, item.token) or item.completed):
            raise HTTPException(404, "剪贴板请求不存在、已使用或已过期。", headers=NO_STORE)
        return item

    def fetch(self, token: str) -> str:
        item = self._lookup(token)
        if item.fetched:
            raise HTTPException(404, "剪贴板内容已经领取，不能重复读取。", headers=NO_STORE)
        text = item.text
        item.text = None
        item.fetched = True
        return text

    def acknowledge(self, token: str, *, copied: bool, focused: bool) -> None:
        item = self._lookup(token)
        if item.acknowledged:
            raise HTTPException(404, "剪贴板复制已经确认，不能重复确认。", headers=NO_STORE)
        if not item.fetched or copied is not True or focused is not True:
            raise HTTPException(409, "请先复制到 iPhone 剪贴板并确认目标输入框有焦点。", headers=NO_STORE)
        item.acknowledged = True
        item.changed.set()

    def status(self, token: str) -> dict:
        return {"paste_sent": self._lookup(token).paste_sent}

    def complete(self, token: str, *, pasted: bool) -> None:
        item = self._lookup(token)
        if not item.paste_sent or pasted is not True:
            raise HTTPException(409, "请等待粘贴并确认本次文本已出现在目标输入框。", headers=NO_STORE)
        item.completed = True
        item.changed.set()

    def resolve(self, *, paste_finished: bool) -> None:
        if self._operation_active or paste_finished is not True:
            raise HTTPException(409, "请等待请求结束并确认上一次快捷指令和粘贴都已结束，再解除阻止。", headers=NO_STORE)
        self._needs_resolution = False

    @staticmethod
    def _timeout(item: _Pending) -> DeviceError:
        if item.paste_started:
            return DeviceError("等待粘贴完成确认超时；已阻止后续复制，请核对结果并解除阻止。")
        if item.fetched:
            return DeviceError("等待剪贴板确认超时；请结束上一次快捷指令并核对结果，再解除复制阻止。")
        return DeviceError("等待 iPhone 剪贴板确认超时，没有粘贴。请检查快捷指令并重新发起输入。")

    async def _wait(self, item: _Pending, done: Callable[[], bool], stopped: Callable[[], bool]) -> None:
        while True:
            if stopped():
                raise DeviceError("已紧急停止；如已发送粘贴键，请先确认结果再继续。")
            if item.expired or time.monotonic() >= item.deadline:
                raise self._timeout(item)
            if done():
                return
            item.changed.clear()
            try:
                await asyncio.wait_for(item.changed.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass

    async def _run(self, item: _Pending, stopped: Callable[[], bool],
                   send: Callable[[str], Awaitable[None]]) -> None:
        if self.trigger_key is not None:
            await send(f"key {self.trigger_key}")
        await self._wait(item, lambda: item.acknowledged, stopped)
        # 给快捷指令 HTTP 动作和浮层留出退出时间；完成仍须单独确认。
        await asyncio.sleep(0.5)
        await self._wait(item, lambda: item.acknowledged, stopped)
        item.paste_started = True
        await send("key cmd+v")
        item.paste_sent = True
        await self._wait(item, lambda: item.completed, stopped)

    async def paste(self, text: str, *, stopped: Callable[[], bool],
                    send: Callable[[str], Awaitable[None]]) -> None:
        if not text or len(text) > 4000:
            raise DeviceError("粘贴内容需要 1..4000 个字符。")
        if stopped():
            raise DeviceError("已紧急停止，剪贴板内容没有发出。")
        if self._operation_active:
            raise DeviceError("剪贴板正在处理上一次输入，请等待完成。")
        if self._needs_resolution:
            raise DeviceError("上一次剪贴板操作是否结束尚未确认，已阻止后续复制。"
                              "请按 docs/PASTE_INPUT.md 核对并解除阻止。")
        item = _Pending(secrets.token_urlsafe(32), text, time.monotonic() + self.ttl)
        text = ""
        self._pending = item
        self._operation_active = True
        expiry = asyncio.get_running_loop().call_later(self.ttl, self._expire, item)
        try:
            await asyncio.wait_for(self._run(item, stopped, send), timeout=self.ttl)
        except asyncio.TimeoutError as exc:
            raise self._timeout(item) from exc
        finally:
            expiry.cancel()
            self._expire(item)
            self._operation_active = False


def create_router(get_provider: Callable[[], CompanionClipboard | None]) -> APIRouter:
    router = APIRouter(prefix="/api/clipboard")

    def provider() -> CompanionClipboard:
        current = get_provider()
        if current is None:
            raise HTTPException(404, "尚未配置 iPhone 剪贴板快捷指令。", headers=NO_STORE)
        return current

    def paired(request: Request) -> CompanionClipboard:
        current = provider()
        if not current.authorized(request.headers.get("authorization", "")):
            raise HTTPException(401, "剪贴板配对密钥不正确。", headers=NO_STORE)
        return current

    @router.get("/pending")
    async def pending(request: Request):
        return JSONResponse(paired(request).pending() or {"pending": False}, headers=NO_STORE)

    @router.get("/current")
    async def current(request: Request):
        return JSONResponse(paired(request).current(), headers=NO_STORE)

    @router.post("/resolve")
    async def resolve(request: Request, confirmation: _ResolveConfirmation):
        paired(request).resolve(paste_finished=confirmation.paste_finished)
        return JSONResponse({"ok": True}, headers=NO_STORE)

    @router.get("/{token}")
    async def fetch(token: str):
        return PlainTextResponse(provider().fetch(token), headers=NO_STORE)

    @router.get("/{token}/status")
    async def status(token: str):
        return JSONResponse(provider().status(token), headers=NO_STORE)

    @router.post("/{token}/ack")
    async def acknowledge(token: str, confirmation: _CopyConfirmation):
        provider().acknowledge(token, copied=confirmation.copied, focused=confirmation.focused)
        return JSONResponse({"ok": True}, headers=NO_STORE)

    @router.post("/{token}/complete")
    async def complete(token: str, confirmation: _PasteConfirmation):
        provider().complete(token, pasted=confirmation.pasted)
        return JSONResponse({"ok": True}, headers=NO_STORE)

    return router
