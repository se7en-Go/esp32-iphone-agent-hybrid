"""See-think-act loop: a planner model writes the plan, an actor model picks each step."""

import asyncio
import json
import logging
import math
import os
import re
import time
from types import SimpleNamespace

from . import feedback, ocr, screen_compare, screen_diff, vision
from .debug import Trace
from .device import DeviceError, input_attempt
from .freshness import FrameSnapshot, fresh_frame, snapshot
from .input_context import InputContextGuard
from .llm import LLM
from .navigation_context import NavigationContext, for_phone
from .phone import AppSearchError, Phone, PointerLandingError, TypingVerificationError
from .screen import Screen
from .send_context import is_send_target, send_target_scope
from .text_target import TextTargetGuard

log = logging.getLogger(__name__)

PLANNER_PROMPT = """You plan tasks on an iPhone. Given the goal and the current screen,
write a short numbered plan (at most 6 steps) of what to do on screen. Plain text only, in English.
Plan only what the goal needs, from what you can see; never invent accounts, emails, logins or
screens you have not seen. Later steps can stay general (e.g. "Draw the face on the canvas").
To open an app, use the visible home screen or its Search control. Verify the matching app result before
selecting it."""

ACTOR_PROMPT = """You operate an iPhone through a mouse pointer. You see the current screen.
Reply in this format, nothing else:
THINK: <plain English a non-technical user understands: first check your last action against the
       screen (did it do what you intended?), then what you do now and why>
DO: <action>

Give several DO lines (up to 6, run in order) only for actions that do not need a new screen, such as
editing keys. After a tap, drawing path, or circle, inspect the new screen before the next action.
Give ONE DO line when the next action depends on what this one opens or changes.
Use find_text and tap_text alone, then inspect the result before continuing.
Use tap_text with an exact label and a tight, observed region when a label has no clear control boundary.
Use home or back for system navigation when the screen and goal support it.

Coordinates are integers 0-1000 relative to the screenshot (x left->right, y top->bottom).
Aim at the visual center of the element you want, not its edge or its label.
Look before you act: describe what is really on the screen now, not what you expect.
Each recent action says whether the screen changed after it. If it did NOT change, the action
missed or had no effect: do not assume it worked; aim again more precisely or try another way.
If feedback is stale, the tap outcome is unknown; do not repeat it automatically. A new observation
of the same screenshot does not prove failure. Inspect the result or stop for the user to check.
Only type when a text field is visibly focused (the keyboard is showing).
Swipes have no momentum: content moves exactly as far as the finger and stops. Prefer short swipes
(100-250 units) and check the result; repeat if needed rather than one big swipe.
Time or number wheels (e.g. an alarm's time): first tap the number in the wheel's center row and type
the digits (e.g. type 0700); check the result. Only if typing does not work, swipe inside that column:
measure the row spacing from two neighbouring numbers and swipe k times that spacing to move k rows.
Looking for an item in a list: scroll less than one screen at a time so nothing is skipped, and read every
visible item. If you reached both ends of the list and the item is not there, stop with fail and name the
closest options you saw; do not keep scrolling.
For an Apple whiteboard, look for Freeform (无边记). Create a new board for a new drawing. Verify the
drawing toolbar is open and verify the ink after each stroke. Stop if the app is unavailable.
Only tap things you can actually see on this screenshot; never guess where a button might be.
If you are unsure what an element does, tap it alone (one DO line) and check the result next round.
Actions:
  tap {"x":500,"y":380,"box":[150,350,900,410]}
                            (preferred for a visible button, icon, or list row: give the center and the
                             visible edges of that single control, all in 0-1000 coordinates)
  tap X Y                  (for a point on a canvas or when the control edges are unclear)
  find_text TEXT            (optional OCR: list visible text matches, then inspect their boxes before acting)
  tap_text TEXT             (optional OCR: locate visible text and tap its center with pointer verification)
  find_text {"text":"发送","region":[0.7,0.6,1,0.9],"exact":true}
  tap_text {"text":"发送","region":[0.7,0.6,1,0.9],"nth":1,"exact":true}
                            (OCR regions are normalized left,top,right,bottom, chosen from this screenshot;
                             duplicate matches require explicit nth, starting at 1. Send labels and substring
                             matches require a region that excludes message content. Never guess a region.)
  swipe X1 Y1 X2 Y2         (finger moves from X1,Y1 to X2,Y2: "swipe down" means Y2 > Y1)
  path X1 Y1 X2 Y2 X3 Y3 …  (one continuous stroke through 2-20 points, for drawing)
  circle X Y R              (draw a circle centered at X Y; R is the radius in x units)
  type TEXT                 (paste via configured companion, otherwise ASCII; focus the field first)
  key COMBO                 (enter, backspace, esc, cmd+a, ...)
  home | back | wait
  replan                    (the plan no longer fits the screen)
  done ANSWER               (the goal is reached; ANSWER is the result for the user)
  fail REASON

Example:
THINK: My last tap opened the home screen as intended. The App Store icon is visible, so I tap it.
DO: tap {"x":152,"y":362,"box":[94,335,210,389]}"""

