"""Web API + control page. Run with `python -m hidagent`."""

import asyncio
import math
import os
from contextlib import asynccontextmanager, contextmanager, nullcontext
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .agent import Agent
from .clipboard import CompanionClipboard, create_router
from .clipboard_control import create_control_router
from .control import Controls
from .debug import Trace
from .device import Device, DeviceError, input_attempt, supervise_maintain
from .enter_context import verify_enter_context
from .freshness import MAX_REPLAY_LAG, snapshot
from .guard import Guard, limits_for, skill_file
from .input_context import InputContextGuard
from .layouts import list_layouts, resolve_point
from .llm import LLM
from .messages import for_user, hints
from .ocr import OCRError, OCRUnavailable
from .operation import ActionLifetimeMiddleware
from .paths import skill_roots
from .phone import Phone
from .request_lifetime import while_connected
from .screen import Screen
from .screen_uxplay import UxPlayScreen
from .send_context import named_send as _named_send
from .text_input import TypeMode, clipboard_mode_options, select_type_mode

DATA_DIR = Path(os.environ.get("HIDAGENT_DATA", Path.home() / ".hidagent"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEBUG = os.environ.get("HIDAGENT_DEBUG") == "1"


def make_screen():
    backend = os.environ.get("SCREEN_BACKEND", "board").strip() or "board"
    if backend == "board":
        return Screen()
    if backend == "uxplay":
        return UxPlayScreen(os.environ.get("AIRPLAY_NAME", "ESP32 Agent Mac"),
                            socket_path=os.environ.get("AIRPLAY_SOCKET") or None)
    raise RuntimeError(
        "SCREEN_BACKEND 只能是 board（默认，板载 TCP 7201）或 uxplay（仅 macOS 的退路）。"
        f"现在是 {backend!r}。"
    )


device = Device()
screen = make_screen()
phone = Phone(device, DATA_DIR / "calibration.json", screen, clipboard=CompanionClipboard.from_env())
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
app.include_router(create_router(lambda: getattr(phone, "clipboard", None)))
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def refuse_if_tripped() -> None:
    if guard.tripped:
        raise HTTPException(
            409,
            f"熔断还在（{guard.reason or '风险提示'}）。点、滑、打字都已停下。"
            "看过手机、确认提示消失之后，再调用重置。重启服务不会自动解除。",
        )


def is_enter_key(command: str) -> bool:
    """Recognize Enter chords; observed context decides whether plain Enter sends."""
    name, _, arg = command.strip().partition(" ")
    if name.lower() != "key":
        return False
    parts = [piece.strip().lower() for piece in arg.replace("+", " ").split() if piece.strip()]
    return "enter" in parts


def _check_hid_trip(current: Guard, name: str) -> None:
    """调用方持有 current._lock；release/home 保留紧急停止的清理语义。"""
    if current.tripped and name not in {"release", "home"}:
        raise DeviceError(
            f"熔断还在（{current.reason or '风险提示'}）。这一条 HID 没有发出。"
        )


@contextmanager
def _hid_write_guard(current: Guard, name: str, enter_evidence=None, *, count_send=False, named_send=False):
    # 与 observe/trip 同锁，复查到实际串口写入之间不能有线程插入，也不能 await。
    with current._lock:
        _check_hid_trip(current, name)
        if enter_evidence is not None:
            publication_lock = getattr(screen, "_frame_lock", None)
            if publication_lock is None:
                raise DeviceError("画面后端没有发布锁，不能安全免除 Enter 的发送配额。")
            # Keep the observed keyboard fixed through the synchronous HID write.
            # Lock order matches Device.exchange: Guard first, screen second.
            with publication_lock:
                try:
                    current.check_enter_evidence(enter_evidence, consume=True)
                except RuntimeError as exc:
                    raise DeviceError(str(exc)) from exc
                yield
        else:
            def consume_send():
                decision = current.consume_reservation("")
                if not decision["allowed"]:
                    if named_send:
                        raise HTTPException(409, decision["reason"])
                    raise DeviceError(decision["reason"])

            # Device invokes this after final observation validation, immediately
            # before write, while both the Guard and publication locks remain held.
            yield consume_send if count_send else None


def _hid_gate(command: str):
    name = command.split(" ", 1)[0].strip().lower()
    if name not in {"home", "move", "press", "release", "click", "middlepress", "middleclick",
                    "scroll", "type", "key", "homescreen"}:
        return None
    clipboard = getattr(phone, "clipboard", None)
    check_input = getattr(clipboard, "check_input", None)
    if callable(check_input):
        check_input(command)
    current = guard
    enter_evidence = None
    with current._lock:
        _check_hid_trip(current, name)
        if command.strip().lower() == "key enter":
            try:
                enter_evidence = current.enter_evidence()
            except RuntimeError as exc:
                raise DeviceError(str(exc)) from exc
    named_send = _named_send.get()
    count_send = (is_enter_key(command) and enter_evidence is None) or (name == "click" and named_send)
    return _hid_write_guard(current, name, enter_evidence, count_send=count_send, named_send=named_send)


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


app.add_middleware(ActionLifetimeMiddleware)


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


class TypeText(KeyboardText):
    mode: TypeMode = "auto"


class KeyText(KeyboardText):
    screen_id: str | None = None
    enter_intent: Literal["navigation", "editing"] | None = None


class ActionStep(BaseModel):
    action: Literal["point", "tap", "tap_text", "swipe", "scroll", "type", "compose_chinese", "key", "home", "back",
                    "path", "circle", "long_press", "double_tap", "open_app"]
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
    mode: TypeMode = "auto"
    repeat: int = Field(default=1, ge=1, le=50)
    nth: int | None = Field(default=None, ge=1, strict=True)
    exact: bool = False
    region: tuple[float, float, float, float] | None = None
    enter_intent: Literal["navigation", "editing"] | None = None
    target_box: tuple[float, float, float, float] | None = None
    points: list[tuple[float, float]] | None = Field(default=None, min_length=2, max_length=256)
    radius_x: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    radius_y: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    segments: int = Field(default=48, ge=12, le=128, strict=True)
    hold: bool = True
    rehome: bool = False
    duration: float = Field(default=.8, ge=.3, le=3, allow_inf_nan=False)


class BatchBody(BaseModel):
    screen_id: str = Field(min_length=1)
    steps: list[ActionStep] = Field(min_length=1, max_length=8)
    include_diff: bool = True
    ocr_diff: bool = False


class FindTextBody(BaseModel):
    screen_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=4000)
    nth: int = Field(default=1, ge=1, strict=True)
    exact: bool = False
    region: tuple[float, float, float, float] | None = None


class LayoutTap(BaseModel):
    layout: str = Field(min_length=1, max_length=80)
    point: str = Field(min_length=1, max_length=80)
    screen_id: str = Field(min_length=1)


class SkillBody(BaseModel):
    skill: str = ""
    observed_text: str = Field(default="", max_length=4000)
    bubble_visible: bool = False


class ControlAction(ActionStep):
    screen_id: str = Field(min_length=1)
    include_diff: bool = True
    ocr_diff: bool = False


UnitCoordinate = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]


class HostClickBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screen_id: str = Field(min_length=1)
    pointer_x: UnitCoordinate
    pointer_y: UnitCoordinate
    target_box: tuple[UnitCoordinate, UnitCoordinate, UnitCoordinate, UnitCoordinate]
    target_label: str = Field(default="", max_length=120)
    duration: float | None = Field(default=None, ge=.3, le=3, strict=True, allow_inf_nan=False)


def check_keyboard(text):
    if not text.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise HTTPException(422, "keyboard input must be printable ASCII on one line")


manual_lock = asyncio.Lock()


def check_manual():
    if agent.running or manual_lock.locked():
        raise HTTPException(409, "phone is busy; stop the agent or wait for the current action")


@contextmanager
def _legacy_input_session():
    """Pin old web actions before any await, including delayed clipboard input."""
    original_phone, original_device, original_screen = phone, device, screen
    active_device = getattr(phone, "device", device)
    active_screen = getattr(phone, "screen", screen)
    link, epoch = getattr(active_device, "links", 0), getattr(active_device, "abort_epoch", 0)
    generation = snapshot(active_screen).generation

    def validate():
        if getattr(active_device, "abort_epoch", 0) != epoch:
            raise DeviceError("已紧急停止，后面的内容没有发出。")
        if (phone is not original_phone or device is not original_device or screen is not original_screen
                or getattr(phone, "device", device) is not active_device
                or getattr(phone, "screen", screen) is not active_screen
                or getattr(active_device, "links", 0) != link
                or snapshot(active_screen).generation != generation):
            raise DeviceError("操作期间设备或画面连接已变化，请重新观察。")

    with input_attempt(before_write=validate, inherit=True,
                       write_lock=getattr(active_screen, "_frame_lock", None)):
        validate()
        yield
        validate()


async def act(action):
    refuse_if_tripped()
    check_manual()
    try:
        async with manual_lock:
            with _legacy_input_session():
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


