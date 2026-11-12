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
from .device import DeviceError
from .freshness import FrameSnapshot, fresh_frame
from .llm import LLM
from .phone import Phone
from .screen import Screen
from .text_target import TextTargetGuard

log = logging.getLogger(__name__)

PLANNER_PROMPT = """You plan tasks on an iPhone. Given the goal and the current screen,
write a short numbered plan (at most 6 steps) of what to do on screen. Plain text only, in English.
Plan only what the goal needs, from what you can see; never invent accounts, emails, logins or
screens you have not seen. Later steps can stay general (e.g. "Draw the face on the canvas").
To open an app, prefer search over paging through home screens: tap the "Search" pill above
the dock (or swipe down from the middle of the home screen), type the app name, tap the result."""

ACTOR_PROMPT = """You operate an iPhone through a mouse pointer. You see the current screen.
Reply in this format, nothing else:
THINK: <plain English a non-technical user understands: first check your last action against the
       screen (did it do what you intended?), then what you do now and why>
DO: <action>

Give several DO lines (up to 6, run in order) whenever the next actions do not depend on seeing the
result: e.g. several drawing strokes or editing keys. A tap ends the round; inspect its result before
typing or pressing Enter.
Give ONE DO line when the next action depends on what this one opens or changes.
Use find_text and tap_text alone, then inspect the result before continuing.

Coordinates are integers 0-1000 relative to the screenshot (x left->right, y top->bottom).
Aim at the visual center of the element you want, not its edge or its label.
Look before you act: describe what is really on the screen now, not what you expect.
Each recent action says whether the screen changed after it. If it did NOT change, the action
missed or had no effect: do not assume it worked; aim again more precisely or try another way.
If feedback is stale, the tap outcome is unknown; inspect a new screen before continuing.
Only type when a text field is visibly focused (the keyboard is showing).
Swipes have no momentum: content moves exactly as far as the finger and stops. Prefer short swipes
(100-250 units) and check the result; repeat if needed rather than one big swipe.
Time or number wheels (e.g. an alarm's time): first tap the number in the wheel's center row and type
the digits (e.g. type 0700); check the result. Only if typing does not work, swipe inside that column:
measure the row spacing from two neighbouring numbers and swipe k times that spacing to move k rows.
Looking for an item in a list: scroll less than one screen at a time so nothing is skipped, and read every
visible item. If you reached both ends of the list and the item is not there, stop with fail and name the
closest options you saw; do not keep scrolling.
Only tap things you can actually see on this screenshot; never guess where a button might be.
If you are unsure what an element does, tap it alone (one DO line) and check the result next round.
Actions:
  tap X Y [precise]         (precise: the pointer is located and corrected before tapping, about 1 s slower;
                             use it for small or tightly packed targets: list rows, checkmarks, toggles, small icons)
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
DO: tap 152 362"""

# action name -> argument names; numeric arguments are 0-1000 screen coordinates
ACTIONS = {"tap": ("x", "y"), "swipe": ("x1", "y1", "x2", "y2"), "circle": ("x", "y", "r"), "path": ("points",),
           "type": ("text",), "key": ("combo",), "done": ("answer",), "fail": ("reason",),
           "find_text": ("text",), "tap_text": ("text",),
           "home": (), "back": (), "wait": (), "replan": ()}
TEXT_ARGS = ("text", "combo", "answer", "reason")