APP_SEARCH_PROMPT = """
The verified Spotlight shortcut is available. Prefer `DO: open APP` to search for an English app name.
Use it as the only action in the reply. On the next observation, select the exact visible app result with
tap_text in a narrow region outside the search field. The English query Freeform may show the exact app
label 无边记 or 無邊記. Do not press Enter to choose a search result."""

APP_SEARCH_LABELS = {"freeform": frozenset({"freeform", "无边记", "無邊記"})}

# action name -> argument names; numeric arguments are 0-1000 screen coordinates
ACTIONS = {"tap": ("x", "y"), "swipe": ("x1", "y1", "x2", "y2"), "circle": ("x", "y", "r"), "path": ("points",),
           "type": ("text",), "key": ("combo",), "done": ("answer",), "fail": ("reason",),
           "find_text": ("text",), "tap_text": ("text",),
           "open": ("app",),
           "home": (), "back": (), "wait": (), "replan": ()}
TEXT_ARGS = ("text", "combo", "answer", "reason")


def parse_command(line: str) -> dict:
    """One DO line, e.g. "tap 152 362" -> {"action": "tap", "x": 152, "y": 362}."""
    name, _, rest = line.strip().partition(" ")
    name = name.lower()
    if name not in ACTIONS:
        raise ValueError(f"unknown action: {line[:100]}")
    if name == "open":
        app = rest.strip().strip('"').strip()
        if not app:
            raise ValueError("open needs an app name")
        return {"action": "open", "app": app}
    if name == "tap" and rest.lstrip().startswith("{"):
        target = json.loads(rest)
        if not isinstance(target, dict) or set(target) != {"x", "y", "box"}:
            raise ValueError("tap needs x, y and the observed control box")
        x, y, box = target["x"], target["y"], target["box"]
        if (not isinstance(box, list) or len(box) != 4
                or any(type(value) is not int for value in (x, y, *box))
                or not (0 <= box[0] < x < box[2] <= 1000 and 0 <= box[1] < y < box[3] <= 1000)):
            raise ValueError("tap coordinates must be integers inside the observed control box (0–1000)")
        return {"action": name, **target}
    if name in ("find_text", "tap_text") and rest.lstrip().startswith("{"):
        selector = json.loads(rest)
        if (not isinstance(selector, dict) or not isinstance(selector.get("text"), str)
                or not selector["text"].strip()
                or set(selector) - {"text", "region", "nth", "exact"}):
            raise ValueError("OCR selector needs text and optional region, nth, exact")
        region = selector.get("region")
        if region is not None and (not isinstance(region, list) or len(region) != 4
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(v) or not 0 <= v <= 1 for v in region)
                or region[0] >= region[2] or region[1] >= region[3]):
            raise ValueError("OCR region must be normalized left,top,right,bottom")
        if "nth" in selector and (type(selector["nth"]) is not int or selector["nth"] < 1):
            raise ValueError("OCR nth must be a positive integer")
        if "exact" in selector and type(selector["exact"]) is not bool:
            raise ValueError("OCR exact must be true or false")
        return {"action": name, **selector}
    keys = ACTIONS[name]
    if keys and keys[0] in TEXT_ARGS:
        return {"action": name, keys[0]: rest.strip().strip('"')}
    numbers = [int(float(n)) for n in re.findall(r"-?\d+(?:\.\d+)?", rest)]
    if keys == ("points",):
        if len(numbers) < 4 or len(numbers) % 2:
            raise ValueError(f"path needs 2+ x y pairs: {line[:100]}")
        return {"action": name, "points": [numbers[i:i + 2] for i in range(0, len(numbers), 2)][:20]}
    if len(numbers) != len(keys):
        raise ValueError(f"{name} needs {len(keys)} numbers: {line[:100]}")
    action = {"action": name, **dict(zip(keys, numbers, strict=True))}
    if name == "tap" and "precise" in rest.lower():
        action["precise"] = True
    return action