def _board_hid(dev) -> dict | None:
    board = getattr(dev, "board_status", None)
    hid = board.get("hid") if isinstance(board, dict) else None
    if isinstance(hid, str):
        return {"path": hid}
    return hid if isinstance(hid, dict) else None


def _board_video(dev) -> dict:
    board = getattr(dev, "board_status", None)
    if not isinstance(board, dict) or not getattr(dev, "connected", False):
        return {}
    result = {}
    for key in ("up", "fps", "vage", "fz", "ovf"):
        value = board.get(key)
        if type(value) is int and 0 <= value < 2**32:
            result[key] = value
        elif key == "fps" and type(value) is float and math.isfinite(value) and value >= 0:
            result[key] = value
    for key, allowed in (("ap", ("idle", "connected", "streaming")), ("owner", ("none", "cli", "app"))):
        if board.get(key) in allowed:
            result[key] = board[key]
    return result


def _screen_preview():
    preview = getattr(screen, "preview", None)
    return preview() if preview is not None else None


@app.get("/api/status")
def status():
    dev = bool(getattr(device, "connected", False))
    preview = _screen_preview()
    live = preview.frame is not None if preview is not None else bool(getattr(screen, "live", False))
    board = _board_video(device)
    video = {"board": board}
    if preview is not None:
        video.update(state=preview.state, frame_age_s=preview.frame_age_s, replay_lag_s=preview.replay_lag_s,
                     heartbeat_age_s=preview.heartbeat_age_s, generation=preview.generation)
    adopt = getattr(phone, "adopt_pace", None)
    if adopt:
        adopt(getattr(device, "pace_version", None))
    calibrated = bool(getattr(phone, "calibrated", False))
    can_wake = bool(preview is not None and preview.state == "waiting" and screen.live
                    and preview.frame_age_s is not None and preview.replay_lag_s is not None
                    and preview.replay_lag_s <= MAX_REPLAY_LAG and preview.heartbeat_age_s is not None
                    and preview.heartbeat_age_s <= 3 and board.get("ap") == "streaming" and board.get("fz") == 0)
    last_error = str(getattr(device, "last_error", "") or getattr(screen, "last_error", "") or "")
    hid = _board_hid(device)
    receiver = screen.name if isinstance(screen, UxPlayScreen) else "ESP32 Agent"
    setup_hints = hints(dev, live, calibrated, "" if dev else last_error,
                        str(getattr(screen, "last_error", "") or ""), hid)
    if isinstance(screen, UxPlayScreen) and not live:
        setup_hints["screen"] = {
            "zh": f"在 iPhone 控制中心打开「屏幕镜像」，选择「{receiver}」。",
            "en": f"On the iPhone, open Control Center → Screen Mirroring and select {receiver}.",
        }
    return {"device": dev, "screen": live, "calibrated": calibrated, "ready": dev and live and calibrated,
            "task_ready": dev and calibrated and (live or can_wake),
            "debug": DEBUG, "trace": str(agent.trace.dir or ""), "aspect": round(screen.aspect, 4),
            "hints": setup_hints,
            "hid": hid, "video": video,
            "link": {"port": getattr(device, "port_name", ""), "mode": getattr(device, "mode", ""),
                     "ip": getattr(device, "ip", ""), "receiver": receiver},
            "models": {"planner": agent.planner.model, "actor": agent.actor.model},
            "agent": {"running": agent.running, "task": agent.task, "steps": agent.steps},
            "guard": {"tripped": guard.tripped, "reason": guard.reason}}


@app.get("/api/frame.jpg")
def frame():
    preview = _screen_preview()
    headers = {"Cache-Control": "no-store"}
    if preview is None:
        jpeg = screen.frame if screen.live else None
    else:
        jpeg = preview.frame
        headers["X-Preview-State"] = preview.state
        headers["X-Preview-Generation"] = str(preview.generation)
        if preview.frame_age_s is not None:
            headers["X-Frame-Age"] = f"{preview.frame_age_s:.1f}"
    if jpeg is None:
        raise HTTPException(404, "no live screen: start screen mirroring on the phone", headers=headers)
    return Response(jpeg, media_type="image/jpeg", headers=headers)


app.include_router(create_control_router(lambda: controls, lambda: manual_lock,
                                        check_manual, refuse_if_tripped, while_connected))


