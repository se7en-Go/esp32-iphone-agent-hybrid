"""One monotonic budget shared by an HTTP action and all of its pointer checks."""

import asyncio
import time
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps

from starlette.responses import JSONResponse

# Leave 15 seconds for delivery / scheduling before IPhoneClient's 60 second timeout.
OPERATION_TIMEOUT = 45.0
DEADLINE_ERROR = "operation deadline exceeded; no further HID sent; observe before continuing"


class OperationDeadline(RuntimeError):
    pass


@dataclass
class Operation:
    deadline: float
    cancelled: bool = False
    # The loop timer can fire while time.monotonic() is still on the previous
    # Windows tick (~15.6 ms). That callback is the deadline, not a disconnect.
    expired: bool = False

    def check(self):
        if self.expired or time.monotonic() >= self.deadline:
            raise OperationDeadline(DEADLINE_ERROR)
        if self.cancelled:
            raise asyncio.CancelledError


_current: ContextVar[Operation | None] = ContextVar("hid_operation", default=None)


def check_operation():
    current = _current.get()
    if current is not None:
        current.check()


def remaining_operation_time() -> float:
    """Read the original request budget without creating or extending a scope."""
    current = _current.get()
    if current is None:
        return float("inf")
    current.check()
    return max(0.0, current.deadline - time.monotonic())


async def shield_cleanup(awaitable):
    """Finish bounded cleanup before propagating even repeated cancellation.

    The cleanup supplies its own timeout, independent of the operation deadline.
    """
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    try:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        return task.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


@asynccontextmanager
async def operation_scope(timeout: float):
    current = _current.get()
    if current is not None:
        current.check()
        yield current
        current.check()
        return
    current = Operation(time.monotonic() + timeout)
    token = _current.set(current)
    owner = asyncio.current_task()

    def expire():
        current.expired = True
        current.cancelled = True
        owner.cancel()

    timer = asyncio.get_running_loop().call_later(timeout, expire)
    try:
        yield current
        current.check()
    except asyncio.CancelledError:
        if current.expired or time.monotonic() >= current.deadline:
            raise OperationDeadline(DEADLINE_ERROR) from None
        current.cancelled = True
        raise
    finally:
        timer.cancel()
        _current.reset(token)


def bounded_operation(method):
    @wraps(method)
    async def bounded(self, *args, **kwargs):
        try:
            async with operation_scope(self.operation_timeout):
                return await method(self, *args, **kwargs)
        except BaseException:
            # A cancelled move may have left the physical pointer anywhere.
            self._pos = None
            raise

    return bounded


class ActionLifetimeMiddleware:
    """Consume ASGI disconnects even while the route is waiting for video or HID.

    The receive pump forwards the unchanged request body to the application. Its
    cancellation flag is shared with exchange child tasks, including queued writes.
    """

    PATHS = {
        "/api/control/action", "/api/control/batch", "/api/control/layout-tap", "/api/control/host-click",
        "/api/control/find_text", "/api/control/middle-click-home/enable",
        "/api/control/clipboard/prepare", "/api/control/clipboard/paste",
        "/api/control/clipboard/confirm", "/api/control/clipboard/status", "/api/control/clipboard/cancel",
        "/api/calibrate/start", "/api/tap", "/api/swipe", "/api/scroll",
        "/api/type", "/api/key", "/api/home", "/api/back", "/api/raw",
    }

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in self.PATHS:
            return await self.app(scope, receive, send)
        messages = asyncio.Queue()
        disconnected = False
        response_started = False
        response_finished = False
        owner = asyncio.current_task()

        async def forward(message):
            nonlocal response_started, response_finished
            if message["type"] == "http.response.start":
                response_started = True
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_finished = True
            await send(message)

        try:
            async with operation_scope(OPERATION_TIMEOUT) as operation:
                async def pump():
                    nonlocal disconnected
                    while True:
                        message = await receive()
                        messages.put_nowait(message)
                        if message["type"] == "http.disconnect":
                            disconnected = True
                            if not response_finished:
                                operation.cancelled = True
                                owner.cancel()
                            return

                receiver = asyncio.create_task(pump())
                try:
                    await self.app(scope, messages.get, forward)
                finally:
                    receiver.cancel()
                    with suppress(asyncio.CancelledError):
                        await receiver
        except OperationDeadline:
            if not disconnected and not response_started:
                await JSONResponse({"detail": DEADLINE_ERROR}, status_code=504)(scope, receive, send)
        except asyncio.CancelledError:
            if not disconnected:
                raise