def parse_actions(text: str) -> list[dict]:
    """Parse a THINK / DO… reply (an optional ZH line is kept); the thought goes on the first action."""
    thought = re.search(r"^\s*THINK\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)
    zh = re.search(r"^\s*ZH\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)
    acts = [parse_command(line) for line in re.findall(r"^\s*DO\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)][:6]
    if not acts:
        raise ValueError(f"no DO line in reply: {text[:200]}")
    acts[0] = {"thought": thought.group(1) if thought else "", "thought_zh": zh.group(1) if zh else "", **acts[0]}
    return acts


def parse_plan(text: str) -> tuple[str, str]:
    """Split an optional EN: / ZH: plan reply; a plain reply is used for both."""
    m = re.search(r"EN\s*[:：](.*?)ZH\s*[:：](.*)", text, re.S | re.I)
    return (m.group(1).strip(), m.group(2).strip()) if m else (text.strip(), text.strip())


def _key(step: dict) -> dict:
    """Identity contains only declared arguments, regardless of new feedback fields."""
    name = step.get("action")
    keys = ("action", *ACTIONS.get(name, ()))
    if name in ("find_text", "tap_text"):
        keys += ("region", "nth", "exact")
    identity = {key: step[key] for key in keys if key in step}
    if name in ("find_text", "tap_text"):
        identity["text"] = ocr.normalize_text(identity.get("text", ""))
        for key, default in (("exact", False), ("region", None), ("nth", None)):
            if identity.get(key) == default:
                identity.pop(key, None)
    return identity


def _same_text_target(previous: dict, current) -> bool:
    """Different selectors can still resolve to the same observed OCR target."""
    old = previous.get("text_match")
    return bool(old and current is not None
                and ocr.normalize_text(old["text"]) == ocr.normalize_text(current.text)
                and tuple(old["box"]) == tuple(current.box))


def _diff_feedback(step: dict) -> str:
    """Keep changed text visible in model history without the full region geometry."""
    diff = step.get("screen_diff")
    if not diff:
        return ""
    compact = {key: diff.get(key) for key in ("status", "changed_fraction", "text_status")}
    for key in ("texts_appeared", "texts_disappeared"):
        texts = diff.get(key)
        compact[key] = None if texts is None else [text[:60] for text in texts[:3]]
    return " -> screen diff: " + json.dumps(compact, ensure_ascii=False)


class BlindError(RuntimeError):
    """The screen feed is gone; acting now would be guessing."""


class ObservationInvalidated(ValueError):
    """The original observation cannot authorize further actions."""


class ObservationContentChanged(ObservationInvalidated):
    """A new frame in the same mirror session differs from the observed frame."""


# The shared Phone and TextTargetGuard also report observation failures at the write boundary.
_EXECUTION_OBSERVATION_FAILURES = {
    "画面不够新鲜或正在重放，未发送点击；请重新观察。",
    "目标观察后画面连接已变化，未发送点击；请重新观察。",
    "点击前画面无效，未发送点击。",
    "指针定位期间目标或屏幕已变化，未发送点击；请重新观察。",
    "指针定位期间画面连接已变化，请重新观察。",
    "唤醒后仍没有足够新鲜的画面，未发送点击；请检查屏幕镜像。",
    "唤醒期间画面连接已变化，请重新观察。",
    "点击前画面连接已变化，未发送点击；请重新观察。",
    "返回主屏幕期间画面连接已变化，请重新观察。",
    "HID 命令已发送；没有实时画面，无法确认主屏幕。",
}


class Agent:
    def __init__(self, phone: Phone, screen: Screen, actor: LLM, planner: LLM, trace: Trace,
                 max_steps: int = 40, max_failures: int = 3):
        self.phone, self.screen, self.actor, self.planner = phone, screen, actor, planner
        self.trace = trace
        self.max_steps, self.max_failures = max_steps, max_failures
        self.task, self.plan, self.steps = "", "", []
        self.shots: dict[int, bytes] = {}  # step index -> screenshot the model decided on
        self._pointer = (0.0, 0.0)  # last pointer position (screen fractions)
        self._job: asyncio.Task | None = None
        self._landing_failure = ""
        self._app_search: tuple[str, tuple[float, float, float, float], int] | None = None

    @property
    def running(self) -> bool:
        return self._job is not None and not self._job.done()

    def start(self, task: str) -> None:
        if self.running:
            raise RuntimeError("agent already running")
        if not self.screen.live:
            raise RuntimeError("no phone screen: start Screen Mirroring first")
        self.task, self.plan, self.steps, self.shots = task, "", [], {}
        self._pointer = (0.0, 0.0)
        self._landing_failure, self._app_search = "", None
        self.trace.start(task)
        self._job = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self.running:
            self._job.cancel()

    def clear(self) -> None:
        if self.running:
            raise RuntimeError("stop the agent first")
        self.task, self.plan, self.steps, self.shots = "", "", [], {}
        self._landing_failure, self._app_search = "", None

    def _record(self, shot: bytes | None = None, **step) -> None:
        step = {key: value for key, value in step.items() if not key.startswith("_")}
        if shot:
            self.shots[len(self.steps)] = shot
        self.steps.append({"t": time.time(), "shot": bool(shot), **step})
        self.trace.write(kind="step", **step)
        # 委托模式的逐步正文可能含屏幕上的字。平常只记动作名；HIDAGENT_DEBUG=1 才记整步。
        if os.environ.get("HIDAGENT_DEBUG") == "1":
            log.info("agent: %s", step)
        else:
            log.info("agent: %s", step.get("action", "step"))

    def _device_session(self) -> tuple:
        device = getattr(self.phone, "device", None)
        return device, getattr(device, "links", None), getattr(device, "abort_epoch", None)

    def _validate_session(self, observation: FrameSnapshot, device_session: tuple) -> None:
        device, links, abort_epoch = device_session
        if (snapshot(self.screen).generation != observation.generation
                or getattr(self.phone, "device", None) is not device
                or getattr(device, "links", None) != links
                or getattr(device, "abort_epoch", None) != abort_epoch):
            raise ObservationInvalidated("执行期间设备或画面会话已变化；已停止，未继续后续操作。")

    async def _ask(self, role: str, llm: LLM, system: str, prompt: str,
                   temperature: float = 0) -> tuple[str, float, FrameSnapshot, tuple]:
        # The image and device belong to one session before any await. Neither
        # a wake/refresh nor the model response may rebind an existing decision.
        original, device_session = snapshot(self.screen), self._device_session()
        ensure = getattr(self.phone, "ensure_observation", None)
        try:
            with input_attempt(before_write=lambda: self._validate_session(original, device_session)):
                accepted = await ensure() if ensure is not None else fresh_frame(self.screen)
        except RuntimeError as exc:
            self._validate_session(original, device_session)
            raise BlindError("lost the phone screen: reconnect Screen Mirroring") from exc
        self._validate_session(original, device_session)
        if accepted is None:  # never let the model act on stale video
            raise BlindError("lost the phone screen: reconnect Screen Mirroring")
        return await self._ask_from_frame(role, llm, system, prompt, accepted, temperature,
                                          device_session=device_session)

    async def _ask_from_frame(self, role: str, llm: LLM, system: str, prompt: str,
                              accepted: FrameSnapshot, temperature: float = 0, *,
                              device_session: tuple) -> tuple[str, float, FrameSnapshot, tuple]:
        self._validate_session(accepted, device_session)
        frame, started = accepted.frame, time.time()
        try:
            reply = await llm.chat(system, prompt, frame, temperature=temperature)
        except Exception:
            self._validate_session(accepted, device_session)
            raise
        self._validate_session(accepted, device_session)
        seconds = round(time.time() - started, 2)
        self.trace.write(frame, kind="llm", role=role, model=llm.model, prompt=prompt,
                         reply=reply, seconds=seconds)
        return reply, seconds, accepted, device_session

    async def _run(self) -> None:
        failures = 0
        try:
            await self._replan()
            for _ in range(self.max_steps):
                try:
                    if await self._step():
                        return
                    if self._landing_failure:
                        last = self.steps[-1]
                        if last.get("changed") is not True and last.get("action") != "open":
                            self._record(action="fail", phase="pointer_landing",
                                         reason="换用有依据的操作后仍未观察到进展，请查看当前画面。")
                            return
                        self._landing_failure = ""
                    failures = 0
                except BlindError:
                    raise
                except ObservationInvalidated as exc:
                    self._record(action="fail", phase="observation", reason=str(exc))
                    return
                except AppSearchError as exc:
                    self._record(action="fail", phase="app_search", reason=str(exc))
                    return
                except PointerLandingError as exc:
                    if self._landing_failure or self._app_search:
                        self._record(action="fail", phase="pointer_landing", reason=str(exc))
                        return
                    self._landing_failure = str(exc)
                    self._record(action="hiccup", phase="pointer_landing", reason=self._landing_failure)
                except Exception as e:  # a bad reply or a dropped link: recover, don't crash
                    if self._landing_failure or self._app_search:
                        phase = "app_search" if self._app_search else "pointer_landing"
                        self._record(action="fail", phase=phase, reason=str(e)[:300])
                        return
                    self._record(action="hiccup", reason=str(e)[:300])
                    board_drop = isinstance(e, DeviceError) and await self.phone.device.wait_connected(15)
                    failures += 0 if board_drop else 1  # the board came back: not the agent's fault
                    if failures >= self.max_failures:
                        self._record(action="fail", reason=f"{failures} errors in a row")
                        return
                    await self.screen.settled()
                    await self._replan(stuck=True)
            self._record(action="fail", reason="step limit reached")
        except asyncio.CancelledError:
            self._record(action="stopped")
        except ObservationInvalidated as exc:
            self._record(action="fail", phase="observation", reason=str(exc))
        except Exception as e:  # e.g. the planner itself is unreachable
            self._record(action="error", reason=repr(e))

    async def _step(self) -> bool:
        """One see-think-act round (one or more actions). Returns True when the task is over."""
        acts = await self._next_actions()
        try:
            self._validate_context(acts[0])
        except ObservationContentChanged:
            if self._landing_failure or self._app_search:
                raise
            accepted = fresh_frame(self.screen)
            if accepted is None or accepted.generation != acts[0]["_observation"].generation:
                raise ObservationInvalidated("重新观察时画面已过期或连接已变化；已停止，未发送旧动作。") from None
            self._record(action="hiccup", phase="observation_preflight",
                         reason="首个动作前画面已变化。旧批次已丢弃，正在重新观察。")
            # This is before _do, so the batch has not attempted any action. Use the fresh
            # frame already received instead of a wake that could move the pointer.
            try:
                acts = await self._next_actions(accepted=accepted, device_session=acts[0]["_device_session"])
            except Exception as exc:
                raise ObservationInvalidated("重新观察后未获得可执行动作；已停止。") from exc
            if any(act["action"] == "replan" for act in acts) or self._repeating(acts[0]):
                raise ObservationInvalidated("重新观察后未能选择可执行动作；已停止。") from None
            self._validate_context(acts[0])  # a second change stops the run
        original_session = acts[0]["_observation"]
        device_session = acts[0]["_device_session"]

        def validate_execution_session():
            # Phone's guards can fail in several localised ways. The original
            # mirror/device counters, not an error message, decide whether the
            # run may continue. Keep one binding across the whole action batch.
            self._validate_session(original_session, device_session)

        first = acts[0]["action"]
        bounded_tap = first == "tap" and "box" in acts[0]
        if (self._landing_failure and (len(acts) != 1
                or (not bounded_tap and first not in {"tap_text", "open", "home", "back", "fail", "done"}))):
            self._record(action="fail", phase="pointer_landing", reason=self._landing_failure)
            return True
        if (self._app_search or any(act["action"] == "open" for act in acts)) and len(acts) != 1:
            raise AppSearchError("App 搜索和选择结果各需单独一步，不能与其他操作一起发送。")
        seen = acts[0]["_shot"]
        spots = [self._pointer]  # where the pointer was and goes: pointer moves are not screen changes
        stuck = self._repeating(acts[0])
        if stuck and self._app_search:
            raise AppSearchError("App 搜索没有进展，请核对当前搜索结果。")
        for index, act in enumerate(acts):
            if index:
                self._validate_context(act)
            name = act["action"]
            if name == "replan" or stuck:
                act.pop("_observation", None)
                self._record(act.pop("_shot"), **act)
                await self._replan(stuck=stuck)
                return False
            if name not in ("done", "fail"):
                if self._missed() and _key(act) == _key(self._last_input()):
                    raise ValueError("automatic repeat blocked: the previous input outcome is unchanged or unknown")
                started = time.time()
                before = self.screen.frame
                try:
                    with input_attempt(before_write=validate_execution_session) as attempted_input:
                        validate_execution_session()
                        tap_completed = await self._do(act)
                        validate_execution_session()
                except (Exception, asyncio.CancelledError) as exc:
                    # A click, key, or text write may already have left the host. Record that
                    # uncertain attempt before recovery can plan the same action again.
                    if attempted_input.written:
                        attempted = {key: value for key, value in act.items()
                                     if not key.startswith("_")}
                        attempted.update(outcome="unknown", changed=None, fresh=False, clicked=None,
                                         effect_verified=False, act_s=round(time.time() - started, 2))
                        if isinstance(exc, TypingVerificationError):
                            attempted.update(command_acknowledged=True, **exc.outcome)
                        self._record(act["_shot"], **attempted)
                    if not isinstance(exc, asyncio.CancelledError):
                        try:
                            validate_execution_session()
                        except ObservationInvalidated as session_error:
                            raise session_error from exc
                    if isinstance(exc, PointerLandingError) and name == "tap_text":
                        self._record(action="fail", phase="pointer_landing", reason=str(exc))
                        return True
                    if isinstance(exc, TypingVerificationError):
                        # Post-ACK field failure is not a dropped board and must
                        # retain the original observation-stop behavior.
                        raise ObservationInvalidated(str(exc)) from exc
                    if isinstance(exc, RuntimeError) and str(exc) in _EXECUTION_OBSERVATION_FAILURES:
                        raise ObservationInvalidated(str(exc)) from exc
                    raise
                act["act_s"] = round(time.time() - started, 2)
                spots += _pointer_path(act)
                self._pointer = spots[-1]
                if name in ("tap", "tap_text"):
                    if isinstance(tap_completed, dict):
                        after = tap_completed.pop("_after_frame", None)
                        tap_completed.pop("_after_snapshot", None)
                        usable = tap_completed.get("fresh", False) and after is not None
                        act.update(tap_completed)
                    else:
                        after = await feedback.wait_after_action(self.screen, tap_completed or time.time(), 1.5)
                        usable = after is not None
                    act["screen_diff"] = await screen_diff.summarize(before, after, fresh=usable, ocr_enabled=False)
                    if not usable:
                        act["changed"] = None
                    elif "screen_changed" in act:
                        # Phone already ignored pointer fade and button highlights.
                        act["changed"] = act["screen_changed"]
                    else:
                        act["changed"] = vision.changed(before, after, ignore=tuple(spots))
                    act["effect_verified"] = False
                elif name in ("circle", "path"):
                    after = await feedback.wait_after_action(self.screen, time.time())
                    accepted = fresh_frame(self.screen)
                    if (after is not None and accepted is not None and accepted.frame == after
                            and accepted.generation == act["_observation"].generation):
                        act["changed"] = screen_compare.effect_changed(before, after, ignore=tuple(spots[-1:]))
                    else:
                        act["changed"] = None
            act.pop("_observation", None)
            self._record(act.pop("_shot"), **act)
            if name in ("done", "fail"):
                return True
            if name == "tap_text" and self._app_search:
                if act.get("clicked") is not True or act.get("fresh") is not True or act.get("changed") is not True:
                    raise AppSearchError("App 结果已处理，但未确认画面变化；请查看当前画面。")
                self._app_search = None
            if name in ("find_text", "tap", "tap_text", "open", "circle", "path"):
                return False  # inspect the new screen before acting on it
        # feedback for the next round
        if self.steps[-1]["action"] not in ("tap", "tap_text"):
            await self.screen.settled()
            self.steps[-1]["changed"] = vision.changed(seen, self.screen.frame, ignore=tuple(spots))
        return False

    def _last_input(self) -> dict:
        """Reading the screen or revising a plan does not resolve an input's outcome."""
        return next((step for step in reversed(self.steps)
                     if step.get("action") in ACTIONS
                     and step["action"] not in {"find_text", "wait", "replan", "done", "fail"}), {})

    def _missed(self) -> bool:
        """Unchanged or unknown input evidence cannot authorize an automatic retry."""
        previous = self._last_input()
        if previous.get("outcome") == "unknown":
            return True
        return (previous.get("changed") is False
                or (previous.get("action") in ("tap", "tap_text", "circle", "path")
                    and (previous.get("changed") is not True or previous.get("fresh") is False)))

    def _repeating(self, act: dict, limit: int = 3) -> bool:
        """True when the actor picks the same action as its last limit-1 moves and the last one did nothing.

        Repeating a move that still changes the screen (e.g. scrolling down a long list) is progress, not a loop.
        """
        moves = [s for s in self.steps if "plan" not in s][-(limit - 1):]
        return (len(moves) == limit - 1 and all(_key(m) == _key(act) for m in moves)
                and moves[-1].get("changed") is not True)

    async def _replan(self, stuck: bool = False) -> None:
        prompt = f"Goal: {self.task}"
        if getattr(self.phone, "app_search_available", False):
            prompt += "\nA verified Spotlight shortcut is available to search for an English app name."
        if stuck:
            last = self.steps[-1] if self.steps else {}
            prompt += (f"\n\nThe previous plan got stuck (last event: {last.get('action')} {last.get('reason', '')}):"
                       f"\n{self.plan}\nLook at the current screen and plan from here, another way if needed.")
        reply, seconds, observation, _device_session = await self._ask("planner", self.planner, PLANNER_PROMPT, prompt)
        self.plan, plan_zh = parse_plan(reply)
        self._record(observation.frame, action="replan (stuck)" if stuck else "plan", plan=self.plan, plan_zh=plan_zh,
                     think_s=seconds)

    async def _next_actions(self, *, accepted: FrameSnapshot | None = None,
                            device_session: tuple | None = None) -> list[dict]:
        # actions and their effect only: echoing earlier thoughts makes the model repeat them instead of looking
        hidden = ("t", "plan", "plan_zh", "think_s", "act_s", "shot", "thought", "thought_zh", "changed", "screen_diff")
        effect = {True: " -> screen changed", False: " -> screen did NOT change"}
        history = "\n".join(json.dumps({k: v for k, v in s.items() if k not in hidden})[:300]
                            + effect.get(s.get("changed"), "") + _diff_feedback(s)
                            for s in self.steps[-8:] if "plan" not in s) or "(none)"
        prompt = f"Goal: {self.task}\n\nPlan:\n{self.plan}\n\nRecent actions:\n{history}\n\nNext action?"
        app_search_available = getattr(self.phone, "app_search_available", False)
        system = ACTOR_PROMPT + (APP_SEARCH_PROMPT if app_search_available else "")
        if not ocr.available():
            prompt += "\nLocal OCR is unavailable. Do not use find_text or tap_text."
        if self._landing_failure:
            shortcut = "open, " if app_search_available else ""
            prompt += ("\nThe previous tap was withheld because the pointer landing could not be verified. "
                       "Use one tap with the entire visible control box or one exact tap_text on an observed label. "
                       f"You can also use a supported {shortcut}home or back action. "
                       "If none applies, fail and name the obstacle. Do not retry a point-only tap or replan.")
        if self._app_search:
            app, field, _generation = self._app_search
            labels = APP_SEARCH_LABELS.get(ocr.normalize_text(app), (app,))
            visible_labels = ", ".join(sorted(labels))
            prompt += (f"\nSpotlight was filled with {app!r}. On this new screen, select the clearly visible app "
                       f"result using a single exact tap_text for a visible label ({visible_labels}) in a narrow "
                       f"region outside the search field {field}. If the result is ambiguous, fail. "
                       "Do not press Enter.")
        if accepted is not None:
            prompt += ("\nThe previous batch was discarded before acting. Use this new screenshot to choose an "
                       "action under the current plan. If that is not possible, fail and say why.")
        for attempt in range(1 if accepted is not None else 3):
            if accepted is None:
                reply, seconds, observation, device_session = await self._ask(
                    "actor", self.actor, system, prompt, 0.4 * attempt)
            else:
                if device_session is None:
                    raise ObservationInvalidated("重新观察缺少原设备会话，未发送动作。")
                reply, seconds, observation, device_session = await self._ask_from_frame(
                    "actor", self.actor, system, prompt, accepted, device_session=device_session)
            try:
                acts = parse_actions(reply)
            except ValueError as e:
                log.warning("unparseable actor reply (%s): %s", e, reply[:200])
                continue
            previous = self._last_input()
            duplicate = _key(acts[0]) == _key(previous)
            if (self._missed() and not duplicate and acts[0]["action"] == "tap_text"
                    and previous.get("action") == "tap_text"):
                candidate = acts[0]
                region = candidate.get("region")
                matches = await ocr.find_matches_async(
                    observation.frame, candidate["text"], candidate.get("exact", False), region=region)
                target = ocr.select_match(matches, candidate["text"], nth=candidate.get("nth"), region=region)
                duplicate = _same_text_target(previous, target)
            if self._missed() and duplicate:
                log.info("actor repeated an action without confirmed progress; asking for another")
                reason = ("it changed nothing last time" if previous.get("changed") is False
                          else "its outcome is unknown; the first input may already have executed")
                prompt += (f"\n\nYou just proposed {json.dumps(_key(acts[0]))} again, but {reason}. "
                           "Do not repeat it automatically. Inspect the result, choose a different action, "
                           "or stop for the user to check.")
                continue
            acts[0]["think_s"] = seconds
            return [{**a, "_shot": observation.frame, "_observation": observation,
                     "_device_session": device_session} for a in acts]
        raise ValueError(f"actor gave no valid action: {reply[:200]}")

    def _validate_context(self, action: dict, *, stage: str = "context", cursor=None, reference=None) -> FrameSnapshot:
        """Every write still belongs to the observation, with a stage-specific pixel check.

        ``context`` requires the original frame (paste and the first chunk). Later
        stages allow field edits only within a region confirmed in that original.
        """
        original = action.get("_observation")
        if isinstance(original, FrameSnapshot) and "_device_session" in action:
            self._validate_session(original, action["_device_session"])
        current = fresh_frame(self.screen)
        if current is None:
            raise BlindError("lost the phone screen: reconnect Screen Mirroring")
        if not isinstance(original, FrameSnapshot) or original.frame != action.get("_shot"):
            raise ObservationInvalidated("original screen observation is invalid; discard queued actions")
        if current.generation != original.generation:
            raise ObservationInvalidated("screen mirroring session changed; discard queued actions and observe again")
        if action["action"] in {"back", "home"}:
            if "_navigation_context" not in action:
                action["_navigation_context"] = for_phone(self.phone, action["action"], original)
            navigation = action["_navigation_context"]
            if navigation is not None:
                if (not isinstance(navigation, NavigationContext) or navigation.action != action["action"]
                        or navigation.observation != original):
                    raise ObservationInvalidated("invalid system navigation context")
                try:
                    navigation.validate_current(self.screen, cursor=cursor, reference=reference)
                except RuntimeError as exc:
                    raise ObservationContentChanged(str(exc)) from exc
                self._validate_app_search_action(action, current)
                return current
        if stage in {"accept", "chunk"}:
            if "_input_guard" not in action:
                action["_input_guard"] = InputContextGuard(self.screen, original)
            try:
                return action["_input_guard"](stage=stage)
            except RuntimeError as exc:
                raise ObservationInvalidated(str(exc)) from exc
        if cursor is not None:
            if "_gesture_context" not in action:
                action["_gesture_context"] = TextTargetGuard(
                    self.screen, original.frame, SimpleNamespace(center=cursor[0]), observation=original)
            try:
                action["_gesture_context"](cursor=cursor, reference=reference)
            except RuntimeError as exc:
                raise ObservationContentChanged(str(exc)) from exc
        elif screen_compare.observation_changed(
            original.frame, current.frame,
            caret_frames=getattr(self.screen, "recent_frames", ())):
            raise ObservationContentChanged("screen changed since the original observation; discard queued actions")
        self._validate_app_search_action(action, current)
        return current

    async def _do(self, a: dict) -> dict | float | None:
        observation = self._validate_context(a)
        p = lambda k: a[k] / 1000  # noqa: E731
        match a["action"]:
            case "tap":
                target = SimpleNamespace(center=(p("x"), p("y")))
                options = {}
                if "box" in a:
                    target.box = tuple(value / 1000 for value in a["box"])
                    options = {"target_box": target.box, "target_frame": observation.frame,
                               "allow_estimated_landing": False}
                guard = TextTargetGuard(self.screen, observation.frame, target, observation=observation)
                return await self.phone.tap_verified(*target.center, before_click=guard, **options)
            case "find_text" | "tap_text": return await self._text_action(a)
            case "swipe":
                await self.phone.swipe(p("x1"), p("y1"), p("x2"), p("y2"),
                                       before_action=lambda **evidence: self._validate_context(a, **evidence))
            case "path":
                await self.phone.drag([(x / 1000, y / 1000) for x, y in a["points"]],
                                      verify_start=True,
                                      before_action=lambda **evidence: self._validate_context(a, **evidence))
            case "circle":
                await self.phone.drag(circle(p("x"), p("y"), p("r"), self.screen.aspect),
                                      verify_start=True,
                                      before_action=lambda **evidence: self._validate_context(a, **evidence))
            case "type":
                await self.phone.type(
                    a["text"],
                    before_action=lambda stage="context": self._validate_context(a, stage=stage))
            case "key": await self.phone.key(a["combo"], before_action=lambda: self._validate_context(a))
            case "open":
                if not getattr(self.phone, "app_search_available", False):
                    raise AppSearchError("App 搜索快捷键尚未在当前蓝牙设备上验证，或本地文字识别不可用。")
                field = await self.phone.open_app_search(a["app"], before_action=lambda: self._validate_context(a))
                self._app_search = (a["app"], field, observation.generation)
            case "home":
                options = {"navigation_context": a["_navigation_context"]} if a.get("_navigation_context") else {}
                await self.phone.go_home(**options)
            case "back":
                options = {"navigation_context": a["_navigation_context"]} if a.get("_navigation_context") else {}
                await self.phone.go_back(before_action=lambda **evidence: self._validate_context(a, **evidence),
                                         **options)
            case "wait": pass
            case other: raise ValueError(f"unknown action {other!r}")

    def _validate_app_search_action(self, action: dict, observation: FrameSnapshot) -> None:
        if not self._app_search or action["action"] == "fail":
            return
        app, field, generation = self._app_search
        if observation.generation != generation:
            raise AppSearchError("App 搜索期间画面连接已变化，未选择结果。")
        query = ocr.normalize_text(app)
        labels = APP_SEARCH_LABELS.get(query, (query,))
        if (action["action"] not in {"find_text", "tap_text"} or action.get("exact") is not True
                or ocr.normalize_text(action.get("text", "")) not in labels
                or action.get("nth") is not None):
            raise AppSearchError("只能按当前画面中唯一且精确匹配的 App 名选择搜索结果。")
        region = ocr.validate_region(action.get("region"))
        if (region is None or (region[0] < field[2] and region[2] > field[0]
                               and region[1] < field[3] and region[3] > field[1])):
            raise AppSearchError("App 结果区域必须与搜索输入框分开。")

    async def _text_action(self, action: dict) -> dict | float | None:
        observation = self._validate_context(action)
        seen, region = observation.frame, action.get("region")
        if self._app_search:
            app, field, _generation = self._app_search
            query = await ocr.find_matches_async(seen, app, True, region=field)
            if len(query) != 1:
                raise AppSearchError("当前画面无法确认搜索框中仍是 App 名，未选择结果。")
        matches = await ocr.find_matches_async(seen, action["text"], action.get("exact", False), region=region)
        self._validate_context(action)
        action["text_matches"] = [{"text": item.text, "box": item.box, "center": item.center, "conf": item.conf}
                                  for item in matches]
        match = (ocr.select_match(matches, action["text"], nth=action.get("nth"), region=region)
                 if action["action"] == "tap_text" else (matches[0] if len(matches) == 1 else None))
        action["text_match"] = None if match is None else {
            "text": match.text, "box": match.box, "center": match.center, "conf": match.conf}
        if action["action"] == "tap_text":
            if match is None:
                raise ValueError("未找到屏幕文字，请重新观察")
            if (self._missed() and self._last_input().get("action") == "tap_text"
                    and _same_text_target(self._last_input(), match)):
                raise ValueError("automatic repeat blocked: the previous input outcome is unchanged or unknown")
            guard = TextTargetGuard(self.screen, seen, match, observation=observation)
            with send_target_scope(is_send_target(action["text"], match.text)):
                return await self.phone.tap_verified(*match.center, before_click=guard,
                                                      target_box=match.box, target_frame=seen)


def _pointer_path(act: dict) -> list[tuple[float, float]]:
    """Screen points (fractions) the pointer passes through for one action."""
    f = lambda x, y: (x / 1000, y / 1000)  # noqa: E731
    match act["action"]:
        case "tap": return [f(act["x"], act["y"])]
        case "tap_text": return [tuple(act["text_match"]["center"])] if act.get("text_match") else []
        case "swipe": return [f(act["x1"], act["y1"]), f(act["x2"], act["y2"])]
        case "path": return [f(*p) for p in act["points"]]
        case "circle": return [f(act["x"] + act["r"], act["y"])]
        case "home": return [(0.5, 0.995), (0.5, 0.6)]
        case "back": return [(0.005, 0.45), (0.6, 0.45)]
        case _: return []


def circle(x: float, y: float, r: float, aspect: float, n: int = 24) -> list[tuple[float, float]]:
    """Points around a circle; r is a fraction of the width, aspect = width / height."""
    return [(x + r * math.cos(2 * math.pi * i / n), y + r * aspect * math.sin(2 * math.pi * i / n))
            for i in range(n + 1)]
