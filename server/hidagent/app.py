"""Web API + control page. Run with `python -m hidagent`."""

import asyncio
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent
from .control import Controls
from .debug import Trace
from .device import Device, DeviceError, supervise_maintain
from .guard import Guard, limits_for, skill_file
from .layouts import list_layouts, resolve_point
from .llm import LLM
from .messages import for_user, hints
from .paths import skill_roots
from .phone import Phone
from .screen import Screen
from .screen_uxplay import UxPlayScreen

DATA_DIR = Path(os.environ.get("HIDAGENT_DATA", Path.home() / ".hidagent"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEBUG = os.environ.get("HIDAGENT_DEBUG") == "1"


def make_screen():
    backend = os.environ.get("SCREEN_BACKEND", "board").strip() or "board"
    if backend == "board":
        return Screen()
    if backend == "uxplay":
        return UxPlayScreen(os.environ.get("AIRPLAY_NAME", "ESP32 Agent"))
    raise RuntimeError(
        "SCREEN_BACKEND 只能是 board（默认，板载 TCP 7201）或 uxplay（仅 macOS 的退路）。"
        f"现在是 {backend!r}。"
    )


device = Device()
screen = make_screen()
phone = Phone(device, DATA_DIR / "calibration.json", screen)
controls = Controls(phone, screen)
agent = Agent(phone, screen, LLM.from_env("ACTOR"), LLM.from_env("PLANNER"),
              Trace(DATA_DIR / "traces" if DEBUG else None))
guard = Guard(path=DATA_DIR / "guard.json")


@asynccontextmanager
async def lifespan(_):
    # 测试可替换 device / screen；启动时必须具备设备保活接口。
    device._closing = False
    maintain = asyncio.create_task(supervise_maintain(device)) if hasattr(device, "maintain") else None
    if hasattr(screen, "bind"):
        screen.bind(device)
    if hasattr(screen, "start"):
        screen.start()
    awake = asyncio.create_task(device.keep_awake(screen=screen))
    try:
        yield
    finally:
        device._closing = True
        awake.cancel()
        if maintain:
            maintain.cancel()
        if hasattr(screen, "stop"):
            screen.stop()
        if hasattr(device, "close"):
            device.close()


app = FastAPI(lifespan=lifespan)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def refuse_if_tripped() -> None:
    if guard.tripped:
        raise HTTPException(
            409,
            f"熔断还在（{guard.reason or '风险提示'}）。点、滑、打字都已停下。"
            "看过手机、确认提示消失之后，再调用重置。重启服务不会自动解除。",
        )


def is_enter_key(command: str) -> bool:
    """enter、Enter、enter+shift、shift+enter、带空格的 enter 都是发送键。"""
    name, _, arg = command.strip().partition(" ")
    if name.lower() != "key":
        return False
    parts = [piece.strip().lower() for piece in arg.replace("+", " ").split() if piece.strip()]
    return "enter" in parts


_named_send: ContextVar[bool] = ContextVar("named_send", default=False)


def _hid_gate(command: str) -> None:
    name = command.split(" ", 1)[0].strip().lower()
    if name not in {"home", "move", "press", "release", "click", "scroll", "type", "key", "homescreen"}:
        return
    if guard.tripped and name not in {"release", "home"}:
        raise DeviceError(
            f"熔断还在（{guard.reason or '风险提示'}）。这一条 HID 没有发出。"
        )
    if is_enter_key(command) or (name == "click" and _named_send.get()):
        decision = guard.consume_reservation("")
        if not decision["allowed"]:
            if _named_send.get():
                raise HTTPException(409, decision["reason"])
            raise DeviceError(decision["reason"])


device.gate = _hid_gate


@app.middleware("http")
async def reject_cross_site_posts(request: Request, call_next):
    if request.method == "POST" and request.url.path.startswith("/api/"):
        for header in (request.headers.get("origin"), request.headers.get("referer")):
            if not header:
                continue
            host = (urlparse(header).hostname or "").lower()
            if host not in LOCAL_HOSTS:
                return JSONResponse({"detail": "拒绝来自其他网站的请求。请只从本机网页操作。"}, status_code=403)
        ctype = request.headers.get("content-type", "")
        length = request.headers.get("content-length")
        has_body = bool(request.headers.get("transfer-encoding")) or (
            length is not None and length not in ("", "0")
        )
        if ctype and "application/json" not in ctype.lower():
            return JSONResponse({"detail": "只接受 application/json，不接受表单提交。"}, status_code=415)
        if not ctype and has_body:
            return JSONResponse({"detail": "只接受 application/json，不接受表单提交。"}, status_code=415)
    return await call_next(request)


class Static(StaticFiles):
    """Static files that browsers re-check on every load, so a new version shows up on refresh."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", Static(directory=Path(__file__).parent / "static"), name="static")


class Point(BaseModel):
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)


class Swipe(BaseModel):
    x1: float = Field(ge=0, le=1, allow_inf_nan=False)
    y1: float = Field(ge=0, le=1, allow_inf_nan=False)
    x2: float = Field(ge=0, le=1, allow_inf_nan=False)
    y2: float = Field(ge=0, le=1, allow_inf_nan=False)


class Scroll(Point):
    lines: int = Field(ge=-100, le=100)


class Text(BaseModel):
    text: str


class KeyboardText(Text):
    text: str = Field(min_length=1, max_length=4000)


class ActionStep(BaseModel):
    action: Literal["point", "tap", "swipe", "scroll", "type", "compose_chinese", "key", "home", "back"]
    coordinate_space: Literal["normalized", "pixels"] = "normalized"
    x: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    y: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    x1: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    y1: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    x2: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    y2: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    lines: int = Field(default=0, ge=-100, le=100)
    text: str = Field(default="", max_length=4000)
    input_mode: Literal["pinyin"] | None = None
    repeat: int = Field(default=1, ge=1, le=50)


class BatchBody(BaseModel):
    screen_id: str = Field(min_length=1)
    steps: list[ActionStep] = Field(min_length=1, max_length=8)


class LayoutTap(BaseModel):
    layout: str = Field(min_length=1, max_length=80)
    point: str = Field(min_length=1, max_length=80)
    screen_id: str = Field(min_length=1)


class SkillBody(BaseModel):
    skill: str = ""
    observed_text: str = Field(default="", max_length=4000)
    bubble_visible: bool = False


class ControlAction(BaseModel):
    action: Literal["point", "tap", "swipe", "scroll", "type", "compose_chinese", "key", "home", "back"]
    screen_id: str = Field(min_length=1)
    coordinate_space: Literal["normalized", "pixels"] = "normalized"
    x: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    y: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    x1: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    y1: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    x2: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    y2: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    lines: int = Field(default=0, ge=-100, le=100)
    text: str = Field(default="", max_length=4000)
    input_mode: Literal["pinyin"] | None = None
    repeat: int = Field(default=1, ge=1, le=50)


def check_keyboard(text):
    if not text.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise HTTPException(422, "keyboard input must be printable ASCII on one line")


manual_lock = asyncio.Lock()


def check_manual():
    if agent.running or manual_lock.locked():
        raise HTTPException(409, "phone is busy; stop the agent or wait for the current action")


async def act(action):
    refuse_if_tripped()
    check_manual()
    try:
        async with manual_lock:
            controls.invalidate()
            await action()
    except asyncio.CancelledError as e:
        raise HTTPException(409, "已紧急停止，这一次没有做完。") from e
    except (DeviceError, RuntimeError) as e:
        raise HTTPException(409, for_user(str(e))) from e
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/status")
def status():
    dev = bool(getattr(device, "connected", False))
    live = bool(getattr(screen, "live", False))
    calibrated = bool(getattr(phone, "calibrated", False))
    last_error = str(getattr(device, "last_error", "") or getattr(screen, "last_error", "") or "")
    return {"device": dev, "screen": live, "calibrated": calibrated, "ready": dev and live and calibrated,
            "debug": DEBUG, "trace": str(agent.trace.dir or ""), "aspect": round(screen.aspect, 4),
            "hints": hints(dev, live, calibrated,
                           "" if dev else last_error,
                           str(getattr(screen, "last_error", "") or "")),
            "link": {"port": getattr(device, "port_name", ""), "mode": getattr(device, "mode", ""),
                     "ip": getattr(device, "ip", ""), "receiver": "ESP32 Agent"},
            "models": {"planner": agent.planner.model, "actor": agent.actor.model},
            "agent": {"running": agent.running, "task": agent.task, "steps": agent.steps},
            "guard": {"tripped": guard.tripped, "reason": guard.reason}}


@app.get("/api/frame.jpg")
def frame():
    if not screen.live:
        raise HTTPException(404, "no live screen: start screen mirroring on the phone")
    return Response(screen.frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/control/observe")
async def control_observe():
    check_manual()
    async with manual_lock:
        try:
            return controls.observation()
        except ValueError as e:
            raise HTTPException(409, str(e)) from e


@app.post("/api/control/action")
async def control_action(request: ControlAction):
    refuse_if_tripped()
    check_manual()
    async with manual_lock:
        controls.job = asyncio.current_task()
        try:
            return await controls.perform(request)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        except asyncio.CancelledError as e:
            raise HTTPException(409, "action stopped; observe before continuing") from e
        finally:
            controls.job = None


def _steps(screen_id: str, steps: list[ActionStep]) -> list[ControlAction]:
    return [ControlAction(screen_id=screen_id, **step.model_dump()) for step in steps]


@app.post("/api/control/batch")
async def control_batch(request: BatchBody):
    """一次观察、连续多步，最后只回一张图。"""
    refuse_if_tripped()
    check_manual()
    async with manual_lock:
        controls.job = asyncio.current_task()
        try:
            return await controls.perform_batch(request.screen_id, _steps(request.screen_id, request.steps))
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        except asyncio.CancelledError as e:
            raise HTTPException(409, "action stopped; observe before continuing") from e
        finally:
            controls.job = None


@app.get("/api/layouts")
def layouts():
    return {"layouts": list_layouts()}


@app.post("/api/control/layout-tap")
async def layout_tap(body: LayoutTap):
    refuse_if_tripped()
    check_manual()
    try:
        x, y = resolve_point(body.layout, body.point)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    request = ControlAction(action="tap", screen_id=body.screen_id, x=x, y=y)
    async with manual_lock:
        controls.job = asyncio.current_task()
        # exchange 子任务继承上下文；定位完成后，在实际 click 的串口 gate 里结算。
        send_token = _named_send.set(body.point == "send")
        try:
            result = await controls.perform(request)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        finally:
            _named_send.reset(send_token)
            controls.job = None
    result["layout"] = body.layout
    result["point"] = body.point
    return result


@app.get("/api/skills")
def skills():
    names = [item["id"] for item in list_layouts()]
    found = set(names)
    for root in skill_roots():
        for path in root.glob("*/SKILL.md"):
            found.add(path.parent.name)
    return {"skills": sorted(found)}


@app.get("/api/skills/{name}")
def skill_show(name: str):
    path = skill_file(name)
    if path is None:
        raise HTTPException(404, "没有这个 skill")
    return {"name": name, "markdown": path.read_text(encoding="utf-8"), "limits": limits_for(name)}


@app.post("/api/skill/check")
def skill_check(body: SkillBody):
    return guard.observe(body.observed_text, body.skill)


@app.post("/api/skill/prepare-send")
def skill_prepare(body: SkillBody):
    """发送前检查并预留一个名额，发送时消费且更新实际发送时间。"""
    return guard.prepare_send(body.skill)


@app.post("/api/skill/confirm-send")
def skill_confirm(body: SkillBody):
    """气泡位是调用方的声明。确认已消费的发送不重复计数，也不产生新的预留。"""
    return guard.confirm_send(bubble_visible=body.bubble_visible, observed_text=body.observed_text,
                              skill=body.skill)


class GuardReset(BaseModel):
    confirm: bool = False


@app.post("/api/guard/reset")
def guard_reset(body: GuardReset, request: Request):
    """只有本机网页、并且人明确确认过，才能解除熔断。发送次数保留。"""
    origin = request.headers.get("origin") or ""
    referer = request.headers.get("referer") or ""
    if not origin and not referer:
        raise HTTPException(403, "只能从本机网页解除熔断。")
    for header in (origin, referer):
        if not header:
            continue
        host = (urlparse(header).hostname or "").lower()
        if host not in LOCAL_HOSTS:
            raise HTTPException(403, "只能从本机网页解除熔断。")
    if body.confirm is not True:
        raise HTTPException(400, "需要在网页上确认已经看过手机。这一小时的发送次数不会清掉。")
    guard.reset()
    return {"ok": True, "tripped": guard.tripped, "sends": len(guard.sends)}


@app.get("/api/steps/{index}.jpg")
def step_shot(index: int):
    if index not in agent.shots:
        raise HTTPException(404)
    return Response(agent.shots[index], media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.post("/api/tap")
async def tap(p: Point):
    return await act(lambda: phone.tap(p.x, p.y))


@app.post("/api/swipe")
async def swipe(s: Swipe):
    return await act(lambda: phone.swipe(s.x1, s.y1, s.x2, s.y2))


@app.post("/api/scroll")
async def scroll(s: Scroll):
    return await act(lambda: phone.scroll(s.x, s.y, s.lines))


@app.post("/api/type")
async def type_text(t: KeyboardText):
    check_keyboard(t.text)
    return await act(lambda: phone.type(t.text))


@app.post("/api/key")
async def key(t: KeyboardText):
    check_keyboard(t.text)
    return await act(lambda: phone.key(t.text))


@app.post("/api/home")
async def home():
    return await act(phone.go_home)


@app.post("/api/back")
async def back():
    return await act(phone.go_back)


@app.post("/api/raw")
async def raw(t: Text):
    """Debug only: send one protocol line to the board as-is."""
    if not DEBUG:
        raise HTTPException(404)
    return await act(lambda: device.send(t.text))


@app.post("/api/calibrate/start")
async def calibrate_start():
    """Calibrate the pointer. {"auto": true}: done by itself; otherwise the user clicks the pointer next."""
    refuse_if_tripped()
    check_manual()
    try:
        async with manual_lock:
            controls.invalidate()
            return {"ok": True, "auto": await phone.start_calibration(), "span": phone.span}
    except asyncio.CancelledError as e:
        raise HTTPException(409, "已紧急停止，校准没有继续。") from e
    except (DeviceError, RuntimeError) as e:
        raise HTTPException(409, for_user(str(e))) from e


@app.post("/api/calibrate/finish")
async def calibrate_finish(p: Point):
    check_manual()
    if not 0 < p.x < 1 or not 0 < p.y < 1:
        raise HTTPException(422, "calibration pointer must be inside the screen")
    controls.invalidate()
    phone.finish_calibration(p.x, p.y)
    return {"ok": True, "span": phone.span}


@app.post("/api/agent")
async def agent_start(t: Text):
    refuse_if_tripped()
    if manual_lock.locked():
        raise HTTPException(409, "phone is busy; wait for the current action")
    if not agent.actor.model or not agent.planner.model:
        raise HTTPException(409, "Use host Agent vision, or explicitly configure "
                            "LLM_MODEL/LLM_BASE_URL/LLM_API_KEY")
    try:
        controls.invalidate()
        agent.start(t.text)
    except RuntimeError as e:
        raise HTTPException(409, for_user(str(e))) from e
    return {"ok": True}


@app.post("/api/agent/stop")
async def agent_stop():
    """Emergency stop: cancel the agent and release the mouse button."""
    controls.invalidate()
    job = controls.job
    if job and job is not asyncio.current_task():
        job.cancel()
    agent.stop()
    if hasattr(device, "request_abort"):
        device.request_abort()
    await asyncio.sleep(0)  # 让被取消的那一条先放开串口锁
    try:
        if getattr(device, "connected", False):
            await phone.reset()
    except (DeviceError, RuntimeError) as e:
        raise HTTPException(409, for_user(str(e))) from e
    finally:
        await asyncio.sleep(0)
        if hasattr(device, "clear_abort"):
            device.clear_abort()
    return {"ok": True}


@app.post("/api/agent/clear")
def agent_clear():
    """Forget the last run (steps, screenshots, plan) so the page starts clean."""
    try:
        agent.clear()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}