@app.get("/api/control/capabilities")
def control_capabilities():
    """Report actual transport abilities; availability is not task verification."""
    from typing import get_args

    return {
        "actions": list(get_args(ActionStep.model_fields["action"].annotation)),
        "coordinate_spaces": ["normalized", "pixels"],
        "limits": {"path_points": 256, "circle_segments": [12, 128], "long_press_seconds": [.3, 3],
                   "batch_steps": 8, "observation_seconds": 30},
        "features": {
            "clipboard": {"configured": phone.clipboard is not None, "staged": True,
                          "protocol": getattr(phone.clipboard, "protocol", None),
                          "legacy_paste_available": clipboard_mode_options(phone.clipboard)["clipboard_available"],
                          "staged_available": clipboard_mode_options(phone.clipboard)["clipboard_staged_available"],
                          "trigger_configured": bool(getattr(phone.clipboard, "trigger_key", None)),
                          "stages": ["prepare", "status", "paste", "confirm", "cancel"]},
            "app_search": {"available": phone.app_search_available,
                           "select_result_separately": True, "ascii_query_only": True},
            "multitouch": {"supported": False,
                           "reason": "This firmware exposes one relative mouse pointer, not multitouch contacts."},
            "drawing": {"supported": True, "verified_start": True, "closed_loop_during_stroke": False},
            "middle_click_home": controls.middle_click_home_status(),
            "host_observed_click": {
                "supported": True, "endpoint": "/api/control/host-click", "evidence_source": "host_observed",
                "moves_pointer": False, "requires_calibration": False, "single_click_only": False,
                "hold_seconds": {"minimum": .3, "maximum": 3},
                "auto_fallback": False,
                "passive_observation": "/api/control/observe?for_click=true",
                "qualified_ticket_seconds": 30,
                "requires": ["fresh screen_id after last input and mirror delay", "visible pointer_xy",
                             "target_box strictly containing the pointer"],
            },
        },
        "verification": "Inspect each returned image. Command acknowledgment does not prove task completion.",
    }


@app.get("/api/control/observe")
async def control_observe(http_request: Request, for_click: bool = False):
    check_manual()
    async with manual_lock:
        controls.job = asyncio.current_task()
        try:
            if for_click:
                try:
                    return await while_connected(http_request, controls.observe_for_click())
                except BaseException:
                    controls.invalidate()
                    raise
            ensure = getattr(controls.phone, "ensure_observation", None)
            accepted = await ensure() if ensure is not None else None
            return controls.observation(accepted)
        except (ValueError, RuntimeError) as e:
            raise HTTPException(409, str(e)) from e
        except asyncio.CancelledError as e:
            raise HTTPException(409, "观察已停止，请重新观察。") from e
        finally:
            controls.job = None


@app.post("/api/control/find_text")
async def control_find_text(request: FindTextBody):
    check_manual()
    async with manual_lock:
        controls.job = asyncio.current_task()
        try:
            return await controls.find_text(request.screen_id, request.text, request.nth, request.exact, request.region)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        except OCRUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except OCRError as e:
            raise HTTPException(409, str(e)) from e
        except asyncio.CancelledError as e:
            raise HTTPException(409, "查找已停止，请重新观察。") from e
        finally:
            controls.job = None


@app.post("/api/control/action")
async def control_action(request: ControlAction, http_request: Request):
    refuse_if_tripped()
    check_manual()
    async with manual_lock:
        controls.job = asyncio.current_task()
        try:
            scope = nullcontext()
            if request.enter_intent is not None:
                if request.action != "key" or request.repeat != 1:
                    raise ValueError("Enter 用途只适用于单独的一次 key 动作。")
                observation = getattr(controls, "_observation", None)
                before, _, _ = controls._validate_ticket(controls.ticket, request.screen_id)
                validate = await verify_enter_context(screen, before, request.enter_intent, request.text,
                                                      observation=observation)
                scope = guard.verified_enter(request.enter_intent, validate)
            with scope:
                return await while_connected(http_request, controls.perform(request))
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        except OCRUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except OCRError as e:
            raise HTTPException(409, str(e)) from e
        except (DeviceError, RuntimeError) as e:
            raise HTTPException(409, str(e)) from e
        except asyncio.CancelledError as e:
            raise HTTPException(409, "action stopped; observe before continuing") from e
        finally:
            controls.job = None


@app.post("/api/control/host-click")
async def control_host_click(request: HostClickBody, http_request: Request):
    refuse_if_tripped()
    check_manual()
    async with manual_lock:
        controls.job = asyncio.current_task()
        try:
            return await while_connected(http_request, controls.click_observed(request))
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except asyncio.CancelledError as exc:
            raise HTTPException(409, "host click stopped; observe before continuing") from exc
        finally:
            controls.job = None


