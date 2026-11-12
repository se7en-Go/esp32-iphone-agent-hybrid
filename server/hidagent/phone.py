"""Touch-like gestures on the phone, built from relative mouse moves.

The pointer is relative, so the server tracks where it is (in mouse units) and moves
from there; every few gestures it slams it into the top-left corner (`home`) again to
cancel drift. Coordinates are fractions of the screen (0..1, origin top-left), so they
work for any frame size.
"""

import asyncio
import io
import json
import logging
import math
import time
from collections import deque
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from . import feedback, freshness, screen_compare, vision
from .assistive import AssistiveTouch
from .device import RELEASE_TIMEOUT, Device, DeviceError, exchange_timing
from .operation import OPERATION_TIMEOUT, bounded_operation, check_operation, shield_cleanup
from .screen import FrameSnapshot
from .text_input import ClipboardWriter, TypeMode, select_type_mode
from .text_target import TextTargetGuard

log = logging.getLogger(__name__)


# 固件一行最多收到 511 个字符（hid_bridge.ino 在 512 处停），id 最长是 100000。
SERIAL_LINE_MAX = 511
TYPE_PREFIX_MAX = len("100000 type ")
TYPE_LIMIT = SERIAL_LINE_MAX - TYPE_PREFIX_MAX


def _pace_version(reply: str) -> int | None:
    parts = reply.strip().split()
    if len(parts) == 2 and parts[0] == "ok" and parts[1].isdigit():
        return int(parts[1])
    return None


def type_chunks(text: str, limit: int = TYPE_LIMIT) -> list[str]:
    """按串口行长上限分块，尽量不在一段空格中间切开；固件保留每块的完整内容。"""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    index = 0
    while index < len(text):
        end = min(index + limit, len(text))
        if end < len(text) and text[end - 1].isspace():
            back = end
            while back > index and text[back - 1].isspace():
                back -= 1
            if back > index:
                end = back
            else:
                nxt = end
                while nxt < len(text) and text[nxt].isspace():
                    nxt += 1
                if nxt < len(text) and nxt + 1 - index <= limit:
                    end = nxt + 1
        part = text[index:end]
        if not part:
            break
        parts.append(part)
        index = end
    return parts

CALIBRATION_MOVE = 200  # mouse units moved diagonally for a manual (one point) calibration; stays on screen
CALIBRATION_STOPS = (150, 350)  # automatic calibration finds the pointer at these distances from the corner
REHOME_EVERY = 10       # gestures between re-homing
HOLD = 0.25             # seconds to rest before lifting, so iOS adds no momentum (the swipe stops where it ends)
TAP_GAP = 0.15          # seconds to stay still after a click, so iOS sees a finished tap, not the start of a drag
NUDGE = 12              # mouse units the pointer is wiggled by to find it on screen
# How long to watch for a nudge to show up. The AirPlay mirror lags: on an iPhone 15 Pro Max (iOS 26.3.1) a
# 12 unit nudge appeared 0.15 to 1.5 s after it was sent, so the old fixed 1.2 s window missed some.
POINTER_WAIT = 2.5      # seconds per direction (nudge, then the way back), never more
# After every (re)connect to TCP 7201 the board replays all video since the last IDR, and iOS sends an IDR only
# when mirroring starts: on the test phone that was 26 minutes of frames, 45 s to catch up. Frames older than
# REPLAY_LAG on the board's clock when they arrived are replay, not the screen as it is now.
# Actions additionally require recent publication, including on still screens.
# Arrival lag above REPLAY_LAG is replay. Freshness uses a tighter shared limit for actions.
REPLAY_LAG = 5          # seconds; a still screen keeps its last live frame
CATCH_UP_WAIT = 15      # leave time for verification inside the operation budget
USB_PACE = 3            # firmware HID_PACE_USB. A saved file with no pace number is this schedule.
AIM_TOLERANCE = 0.008   # screen fraction; a bigger miss is corrected before pressing
MAX_MISS = 0.06         # larger misses may be misreadings; never correct or learn from them
TAP_POINTER_WAIT = 0.12  # opportunistic disc check; a late/hidden pointer uses the calibrated landing
TAP_EFFECT_WAIT = 1.75  # one mirror-lag window after the click; no automatic retry
WAKE_NUDGE = 2
POINTER_FADE_SECONDS = 3.0
OBSERVATION_REFRESH_WAIT = 3.5
PASTE_CONTEXT_WAIT = 3.5  # bounded allowance for a dismissed Shortcut alert to leave the mirror


def _serialized(method):
    """Keep refresh motion and public gestures together, allowing nested gestures."""
    @wraps(method)
    async def run(self, *args, **kwargs):
        task = asyncio.current_task()
        if self._interaction_owner is task:
            return await method(self, *args, **kwargs)
        async with self._interaction_lock:
            self._interaction_owner = task
            try:
                return await method(self, *args, **kwargs)
            finally:
                self._interaction_owner = None
    return run


