"""让等待手机确认的操作随 HTTP 调用方断连结束。"""

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from fastapi import HTTPException, Request

T = TypeVar("T")


async def while_connected(request: Request, operation: Awaitable[T]) -> T:
    """请求正文已读取；取消后等待清理，不能留下可确认的粘贴。"""
    finished = False

    async def disconnected():
        while not finished:
            if (await request.receive())["type"] == "http.disconnect":
                return

    work = asyncio.ensure_future(operation)
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            watcher.result()
            raise HTTPException(499, "调用方已断开，输入操作已取消；请重新观察后操作。")
        return await work
    finally:
        # On Python 3.10 a middleware receive cancel scope can swallow our
        # cancellation while returning a body message. Do not receive again:
        # the next disconnect may depend on this very response completing.
        finished = True
        for task in (work, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, watcher, return_exceptions=True)