def _steps(screen_id: str, steps: list[ActionStep]) -> list[ControlAction]:
    return [ControlAction(screen_id=screen_id, **step.model_dump()) for step in steps]


@app.post("/api/control/batch")
async def control_batch(request: BatchBody, http_request: Request):
    """一次观察、连续多步，最后只回一张图。"""
    refuse_if_tripped()
    check_manual()
    if any(step.enter_intent is not None for step in request.steps):
        raise HTTPException(409, "导航/换行 Enter 需要单步的新观察，不能放进 batch。")
    async with manual_lock:
        controls.job = asyncio.current_task()
        try:
            return await while_connected(
                http_request, controls.perform_batch(request.screen_id, _steps(request.screen_id, request.steps),
                                                     include_diff=request.include_diff, ocr_diff=request.ocr_diff))
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        except OCRUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except OCRError as e:
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
async def type_text(t: TypeText, request: Request):
    try:
        select_type_mode(t.text, t.mode, **clipboard_mode_options(getattr(phone, "clipboard", None)))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    async def type_observed():
        observed = await phone.ensure_observation()
        validate = InputContextGuard(screen, observed)
        await phone.type(t.text, mode=t.mode, before_action=validate)

    return await while_connected(request, act(type_observed))


@app.post("/api/key")
async def key(t: KeyText):
    check_keyboard(t.text)
    if t.enter_intent is None:
        return await act(lambda: phone.key(t.text))
    refuse_if_tripped()
    check_manual()
    async with manual_lock:
        controls.job = asyncio.current_task()
        try:
            with _legacy_input_session():
                observation = getattr(controls, "_observation", None)
                before, _, _ = controls.consume(t.screen_id)
                validate = await verify_enter_context(screen, before, t.enter_intent, t.text,
                                                      observation=observation)
                with guard.verified_enter(t.enter_intent, validate):
                    await phone.key(t.text.strip().lower(), before_action=validate)
            return {"ok": True, "enter_intent": t.enter_intent, "send_counted": False}
        except OCRUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except asyncio.CancelledError as exc:
            raise HTTPException(409, "Enter 已停止，请重新观察。") from exc
        finally:
            controls.job = None


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


class MiddleClickEnableBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    screen_id: str = Field(min_length=1)
    include_diff: bool = True
    ocr_diff: bool = False


class MiddleClickRevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    all_devices: bool = False


@app.get("/api/control/middle-click-home")
async def middle_click_home_status():
    await controls.phone.assistive_touch._refresh_board_status()
    return controls.middle_click_home_status()


@app.post("/api/control/middle-click-home/enable")
async def middle_click_home_enable(request: MiddleClickEnableBody, http_request: Request):
    refuse_if_tripped()
    check_manual()
    async with manual_lock:
        controls.job = asyncio.current_task()
        try:
            return await while_connected(
                http_request,
                controls.enable_middle_click_home(
                    request.screen_id, include_diff=request.include_diff, ocr_diff=request.ocr_diff
                ),
            )
        except (ValueError, RuntimeError) as e:
            raise HTTPException(409, str(e)) from e
        except asyncio.CancelledError as e:
            raise HTTPException(409, "验证已停止，请重新观察。") from e
        finally:
            controls.job = None


@app.post("/api/control/middle-click-home/revoke")
async def middle_click_home_revoke(body: MiddleClickRevokeBody | None = None):
    check_manual()
    async with manual_lock:
        return controls.revoke_middle_click_home(all_devices=bool(body and body.all_devices))


class CalibrationStartBody(BaseModel):
    multipoint: bool = False
    rehome_each_move: bool = False


@app.post("/api/calibrate/start")
async def calibrate_start(body: CalibrationStartBody | None = None):
    """Calibrate the pointer. {"auto": true}: done by itself; otherwise the user clicks the pointer next."""
    refuse_if_tripped()
    check_manual()
    try:
        async with manual_lock:
            with _legacy_input_session():
                controls.invalidate()
                auto = await phone.start_calibration(
                    multipoint=bool(body and body.multipoint),
                    rehome_each_move=bool(body and body.rehome_each_move),
                )
            return {
                "ok": True,
                "auto": auto,
                "span": phone.span,
                "multipoint": phone._affine is not None,
                "rehome_each_move": phone._rehome_each_move,
            }
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
    cancel_paste = getattr(getattr(phone, "clipboard", None), "cancel_staged", None)
    if callable(cancel_paste):
        cancel_paste()
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