class Phone:
    operation_timeout = OPERATION_TIMEOUT

    def __init__(self, device: Device, calibration_file: Path, screen=None, *,
                 assistive_touch_position=None, assistive_touch_layout=None, find_text=None,
                 clipboard: ClipboardWriter | None = None):
        self.device, self._file, self.screen = device, calibration_file, screen
        self.clipboard = clipboard
        self._typing = asyncio.Lock()
        self._observation_lock = asyncio.Lock()
        self._interaction_lock = asyncio.Lock()
        self._interaction_owner = None
        # mouse units needed to cross the full screen width / height
        saved = json.loads(calibration_file.read_text()) if calibration_file.exists() else None
        body = saved if isinstance(saved, dict) else {}
        has_span = "w" in body and "h" in body
        self.span = {"w": body["w"], "h": body["h"]} if has_span else None
        # where `home` leaves the pointer, as a screen fraction: not the corner itself on iPhones with rounded
        # corners (the 15 Pro Max stops 91 of 720 px right of it), so every move is counted from there
        self.origin = {"x": body.get("x0", 0.0), "y": body.get("y0", 0.0)}
        pace = body.get("pace")
        self._pace = pace if type(pace) is int else None
        self._pos = None  # pointer position in mouse units; None = unknown
        self._moves = 0
        self._link = 0  # device.links when the pointer was last homed
        # where the pointer really landed (seen on screen) for the screen fraction we aimed at, per axis;
        # a straight-line fit through them corrects every later move, so taps get better as the agent works
        seen = body.get("seen", {})
        self._seen = {axis: deque(map(tuple, seen.get(axis, [])), maxlen=12) for axis in ("x", "y")}
        self._pointer_seen = False
        self._pointer_confirmed = False
        self._last_motion = 0.0
        self._position_before = None
        self._position_from = None
        self._position_started = 0.0
        self._position_completed = 0.0
        self._tap_timing = None
        self._tap_blind_allowed = False
        if screen is not None:
            screen._freshness_phone = self
        self._pointer_spots = ()
        self._clock = None  # board millis minus wall-clock millis, for freshness
        self._clock_uncertainty = 0.0
        self._mono_clock = None  # board millis minus monotonic millis, for nudge ordering
        self._clock_link = None
        self.last_tap = None
        self._effect_frame = self._effect_snapshot = None
        self.assistive_touch = AssistiveTouch(
            self, calibration_file.with_suffix(".assistive-touch.json"), position=assistive_touch_position,
            layout=assistive_touch_layout, find_text=find_text)

    @property
    def calibrated(self) -> bool:
        return self.span is not None

    def _stopped(self, epoch: int) -> bool:
        return getattr(self.device, "abort_epoch", 0) != epoch

    @staticmethod
    def _check_operation() -> None:
        check_operation()

    async def _command(self, command: str) -> None:
        if command != "release":
            self._check_operation()
        await self.device.send(command)

    async def _pull_pace(self) -> None:
        """Ask the board which schedule it is on, right before measuring a new span."""
        query = getattr(self.device, "query", None)
        version = getattr(self.device, "pace_version", None)
        if query is not None:
            try:
                reply = await query("pace")
            except DeviceError:
                reply = ""
            parsed = _pace_version(reply)
            if parsed is not None:
                version = parsed
                if hasattr(self.device, "pace_version"):
                    self.device.pace_version = parsed
        self.adopt_pace(version)

    async def _send(self, epoch: int, command: str, *, before_write=None) -> float:
        if self._stopped(epoch):
            raise DeviceError("已紧急停止，后面的内容没有发出。")
        began = time.monotonic()
        validation_ms = 0.0

        def validate():
            nonlocal validation_ms
            started = time.monotonic()
            try:
                before_write()
            finally:
                validation_ms += (time.monotonic() - started) * 1000

        try:
            started = time.monotonic()
            with exchange_timing() as timing:
                guarded = getattr(self.device, "send_guarded", None)
                if before_write is not None and guarded is not None:
                    await guarded(command, validate, write_lock=getattr(self.screen, "_frame_lock", None))
                else:
                    if before_write is not None:
                        validate()
                    await self._command(command)
            sent = timing.sent_at if timing.sent_at is not None else started
        finally:
            if self._tap_timing is not None:
                self._tap_timing["firmware"] += (time.monotonic() - began) * 1000 - validation_ms
                self._tap_timing["positioning"] += validation_ms
        if command == "home" or command.startswith("move "):
            self._last_motion = time.monotonic()
        return sent

    @_serialized
    async def start_calibration(self) -> bool:
        """Find the pointer on screen at two known distances from the corner and fit the mapping from both.

        Two points give the scale and the offset, which one point cannot (iOS pointer motion is not exactly
        proportional). Returns True when it calibrated itself; otherwise the user clicks the pointer
        (finish_calibration).
        """
        self._pos = None
        epoch = getattr(self.device, "abort_epoch", 0)
        await self._pull_pace()
        await self._send(epoch, "home")
        seen = []
        for units in CALIBRATION_STOPS:
            step = units - (seen[-1][0] if seen else 0)
            await self._send(epoch, f"move {step} {step}")
            found = await self._find_pointer(epoch=epoch) if self.screen is not None and self.screen.live else None
            if not found:
                break
            seen.append((units, *found))
        fit = self._fit(seen) if len(seen) == 2 else None
        if fit is None:
            log.info("calibration: pointer not found on screen (%s), waiting for a click", seen)
            # the user clicks the pointer next, and finish_calibration counts from exactly this spot
            await self._send(epoch, "home")
            await self._send(epoch, f"move {CALIBRATION_MOVE} {CALIBRATION_MOVE}")
            return False
        span, origin = fit
        self.span, self.origin = span, origin
        for samples in self._seen.values():
            samples.clear()
        for units, x, y in seen:  # where each stop landed, so later landings correct any residual drift
            self._seen["x"].append((origin["x"] + units / span["w"], x))
            self._seen["y"].append((origin["y"] + units / span["h"], y))
        self._pos = (seen[-1][0], seen[-1][0])
        self._moves, self._link = 0, getattr(self.device, "links", 0)
        self._save()
        log.info("calibration: pointer at %s, span %s, origin %s",
                 [(round(x, 3), round(y, 3)) for _, x, y in seen], span,
                 {k: round(v, 3) for k, v in origin.items()})
        return True

    @staticmethod
    def _fit(seen: list) -> tuple[dict, dict] | None:
        """Span and origin from two stops (units, x, y); None if they make no sense."""
        (u1, x1, y1), (u2, x2, y2) = seen
        if x2 - x1 < 0.02 or y2 - y1 < 0.01:  # the pointer did not really move between the stops
            return None
        span = {"w": round((u2 - u1) / (x2 - x1)), "h": round((u2 - u1) / (y2 - y1))}
        origin = {"x": x1 - u1 / span["w"], "y": y1 - u1 / span["h"]}
        if not (-0.05 < origin["x"] < 0.3 and -0.05 < origin["y"] < 0.2):
            return None
        return span, origin

    async def _find_pointer(self, near: tuple[float, float] | None = None,
                            conservative: bool = False, epoch: int | None = None,
                            quick: bool = False) -> tuple[float, float] | None:
        """Wiggle the pointer and find where it rests (the span may still be unknown).

        The mirror lags and stays silent while the screen is still, so the frame taken before the nudge may
        predate the last move. Every frame that arrives is kept and each new one is compared with the ones
        before it; the pointer is the round blob that jumped sideways by about the nudge. If the nudge never
        shows up, the way back is watched the same way. Both waits are bounded (POINTER_WAIT).
        """
        if epoch is None:
            epoch = getattr(self.device, "abort_epoch", 0)
        if quick:
            return await self._quick_pointer(near, epoch)
        await self._catch_up(epoch)
        await self._wait_screen(epoch, self.screen.settled(
            quiet=0.2, least=0.3, most=1.5, transport=conservative))
        if self._stopped(epoch):
            raise DeviceError("已紧急停止，后面的内容没有发出。")
        nudge = NUDGE if near is None or near[0] < 0.9 else -NUDGE  # stay clear of the right edge
        shift = nudge / self.span["w"] if self.span else 0.025 * (1 if nudge > 0 else -1)
        baseline = self._snapshot()
        history = [baseline.frame] if baseline and baseline.frame else []
        sent = await self._send(epoch, f"move {nudge} 0")
        found = await self._watch(epoch, history, near, shift, sent, conservative, True, self._mono_clock)
        # An interrupted or expired search must not issue a cleanup movement.
        back = await self._send(epoch, f"move {-nudge} 0")
        if found is None and not self._stopped(epoch):
            # the nudge was not seen: the pointer rests where the way back leaves it
            found = await self._watch(epoch, history, near, -shift, back, conservative, False, self._mono_clock)
        return found if found and 0.03 < found[0] < 0.97 and 0.03 < found[1] < 0.97 else None

    async def _quick_pointer(self, near: tuple[float, float], epoch: int) -> tuple[float, float] | None:
        """Observe motion first; wake only an idle pointer when a search is necessary."""
        if self._clock_link != getattr(self.device, "links", 0):
            await self._board_clock()
        freshness.require_fresh(self.screen)
        if time.monotonic() - self._last_motion < POINTER_FADE_SECONDS:
            found = await self._observe_landing(near, epoch)
            if found is not None or self._tap_blind_allowed or self._tap_timing is None:
                return found
            return await self._localize_pointer(near, epoch)
        if self._tap_blind_allowed:
            return None  # an already bounded observed target does not need an idle-pointer search
        if self._tap_timing is not None:
            return await self._localize_pointer(near, epoch)
        started = time.time()  # point() has finished; earlier captures cannot describe this landing
        clock = self._clock
        if clock is None or self._clock_link != getattr(self.device, "links", 0):
            clock = await self._board_clock()
        freshness.require_fresh(self.screen, clock=clock)
        age = self._frame_age(clock)
        history = []
        frame_ts = getattr(self.screen, "frame_ts", None)
        if clock is not None and frame_ts is not None:
            relative = freshness.board_delta(frame_ts, started * 1000 + clock)
            if relative >= 0:
                history.append((self.screen.frame, relative))
        since = time.time()
        nudge = WAKE_NUDGE if near[0] < 0.9 else -WAKE_NUDGE
        position, self._pos = self._pos, None
        await self._send(epoch, f"move {nudge} 0")
        back = time.time()
        await self._send(epoch, f"move {-nudge} 0")
        self._pos = position
        if (clock is None or frame_ts is None or (age is not None and age >= TAP_POINTER_WAIT)
                or freshness.MIRROR_LAG_SECONDS >= TAP_POINTER_WAIT):
            return None  # no capture timing, or mirror too late to relate a pair to this wake
        nudged_at, back_at = ((stamp - started) * 1000 for stamp in (since, back))
        deadline = time.monotonic() + TAP_POINTER_WAIT
        shift = nudge / self.span["w"]
        while time.monotonic() < deadline:
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            after = await self.screen.fresh(since, min(.05, max(0, deadline - time.monotonic())))
            if after:
                since = self.screen.updated
                freshness.require_fresh(self.screen, clock=clock)
                timestamp = getattr(self.screen, "frame_ts", None)
                if timestamp is None:
                    continue
                captured = freshness.board_delta(timestamp, started * 1000 + clock)
                if captured < 0:
                    continue
                for before, before_ts in history[-2:]:
                    if not before_ts < captured:
                        continue
                    if before_ts < nudged_at <= captured < back_at:
                        direction = 1
                    elif nudged_at <= before_ts < back_at <= captured:
                        direction = -1
                    else:
                        continue  # both frames on one side of the wake, or missed its outward leg
                    moved = vision.pointer_move(before, after, near, direction * shift,
                                                (MAX_MISS + .03, MAX_MISS + .02), strict=True)
                    if moved:
                        return moved[0] if direction == 1 else moved[1]
                history.append((after, captured))
            await asyncio.sleep(0.005)
        return None

    async def _localize_pointer(self, near, epoch):
        """Required localization pairs a deliberate move and return, never a lone round UI element."""
        position, self._pos = self._pos, None
        nudge = NUDGE if near[0] < .9 else -NUDGE
        shift = nudge / self.span["w"]
        baseline = freshness.require_fresh(self.screen).frame

        async def capture_after(sent):
            deadline = time.monotonic() + feedback.MIRROR_LAG_SECONDS + .3
            cursor = sent
            while time.monotonic() < deadline:
                if self._stopped(epoch):
                    raise DeviceError("已紧急停止，后面的内容没有发出。")
                eligible = freshness.fresh_frame(self.screen, since=sent)
                if eligible is not None:
                    return eligible.frame
                await self.screen.fresh(cursor, min(.1, max(0, deadline - time.monotonic())))
                cursor = max(cursor, getattr(self.screen, "updated", cursor))
                await asyncio.sleep(.005)
            return None

        outward = None
        await self._send(epoch, f"move {nudge} 0")
        try:
            outward = await capture_after(time.time())
        finally:
            if not self._stopped(epoch):
                await self._send(epoch, f"move {-nudge} 0")
                self._pos = position
        returned = await capture_after(time.time())
        if outward is None or returned is None or screen_compare.observation_changed(baseline, returned):
            return None
        paired = vision.pointer_move(outward, returned, near, -shift,
                                      (MAX_MISS + .04, MAX_MISS + .03), strict=True)
        return paired[1] if paired else None

    async def _observe_landing(self, near, epoch):
        """Pair discs across the actual positioning move, without another nudge."""
        age = self._frame_age(self._clock)
        if self._position_before is None or self._position_from is None:
            return None
        if self._tap_blind_allowed and (self._clock is None or (age is not None and age >= TAP_POINTER_WAIT)):
            return None
        search_wait = TAP_POINTER_WAIT if self._tap_blind_allowed else feedback.MIRROR_LAG_SECONDS + .3
        deadline = time.monotonic() + search_wait
        since = self._position_completed
        with Image.open(io.BytesIO(self._position_before)) as image:
            before = image.convert("RGB")
        width, height = before.size
        bounds = (max(0, round((near[0] - .1) * width)), max(0, round((near[1] - .08) * height)),
                  min(width, round((near[0] + .1) * width)), min(height, round((near[1] + .08) * height)))
        old = [(px + bounds[0], py + bounds[1], diameter, color)
               for px, py, diameter, color in vision._discs(before.crop(bounds), width)]
        ox, oy = self._position_from
        source_bounds = (max(0, round((ox - .08) * width)), max(0, round((oy - .06) * height)),
                         min(width, round((ox + .08) * width)), min(height, round((oy + .06) * height)))
        source = [(px + source_bounds[0], py + source_bounds[1], diameter, color)
                  for px, py, diameter, color in vision._discs(before.crop(source_bounds), width)
                  if .025 * width <= diameter <= .065 * width
                  and 90 <= min(color) <= max(color) <= 210 and max(color) - min(color) <= 25]
        if len(source) != 1:
            return None  # no observed starting disc: a new round UI element is not pointer evidence
        sx, sy, size, shade = source[0]

        def same_disc(candidate, previous):
            px, py, diameter, color = candidate
            x, y, old_size, old_color = previous
            return (math.hypot(px - x, py - y) <= max(2, old_size * .15)
                    and abs(diameter - old_size) <= .3 * old_size
                    and max(abs(a - b) for a, b in zip(color, old_color, strict=True)) <= 40)

        while time.monotonic() < deadline:
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            after = await self.screen.fresh(since, min(.05, max(0, deadline - time.monotonic())))
            if after is None:
                continue
            since = self.screen.updated
            snap = freshness.fresh_frame(self.screen, since=self._position_completed)
            if snap is None:
                continue
            with Image.open(io.BytesIO(snap.frame)) as image:
                current = image.convert("RGB")
            if current.size != before.size or screen_compare.observation_changed(self._position_before, snap.frame):
                return None
            remaining = [(px + source_bounds[0], py + source_bounds[1], diameter, color)
                         for px, py, diameter, color in vision._discs(current.crop(source_bounds), width)]
            if any(same_disc(candidate, source[0]) for candidate in remaining):
                continue  # a static source plus a newly appeared circle does not prove pointer motion
            candidates = [(px, py) for px, py, diameter, color in vision._discs(current.crop(bounds), width)
                          if .025 * width <= diameter <= .065 * width
                          and 90 <= min(color) <= max(color) <= 210 and max(color) - min(color) <= 25
                          # Reject unchanged circles, not the departed source's whole diameter:
                          # a legitimate small correction overlaps the old pointer footprint.
                          and not any(same_disc((px + bounds[0], py + bounds[1], diameter, color), previous)
                                      for previous in old)
                          and abs(diameter - size) <= .3 * size
                          and max(abs(a - b) for a, b in zip(color, shade, strict=True)) <= 40
                          and abs((px + bounds[0] - sx) / width - (near[0] - ox)) <= MAX_MISS
                          and abs((py + bounds[1] - sy) / height - (near[1] - oy)) <= MAX_MISS]
            if len(candidates) == 1:
                px, py = candidates[0]
                return (px + bounds[0]) / width, (py + bounds[1]) / height
        return None

    async def _watch(self, epoch: int, history: list, near, shift: float, since: float,
                     conservative: bool, at_start: bool, clock: float | None = None,
                     *, generation: int | None = None) -> tuple[float, float] | None:
        """Watch new frames until one shows the pointer nudged by `shift` from an earlier frame.

        `since` and `clock` are monotonic: board time of the command is `since * 1000 + clock`.
        A frame from before that write can refresh the baseline, but it cannot prove the nudge.
        """
        deadline = time.monotonic() + POINTER_WAIT + (0.5 if conservative else 0)
        sent = since  # fixed command boundary; `since` advances as publications are consumed
        if generation is None and self.screen is not None:
            generation = freshness.snapshot(self.screen).generation
        searches = ([(near, (0.12, 0.08))] if near else []) + [((0.5, 0.5), (0.5, 0.5))]
        while time.monotonic() < deadline:
            self._check_operation()
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            snapshot = await self._fresh_snapshot(since, min(.2, max(0, deadline - time.monotonic())))
            if snapshot:
                after, since = snapshot.frame, snapshot.arrived
                current_generation = getattr(snapshot, "generation", generation)
                if generation is not None and current_generation != generation:
                    raise RuntimeError("指针定位期间画面连接已变化，请重新观察。")
                age = self._frame_age(clock, snapshot)
                if age is not None and age > REPLAY_LAG:
                    history.clear()
                    await asyncio.sleep(.02)
                    continue
                if (clock is not None and snapshot.board_ts is not None
                        and self._board_delta(snapshot.board_ts, sent * 1000 + clock) <= 0):
                    history[:] = [after]
                    await asyncio.sleep(.02)
                    continue
                if not history or after != history[-1]:
                    # the frame before the nudge, and the latest one (the before frame may predate the last move);
                    # each comparison is tens of ms of pure Python, and hogging the GIL starves the video thread
                    for before in dict.fromkeys(history[:1] + history[-1:]):
                        for look, reach in searches:
                            moved = vision.pointer_move(before, after, look, shift, reach)
                            if moved:
                                return moved[0] if at_start else moved[1]
                            await asyncio.sleep(0)
                    history.append(after)
            await asyncio.sleep(.02)
        return None

    async def _board_clock(self) -> float | None:
        """Refresh both clock offsets from `status`.

        The monotonic offset is an upper bound at the actual write, so a frame captured before the
        command cannot look newer. The wall offset is the same bound, for freshness checks that
        compare `updated * 1000 + clock` with the board timestamp. Returns the wall offset.
        """
        self._clock = self._mono_clock = self._clock_link = None
        query = getattr(self.device, "query", None)
        frame_ts = getattr(self.screen, "frame_ts", None)
        if query is None or frame_ts is None:
            return None
        try:
            mono_sent = time.monotonic()
            wall_before = time.time()
            with exchange_timing() as timing:
                up = json.loads(await query("status"))["up"]
            wall_after = time.time()
            mono_after = time.monotonic()
            mono_sent = timing.sent_at if timing.sent_at is not None else mono_sent
            wall_at_write = wall_after - max(0.0, mono_after - mono_sent)
            self._clock_uncertainty = max(0.0, (wall_after - wall_before) / 2)
            self._clock = up + 1 - wall_at_write * 1000
            self._mono_clock = up + 1 - mono_sent * 1000
            self._clock_link = getattr(self.device, "links", 0)
            return self._clock
        except (DeviceError, OSError, ValueError, KeyError, TypeError) as exc:
            raise DeviceError("无法确认画面对应的板子时间；没有继续验证或点击，请重试。") from exc

    @staticmethod
    def _board_delta(later: float, earlier: float) -> float:
        """Signed u32 millis distance, including a wrap (timestamps within half the 49.7 day period)."""
        return (later - earlier + 2**31) % 2**32 - 2**31

    def _snapshot(self) -> FrameSnapshot | None:
        snap = getattr(self.screen, "snapshot", None)
        if isinstance(snap, FrameSnapshot):
            return snap
        if self.screen is None or getattr(self.screen, "frame", None) is None:
            return None
        updated = getattr(self.screen, "updated", time.time())
        arrived = time.monotonic() - max(0.0, time.time() - updated)
        return FrameSnapshot(self.screen.frame, getattr(self.screen, "frame_ts", None), arrived, updated)

    async def _fresh_snapshot(self, since: float, timeout: float) -> FrameSnapshot | None:
        fresh = getattr(self.screen, "fresh_snapshot", None)
        if fresh is not None:
            return await fresh(since, timeout)
        wall_since = time.time() - (time.monotonic() - since)
        after = await self.screen.fresh(wall_since, timeout)
        return self._snapshot() if after else None

    @_serialized
    async def ensure_observation(self):
        """Reacquire a silent live stream with one bounded, serialized pointer wake."""
        async with self._observation_lock:
            if self._clock_link != getattr(self.device, "links", 0) or self._clock is None:
                await self._board_clock()
            current = freshness.fresh_frame(self.screen)
            if current is not None:
                return current
            # A stale host receipt may be a healthy still screen. Replay or a
            # disconnected stream cannot use this exception to authorize motion.
            stale = freshness.require_fresh(self.screen, max_age=float("inf"))
            epoch = getattr(self.device, "abort_epoch", 0)
            try:
                return await asyncio.wait_for(self._refresh_observation(stale, epoch), OBSERVATION_REFRESH_WAIT)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("唤醒后仍没有足够新鲜的画面，未发送点击；请检查屏幕镜像。") from exc

    async def _refresh_observation(self, stale, epoch):
        started = time.time()
        self._pos = None  # clipping a small edge nudge can invalidate a known position
        await self._send(epoch, f"move {WAKE_NUDGE} 0")
        try:
            # Let pre-wake phone-to-board images drain, then induce one more
            # frame even when AirPlay stops publishing a motionless screen.
            await asyncio.sleep(freshness.MIRROR_LAG_SECONDS + .02)
        finally:
            if not self._stopped(epoch):
                await self._send(epoch, f"move {-WAKE_NUDGE} 0")
        cursor = started
        while True:
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            current = freshness.snapshot(self.screen)
            if current.generation != stale.generation:
                raise RuntimeError("唤醒期间画面连接已变化，请重新观察。")
            accepted = freshness.fresh_frame(self.screen, since=started)
            if accepted is not None:
                return accepted
            await self.screen.fresh(cursor, .1)
            cursor = max(cursor, getattr(self.screen, "updated", cursor))
            await asyncio.sleep(.005)

    def _frame_age(self, clock: float | None, snapshot: FrameSnapshot | None = None) -> float | None:
        """Replay delay at arrival. An arrival snapshot uses the monotonic clock; otherwise wall time."""
        if snapshot is not None and clock is not None and snapshot.board_ts is not None:
            return self._board_delta(snapshot.arrived * 1000 + clock, snapshot.board_ts) / 1000
        return freshness.frame_age(freshness.snapshot(self.screen), clock)

    async def _catch_up(self, epoch: int) -> float | None:
        """Wait (bounded) until arriving frames are current, not a replay of old video.

        A still screen keeps its last live frame: age is measured at arrival, and two quiet seconds
        end the wait. Returns the monotonic board-clock offset.
        """
        await self._board_clock()
        clock = self._mono_clock
        if clock is None:
            return None
        deadline, logged = time.monotonic() + CATCH_UP_WAIT, False
        while time.monotonic() < deadline:
            self._check_operation()
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            snapshot = self._snapshot()
            age = self._frame_age(clock, snapshot)
            if age is None or age < REPLAY_LAG or (snapshot is not None and time.monotonic() - snapshot.arrived > 2):
                if logged:
                    log.info("screen: replay finished")
                return clock
            if not logged:
                log.info("screen: board is replaying old video (frame %.0f s behind on arrival), waiting", age)
                logged = True
            await asyncio.sleep(0.2)
        raise DeviceError("画面还在重放旧视频（重连后板子从上一个关键帧重放），请稍后再试。")

    async def _wait_screen(self, epoch: int, awaitable) -> None:
        task = asyncio.create_task(awaitable)
        try:
            while not task.done():
                if self._stopped(epoch):
                    break
                await asyncio.sleep(0.02)
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    def finish_calibration(self, x: float, y: float) -> None:
        """x, y: where the pointer ended up after start_calibration() (one point, picked by the user).

        One point cannot give both scale and offset, so this uses what is known about `home`: it runs up
        the left edge, so the pointer starts at the very top (y = 0) but, on a rounded corner, some way right
        of the left edge. The pointer moves the same number of pixels per unit across and down, so the
        vertical scale gives the horizontal one, and the rest of x is the corner offset.
        """
        span = {"w": round(CALIBRATION_MOVE / x), "h": round(CALIBRATION_MOVE / y)}
        origin = {"x": 0.0, "y": 0.0}
        aspect = self.screen.aspect if self.screen is not None and getattr(self.screen, "frame", None) else None
        if aspect:
            w = round(CALIBRATION_MOVE * aspect / y)
            x0 = x - CALIBRATION_MOVE / w
            if 0 <= x0 < 0.25:  # otherwise the point is odd; fall back to the plain one point scale
                span["w"], origin["x"] = w, x0
        self.span, self.origin = span, origin
        for samples in self._seen.values():
            samples.clear()
        self._pos = (CALIBRATION_MOVE, CALIBRATION_MOVE)
        self._moves, self._link = 0, getattr(self.device, "links", 0)
        self._save()

    def adopt_pace(self, version: int | None) -> None:
        """Drop the span when the board's pace number changes. USB files with no number stay valid."""
        if type(version) is not int:
            return
        if self._pace == version:
            return
        if self._pace is None and version == USB_PACE:
            self._pace = version
            if self.span is not None:
                self._save()
            return
        log.info("calibration cleared: pace %s -> %s", self._pace, version)
        self._pace = version
        self.span = None
        self.origin = {"x": 0.0, "y": 0.0}
        self._pos = None
        self._moves = 0
        for samples in self._seen.values():
            samples.clear()
        self._save()

    def _save(self) -> None:
        """Calibration plus the observed landings, so the correction survives a restart."""
        body = {"x0": self.origin["x"], "y0": self.origin["y"],
                "seen": {k: list(v) for k, v in self._seen.items()}}
        if self.span:
            body.update(self.span)
        if self._pace is not None:
            body["pace"] = self._pace
        self._file.write_text(json.dumps(body))

    @_serialized
    async def point(self, x: float, y: float, aim: bool = False) -> None:
        self.adopt_pace(getattr(self.device, "pace_version", None))
        if not self.calibrated:
            raise RuntimeError("pointer not calibrated")
        self._position_before = getattr(self.screen, "frame", None)
        self._position_from = self._pointer_position()
        self._position_started = time.time()
        await self._move_to(self._dx(x), self._dy(y))
        self._position_completed = time.time()
        if aim and self.screen is not None:
            await self._aim(x, y)

    @_serialized
    async def point_verified(self, x: float, y: float) -> tuple[float, float]:
        """Use calibrated landings, correcting at most once from a plausible visible disc."""
        freshness.require_fresh(self.screen)
        epoch = getattr(self.device, "abort_epoch", 0)
        self._pointer_seen = self._pointer_confirmed = False
        await self.point(x, y)
        self._pointer_spots = ((x, y),)
        found = await self._find_pointer((x, y), epoch=epoch, quick=True)
        if found is None or not all(abs(f - t) <= MAX_MISS for f, t in zip(found, (x, y), strict=True)):
            return x, y
        self._pointer_seen = True
        self._pointer_spots += (found,)
        fx, fy = found
        self._seen["x"].append((self._fix(x, "x"), fx))
        self._seen["y"].append((self._fix(y, "y"), fy))
        self._save()
        if abs(fx - x) <= AIM_TOLERANCE and abs(fy - y) <= AIM_TOLERANCE:
            self._pointer_confirmed = True
            return found
        dx, dy = round((x - fx) * self.span["w"]), round((y - fy) * self.span["h"])
        position, self._pos = self._pos, None
        self._position_before, self._position_from = self.screen.frame, found
        await self._send(epoch, f"move {dx} {dy}")
        self._pos = (position[0] + dx, position[1] + dy)
        self._position_completed = time.time()
        if not self._tap_blind_allowed:
            corrected = await self._observe_landing((x, y), epoch)
            self._pointer_confirmed = bool(corrected and abs(corrected[0] - x) <= AIM_TOLERANCE
                                           and abs(corrected[1] - y) <= AIM_TOLERANCE)
            if self._pointer_confirmed:
                return corrected
        return x, y

    def _pointer_position(self) -> tuple[float, float] | None:
        if self._pos is None or self.span is None:
            return None
        return (self.origin["x"] + self._pos[0] / self.span["w"],
                self.origin["y"] + self._pos[1] / self.span["h"])

    def _landing_error(self, x: float, y: float) -> tuple[float, float] | None:
        """Conservative measured residual envelope; a two-point fitted calibration is not validation."""
        errors = []
        for axis, target in (("x", x), ("y", y)):
            samples = self._seen[axis]
            if len(samples) < 3 or not all(math.isfinite(v) for pair in samples for v in pair):
                return None
            commands = [command for command, _ in samples]
            if max(commands) - min(commands) < .02 or not min(commands) <= self._fix(target, axis) <= max(commands):
                return None  # never extrapolate a blind landing beyond measured coverage
            error = max(abs(self._fix(landed, axis) - command) for command, landed in samples)
            errors.append(error + AIM_TOLERANCE)
        return tuple(errors)

    def _guard_blind_landing(self, x, y, target_box, target_frame):
        if target_frame is None or target_box is None:
            raise RuntimeError("无法确认指针落点：需要当前观察中的已知目标及其大小，未发送点击。")
        if len(target_box) != 4 or not all(math.isfinite(value) for value in target_box):
            raise RuntimeError("目标大小无效，未发送点击。")
        left, top, right, bottom = target_box
        if not (0 <= left < x < right <= 1 and 0 <= top < y < bottom <= 1):
            raise RuntimeError("点击坐标不在已观察目标内，未发送点击。")
        error = self._landing_error(x, y)
        if error is None or error[0] * 2 > min(x - left, right - x) or error[1] * 2 > min(y - top, bottom - y):
            raise RuntimeError("校准与已测落点不足，或落点误差相对目标过大，未发送点击。")

    @_serialized
    async def tap_verified(self, x: float, y: float, *,
                           before_click: Callable[[], None] | None = None,
                           target_box: tuple[float, float, float, float] | None = None,
                           target_frame: bytes | None = None) -> dict:
        """Click once only with a confirmed pointer or a bounded, observed target landing."""
        began = time.monotonic()
        self._tap_timing = {"positioning": 0.0, "firmware": 0.0, "wait_for_frames": 0.0, "diff": 0.0}
        timing = self._tap_timing
        epoch = getattr(self.device, "abort_epoch", 0)
        previous = self._pointer_spots + tuple(p for p in (self._pointer_position(),) if p is not None)
        hover = self._pointer_spots[:1] + ((x, y),)
        self._pointer_seen = self._pointer_confirmed = False
        self.last_tap = None
        try:
            observed = await self.ensure_observation()
            target = SimpleNamespace(center=(x, y))
            if target_box is not None:
                target.box = target_box
            target_guard = TextTargetGuard(self.screen, target_frame or observed.frame, target)
            try:
                self._guard_blind_landing(x, y, target_box, target_frame)
                self._tap_blind_allowed = True
            except RuntimeError:
                self._tap_blind_allowed = False
            landed = await self.point_verified(x, y)
            freshness.require_fresh(self.screen)
            if self._pointer_confirmed and target_box is not None:
                self._pointer_confirmed = bool(landed and len(target_box) == 4
                    and target_box[0] < landed[0] < target_box[2]
                    and target_box[1] < landed[1] < target_box[3])
            if not self._pointer_confirmed:
                self._guard_blind_landing(x, y, target_box, target_frame)
            before = None

            def validate_click():
                nonlocal before
                current = freshness.require_fresh(self.screen)
                if current.generation != observed.generation:
                    raise RuntimeError("点击前画面连接已变化，未发送点击；请重新观察。")
                target_guard()
                if before_click is not None:
                    before_click()
                before = freshness.require_fresh(self.screen).frame

            timing["positioning"] = (time.monotonic() - began) * 1000 - timing["firmware"]
            await self._send(epoch, "click", before_write=validate_click)
            sent = time.time()
            ignore = previous + self._pointer_spots + ((x, y),)
            changed, fresh = await self._tap_effect(before, sent, epoch, ignore, hover)
            timing["total"] = (time.monotonic() - began) * 1000
            result = {"clicked": True, "pointer_seen": self._pointer_seen, "screen_changed": changed,
                      "effect_observed": changed if fresh else None, "effect_verified": False,
                      "fresh": fresh, "completed_at": sent, "latency": round(timing["total"] / 1000, 3),
                      "timing_ms": {key: round(value, 2) for key, value in timing.items()},
                      "_after_frame": self._effect_frame, "_after_snapshot": self._effect_snapshot,
                      "warning": ("已点击并观察到画面变化，但尚未验证预期效果。" if changed else
                                  "已点击，但等待窗口内未见有效画面变化；请观察后再操作，勿自动重试。")}
            self.last_tap = result
            return result
        finally:
            self._tap_timing = None
            self._tap_blind_allowed = False

    async def _tap_effect(self, before, since: float, epoch: int, ignore: tuple, hover: tuple) -> tuple[bool, bool]:
        self._effect_frame = self._effect_snapshot = None
        if before is None:
            return False, False
        deadline = time.monotonic() + TAP_EFFECT_WAIT + freshness.MIRROR_LAG_SECONDS
        cursor = since
        if self._clock is None:
            cursor += freshness.MIRROR_LAG_SECONDS
        fresh = False
        while time.monotonic() < deadline:
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            wait_start = time.monotonic()
            after = await self.screen.fresh(cursor, min(.1, max(0, deadline - time.monotonic())))
            if self._tap_timing is not None:
                self._tap_timing["wait_for_frames"] += (time.monotonic() - wait_start) * 1000
            if after is not None and self.screen.updated > cursor:
                cursor = self.screen.updated
                eligible = freshness.fresh_frame(self.screen, since=since)
                if eligible is None:
                    # Only board receipt before the click proves a pre-click
                    # baseline. A frame inside the mirror uncertainty window
                    # might already show the effect; do not erase that evidence.
                    current = freshness.snapshot(self.screen)
                    if (self._clock is not None and current.frame_ts is not None
                            and freshness.board_delta(current.frame_ts, since * 1000 + self._clock)
                            < -getattr(self, "_clock_uncertainty", 0) * 1000):
                        before = current.frame
                    continue
                fresh, self._effect_frame = True, eligible.frame
                self._effect_snapshot = eligible
                diff_start = time.monotonic()
                changed = screen_compare.effect_changed(before, eligible.frame, ignore=ignore, hover=hover)
                if self._tap_timing is not None:
                    self._tap_timing["diff"] += (time.monotonic() - diff_start) * 1000
                if changed:
                    return True, True
            wait_start = time.monotonic()
            await asyncio.sleep(.005)
            if self._tap_timing is not None:
                self._tap_timing["wait_for_frames"] += (time.monotonic() - wait_start) * 1000
        return False, fresh

    async def _aim(self, x: float, y: float) -> None:
        """Closed loop: find the real pointer on screen, fix a miss, and learn the error for later moves."""
        found = await self._find_pointer((x, y))
        if not found:
            log.debug("aim (%.3f, %.3f): pointer not found", x, y)
            return
        fx, fy = found
        log.debug("aim (%.3f, %.3f): pointer at (%.3f, %.3f)", x, y, fx, fy)
        if not all(abs(f - t) <= MAX_MISS for f, t in zip(found, (x, y), strict=True)):
            return
        if max(abs(fx - x), abs(fy - y)) <= MAX_MISS:  # learn from normal drift only, never from a stray reading
            self._seen["x"].append((self._fix(x, "x"), fx))
            self._seen["y"].append((self._fix(y, "y"), fy))
            self._save()
        if abs(fx - x) <= AIM_TOLERANCE and abs(fy - y) <= AIM_TOLERANCE:
            return
        dx, dy = round((x - fx) * self.span["w"]), round((y - fy) * self.span["h"])  # fix this miss right away
        await self.device.send(f"move {dx} {dy}")
        self._pos = (self._pos[0] + dx, self._pos[1] + dy)

    def _fix(self, t: float, axis: str) -> float:
        """The fraction to aim at so the pointer lands on t, from the line through the observed landings."""
        pts = self._seen[axis]
        if len(pts) < 2:
            return t
        n = len(pts)
        mc, mf = sum(c for c, _ in pts) / n, sum(f for _, f in pts) / n
        var = sum((c - mc) ** 2 for c, _ in pts)
        a = sum((c - mc) * (f - mf) for c, f in pts) / var if var > 1e-4 else 1.0  # landed = a * aimed + b
        a = a if 0.85 < a < 1.15 else 1.0
        b = mf - a * mc
        return (t - b) / a if abs(b) < 0.1 else t

    async def _move_to(self, tx: int, ty: int, rehome: bool = True) -> None:
        if self._link != self.device.links:  # new link: a dropped gesture may have left the button down
            await self._send(getattr(self.device, "abort_epoch", 0), "release")
            self._pos, self._link = None, self.device.links
        if self._pos is None or (rehome and self._moves >= REHOME_EVERY):
            await self._send(getattr(self.device, "abort_epoch", 0), "home")
            self._pos, self._moves = (0, 0), 0
        x, y = self._pos
        self._pos = None  # unknown until every move succeeds
        epoch = getattr(self.device, "abort_epoch", 0)
        if (tx < 0 or ty < 0) and (x, y) == (0, 0):
            # Leave the rounded home corner first; negative offsets are reachable from the interior.
            wx, wy = max(tx, round(.15 * self.span["w"])), max(ty, round(.15 * self.span["h"]))
            await self._send(epoch, f"move {wx} {wy}")
            await self._send(epoch, f"move {tx - wx} 0")
            await self._send(epoch, f"move 0 {ty - wy}")
        elif (tx, ty) != (x, y):
            await self._send(epoch, f"move {tx - x} {ty - y}")
        self._pos, self._moves = (tx, ty), self._moves + 1

    async def reset(self) -> None:
        """Let go of the button and re-home the pointer, whatever state a gesture left."""
        self._pos = None
        await self.device.send("release")
        await self.device.send("home")
        self._pos, self._moves = (0, 0), 0

    async def tap(self, x: float, y: float, aim: bool = False, *,
                  target_box: tuple[float, float, float, float] | None = None,
                  target_frame: bytes | None = None,
                  before_click: Callable[[], None] | None = None) -> float:
        """All public taps share the guarded path; retain the legacy ACK timestamp return."""
        result = await self.tap_verified(x, y, target_box=target_box, target_frame=target_frame,
                                         before_click=before_click)
        return result["completed_at"]

    @_serialized
    async def swipe(self, x1: float, y1: float, x2: float, y2: float, hold: bool = True, *,
                    before_action: Callable[[], None] | None = None) -> None:
        await self.drag([(x1, y1), (x2, y2)], hold, before_action=before_action)

    def _gesture_guard(self, point, before_action):
        if before_action is not None:
            before_action()
        observed = freshness.require_fresh(self.screen)
        guard = TextTargetGuard(self.screen, observed.frame, SimpleNamespace(center=point), observation=observed)

        def validate():
            guard()
            if before_action is not None:
                before_action()

        return validate

    @_serialized
    async def drag(self, points: list[tuple[float, float]], hold: bool = True, *,
                   before_action: Callable[[], None] | None = None) -> None:
        """Press at the first point, move through the rest, release: one continuous stroke.

        hold=True rests at the end first, so lists and pickers stop exactly there instead of flinging on.
        System gestures (home, back) need the flick, so they pass hold=False.
        """
        epoch = getattr(self.device, "abort_epoch", 0)
        validate = self._gesture_guard(points[0], before_action)
        await self.point(*points[0])
        try:
            await self._send(epoch, "press", before_write=validate)
            for x, y in points[1:]:
                await self._move_to(self._dx(x), self._dy(y), rehome=False)  # never home mid-drag
            if hold:
                await asyncio.sleep(HOLD)
        finally:
            await self.device.send("release")

    @_serialized
    async def scroll(self, x: float, y: float, lines: int, *,
                     before_action: Callable[[], None] | None = None) -> None:
        epoch = getattr(self.device, "abort_epoch", 0)
        validate = self._gesture_guard((x, y), before_action)
        await self.point(x, y)
        await self._send(epoch, f"scroll {lines} 0", before_write=validate)

    @_serialized
    async def type(self, text: str, mode: TypeMode = "auto", *,
                   before_action: Callable[[], None] | None = None) -> None:
        """优先确认剪贴板后粘贴；未配置时仅 ASCII 可退回逐键输入。"""
        try:
            selected = select_type_mode(text, mode, clipboard_available=self.clipboard is not None)
        except ValueError as exc:
            raise DeviceError(str(exc)) from exc
        epoch = getattr(self.device, "abort_epoch", 0)
        async with self._typing:
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            if before_action is not None:
                before_action()  # serialization may have waited while the observed context changed
            first_write = before_action

            async def send_input(command):
                nonlocal first_write
                paste_command = selected == "paste" and command == "key cmd+v"
                validate_paste = getattr(self.clipboard, "validate_paste", None) if paste_command else None
                if paste_command and first_write is not None:
                    # In manual mode Cmd+V is the first write. Copy/focus can be
                    # confirmed before the mirror shows the original context again.
                    deadline = time.monotonic() + PASTE_CONTEXT_WAIT
                    while True:
                        if self._stopped(epoch):
                            raise DeviceError("已紧急停止，后面的内容没有发出。")
                        if validate_paste is not None:
                            validate_paste()
                        try:
                            first_write()
                            break
                        except (RuntimeError, ValueError) as exc:
                            if time.monotonic() >= deadline:
                                raise DeviceError("等待原输入画面恢复超时，没有粘贴；"
                                                  "请重新观察并核对快捷指令。") from exc
                        await asyncio.sleep(.05)

                def validate_input():
                    if first_write is not None:
                        first_write()
                    if self._stopped(epoch):
                        raise DeviceError("已紧急停止，后面的内容没有发出。")
                    # Context validation can itself span the deadline while the
                    # event loop cannot run its expiry/cancellation callbacks.
                    validate_paste()

                await self._send(epoch, command,
                                 before_write=validate_input if validate_paste is not None else first_write)
                first_write = None  # later chunks/Shortcut stages intentionally alter the screen

            if selected == "paste":
                await self.clipboard.paste(text, stopped=lambda: self._stopped(epoch),
                                           send=send_input)
            else:
                for part in type_chunks(text):
                    await send_input(f"type {part}")

    @_serialized
    async def key(self, combo: str, *, before_action: Callable[[], None] | None = None) -> None:
        if before_action is not None:
            before_action()
        await self._send(getattr(self.device, "abort_epoch", 0), f"key {combo}", before_write=before_action)

    @_serialized
    async def go_home(self) -> None:
        await self.assistive_touch.go_home()

    @_serialized
    async def go_back(self, *, before_action: Callable[[], None] | None = None) -> None:
        await self.swipe(0.005, 0.45, 0.6, 0.45, hold=False, before_action=before_action)

    def _dx(self, x: float) -> int:
        return round((self._fix(x, "x") - self.origin["x"]) * self.span["w"])

    def _dy(self, y: float) -> int:
        return round((self._fix(y, "y") - self.origin["y"]) * self.span["h"])