def parse_command(line: str) -> dict:
    """One DO line, e.g. "tap 152 362" -> {"action": "tap", "x": 152, "y": 362}."""
    name, _, rest = line.strip().partition(" ")
    name = name.lower()
    if name not in ACTIONS:
        raise ValueError(f"unknown action: {line[:100]}")
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
    if name == "tap":
        keys += ("precise",)
    elif name in ("find_text", "tap_text"):
        keys += ("region", "nth", "exact")
    return {key: step[key] for key in keys if key in step}


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
        self.trace.start(task)
        self._job = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self.running:
            self._job.cancel()

    def clear(self) -> None:
        if self.running:
            raise RuntimeError("stop the agent first")
        self.task, self.plan, self.steps, self.shots = "", "", [], {}

    def _record(self, shot: bytes | None = None, **step) -> None:
        if shot:
            self.shots[len(self.steps)] = shot
        self.steps.append({"t": time.time(), "shot": bool(shot), **step})
        self.trace.write(kind="step", **step)
        # 委托模式的逐步正文可能含屏幕上的字。平常只记动作名；HIDAGENT_DEBUG=1 才记整步。
        if os.environ.get("HIDAGENT_DEBUG") == "1":
            log.info("agent: %s", step)
        else:
            log.info("agent: %s", step.get("action", "step"))

    async def _ask(self, role: str, llm: LLM, system: str, prompt: str,
                   temperature: float = 0) -> tuple[str, float, FrameSnapshot]:
        ensure = getattr(self.phone, "ensure_observation", None)
        try:
            accepted = await ensure() if ensure is not None else fresh_frame(self.screen)
        except RuntimeError as exc:
            raise BlindError("lost the phone screen: reconnect Screen Mirroring") from exc
        if accepted is None:  # never let the model act on stale video
            raise BlindError("lost the phone screen: reconnect Screen Mirroring")
        frame, started = accepted.frame, time.time()
        reply = await llm.chat(system, prompt, frame, temperature=temperature)
        seconds = round(time.time() - started, 2)
        self.trace.write(frame, kind="llm", role=role, model=llm.model, prompt=prompt,
                         reply=reply, seconds=seconds)
        return reply, seconds, accepted

    async def _run(self) -> None:
        failures = 0
        try:
            await self._replan()
            for _ in range(self.max_steps):
                try:
                    if await self._step():
                        return
                    failures = 0
                except BlindError:
                    raise
                except Exception as e:  # a bad reply or a dropped link: recover, don't crash
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
        except Exception as e:  # e.g. the planner itself is unreachable
            self._record(action="error", reason=repr(e))

    async def _step(self) -> bool:
        """One see-think-act round (one or more actions). Returns True when the task is over."""
        acts = await self._next_actions()
        seen = acts[0]["_shot"]
        spots = [self._pointer]  # where the pointer was and goes: pointer moves are not screen changes
        stuck = self._repeating(acts[0])
        for act in acts:
            self._validate_context(act)
            name = act["action"]
            if name == "replan" or stuck:
                act.pop("_observation", None)
                self._record(act.pop("_shot"), **act)
                await self._replan(stuck=stuck)
                return False
            if name not in ("done", "fail"):
                started = time.time()
                before = self.screen.frame
                tap_completed = await self._do(act)
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
                    act["changed"] = vision.changed(before, after, ignore=tuple(spots)) if usable else None
                    act["effect_verified"] = False
            act.pop("_observation", None)
            self._record(act.pop("_shot"), **act)
            if name in ("done", "fail"):
                return True
            if name in ("find_text", "tap", "tap_text"):
                return False  # pixel changes alone never establish focus for queued type/key actions
        # feedback for the next round
        if self.steps[-1]["action"] not in ("tap", "tap_text"):
            await self.screen.settled()
            self.steps[-1]["changed"] = vision.changed(seen, self.screen.frame, ignore=tuple(spots))
        return False

    def _missed(self) -> bool:
        """The previous round changed nothing on screen, so a tap may have landed off target."""
        return bool(self.steps) and self.steps[-1].get("changed") is False

    def _repeating(self, act: dict, limit: int = 3) -> bool:
        """True when the actor picks the same action as its last limit-1 moves and the last one did nothing.

        Repeating a move that still changes the screen (e.g. scrolling down a long list) is progress, not a loop.
        """
        moves = [s for s in self.steps if "plan" not in s][-(limit - 1):]
        return (len(moves) == limit - 1 and all(_key(m) == _key(act) for m in moves)
                and moves[-1].get("changed") is not True)

    async def _replan(self, stuck: bool = False) -> None:
        prompt = f"Goal: {self.task}"
        if stuck:
            last = self.steps[-1] if self.steps else {}
            prompt += (f"\n\nThe previous plan got stuck (last event: {last.get('action')} {last.get('reason', '')}):"
                       f"\n{self.plan}\nLook at the current screen and plan from here, another way if needed.")
        reply, seconds, observation = await self._ask("planner", self.planner, PLANNER_PROMPT, prompt)
        self.plan, plan_zh = parse_plan(reply)
        self._record(observation.frame, action="replan (stuck)" if stuck else "plan", plan=self.plan, plan_zh=plan_zh,
                     think_s=seconds)

    async def _next_actions(self) -> list[dict]:
        # actions and their effect only: echoing earlier thoughts makes the model repeat them instead of looking
        hidden = ("t", "plan", "plan_zh", "think_s", "act_s", "shot", "thought", "thought_zh", "changed", "screen_diff")
        effect = {True: " -> screen changed", False: " -> screen did NOT change"}
        history = "\n".join(json.dumps({k: v for k, v in s.items() if k not in hidden})[:300]
                            + effect.get(s.get("changed"), "") + _diff_feedback(s)
                            for s in self.steps[-8:] if "plan" not in s) or "(none)"
        prompt = f"Goal: {self.task}\n\nPlan:\n{self.plan}\n\nRecent actions:\n{history}\n\nNext action?"
        for attempt in range(3):  # retry unparseable replies, a little less deterministically
            reply, seconds, observation = await self._ask("actor", self.actor, ACTOR_PROMPT, prompt, 0.4 * attempt)
            try:
                acts = parse_actions(reply)
            except ValueError as e:
                log.warning("unparseable actor reply (%s): %s", e, reply[:200])
                continue
            if self._missed() and _key(acts[0]) == _key(self.steps[-1]):  # same move that just did nothing
                log.info("actor repeated an action that changed nothing; asking for another")
                prompt += (f"\n\nYou just proposed {json.dumps(_key(acts[0]))} again, but it changed nothing last "
                           "time. Choose a different action (another position, a shorter swipe, or another way).")
                continue
            acts[0]["think_s"] = seconds
            return [{**a, "_shot": observation.frame, "_observation": observation} for a in acts]
        raise ValueError(f"actor gave no valid action: {reply[:200]}")

    def _validate_context(self, action: dict) -> FrameSnapshot:
        """Every queued action still belongs to the coherent observation given to the model."""
        original = action.get("_observation")
        current = fresh_frame(self.screen)
        if current is None:
            raise BlindError("lost the phone screen: reconnect Screen Mirroring")
        if (not isinstance(original, FrameSnapshot) or original.frame != action.get("_shot")
                or current.generation != original.generation
                or screen_compare.observation_changed(original.frame, current.frame)):
            raise ValueError("screen changed since the original observation; discard queued actions and observe again")
        return original

    async def _do(self, a: dict) -> dict | float | None:
        observation = self._validate_context(a)
        p = lambda k: a[k] / 1000  # noqa: E731
        match a["action"]:
            case "tap":
                target = SimpleNamespace(center=(p("x"), p("y")))
                guard = TextTargetGuard(self.screen, observation.frame, target, observation=observation)
                return await self.phone.tap_verified(*target.center, before_click=guard)
            case "find_text" | "tap_text": return await self._text_action(a)
            case "swipe": await self.phone.swipe(p("x1"), p("y1"), p("x2"), p("y2"))
            case "path": await self.phone.drag([(x / 1000, y / 1000) for x, y in a["points"]])
            case "circle": await self.phone.drag(circle(p("x"), p("y"), p("r"), self.screen.aspect))
            case "type": await self.phone.type(a["text"], before_action=lambda: self._validate_context(a))
            case "key": await self.phone.key(a["combo"], before_action=lambda: self._validate_context(a))
            case "home": await self.phone.go_home()
            case "back": await self.phone.go_back()
            case "wait": pass
            case other: raise ValueError(f"unknown action {other!r}")

    async def _text_action(self, action: dict) -> dict | float | None:
        observation = self._validate_context(action)
        seen, region = observation.frame, action.get("region")
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
            guard = TextTargetGuard(self.screen, seen, match, observation=observation)
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
