"""Touch-like gestures on the phone, built from relative mouse moves.

The pointer is relative, so the server tracks where it is (in mouse units) and moves
from there; every few gestures it slams it into the top-left corner (`home`) again to
cancel drift. Coordinates are fractions of the screen (0..1, origin top-left), so they
work for any frame size.
"""

import asyncio
import inspect
import io
import json
import logging
import math
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageChops, ImageStat

from . import assistive_vision, feedback, freshness, ocr, screen_compare, vision
from .assistive import AssistiveTouch
from .device import RELEASE_TIMEOUT, Device, DeviceError, exchange_timing, input_attempt
from .input_context import InputContextGuard
from .operation import OPERATION_TIMEOUT, OperationDeadline, bounded_operation, check_operation, shield_cleanup
from .pointer_diagnostics import PointerDiagnostics
from .screen import FrameSnapshot
from .text_input import ClipboardWriter, TypeMode, clipboard_mode_options, select_type_mode
from .text_target import PointerLandingError, TargetChangedError, TextTargetGuard

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
BLE_CALIBRATION_MOVE = 90
BLE_CALIBRATION_STOPS = (60, 120)  # larger BLE reports can hit the edge before the USB probe distances
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
BLE_PACE = 4            # firmware HID_PACE_BLE
AIM_TOLERANCE = 0.008   # screen fraction; a bigger miss is corrected before pressing
MAX_MISS = 0.06         # larger misses may be misreadings; never correct or learn from them
BLE_PAIRED_MAX_MISS = 0.12  # a paired movement can authorize a correction, never an unseen click
TAP_POINTER_WAIT = 0.12  # opportunistic disc check; a late/hidden pointer uses the calibrated landing
TAP_EFFECT_WAIT = 1.75  # one mirror-lag window after the click; no automatic retry
WAKE_NUDGE = 2
WAKE_OUTWARD_HOLD = .05  # separate the two reports; only the completed return authorizes a new frame
POINTER_FADE_SECONDS = 3.0
OBSERVATION_REFRESH_WAIT = 3.5
OBSERVATION_ENTRY_MAX_AGE = 1.0  # leave motion/verification time inside the unchanged 5-second write limit
PASTE_CONTEXT_WAIT = 3.5  # bounded allowance for a dismissed Shortcut alert to leave the mirror
APP_SEARCH_MAX = 64
SPOTLIGHT_FOCUS_WAIT = 2.0  # one passive alternate frame; never repeat the search shortcut
SPOTLIGHT_PLACEHOLDERS = {"search", "搜索", "搜尋"}
SPOTLIGHT_MARKERS = {"sirisuggestions", "siri建议", "siri建議"}
HOST_OBSERVATION_TTL = 30.0
_HOST_OBSERVATION_ISSUER = object()


def _host_observed_pixels_match(before: bytes, current: bytes, pointer_xy) -> bool:
    """Host-only stationary evidence: unchanged page plus tiny native RGB noise.

    No caller-supplied mask, target-box exemption, resizing, pointer estimation
    or CV confirmation is used. In particular, a faded/moved pointer must still
    pass the stricter native-pixel neighborhood even if page comparison ignores it.
    """
    try:
        with Image.open(io.BytesIO(before)) as image:
            original = image.convert("RGB")
        with Image.open(io.BytesIO(current)) as image:
            latest = image.convert("RGB")
    except (OSError, TypeError, ValueError):
        return False
    if original.size != latest.size:
        return False
    if screen_compare.observation_changed(before, current):
        return False
    width, height = original.size
    x, y = round(pointer_xy[0] * width), round(pointer_xy[1] * height)
    radius = max(32, math.ceil(.064 * width))
    region = (max(0, x - radius), max(0, y - radius), min(width, x + radius + 1), min(height, y + radius + 1))
    difference = ImageChops.difference(original.crop(region), latest.crop(region))
    return (max(high for _, high in difference.getextrema()) <= 5
            and sum(ImageStat.Stat(difference).mean) / 3 <= .15)


@dataclass(frozen=True, init=False)
class HostObservation:
    """Internal proof that original pixels were fresh and causal when issued."""

    snapshot: freshness.FrameSnapshot = field(repr=False)
    device: object = field(repr=False)
    links: int
    abort_epoch: int
    input_sequence: int
    last_input_at: float
    session_started_at: float
    issued_monotonic: float
    expires_monotonic: float
    lease: object = field(repr=False)
    _phone: object = field(repr=False)
    _seal: object = field(repr=False)

    def __init__(self, *args, **kwargs):
        raise TypeError("host observation must come from passive, verified source observation")

    def validate(self, phone):
        check_operation()
        device = phone.device
        if (getattr(self, "_seal", None) is not _HOST_OBSERVATION_ISSUER or self._phone is not phone
                or not 0 <= time.monotonic() - self.issued_monotonic < HOST_OBSERVATION_TTL
                or time.monotonic() >= self.expires_monotonic or not self.lease.active):
            raise RuntimeError("宿主观察资格无效或已过期，请重新观察。")
        if (device is not self.device or device.links != self.links
                or device.abort_epoch != self.abort_epoch or device.input_sequence != self.input_sequence
                or device.last_input_at != self.last_input_at or device.session_started_at != self.session_started_at
                or freshness.snapshot(phone.screen).generation != self.snapshot.generation):
            raise RuntimeError("宿主观察后的设备、画面或输入会话已变化，未继续操作。")


class AppSearchError(RuntimeError):
    """App search could not proceed with verified evidence."""


class TypingVerificationError(DeviceError):
    """A type command was ACKed, but its field could not be verified afterward."""

    def __init__(self, message, *, completed_at, chunks_acknowledged, chunks_total, characters_acknowledged):
        super().__init__(message)
        self.completed_at = completed_at
        self.outcome = {
            "requires_verification": True, "input_verified": False,
            "typing_progress": {"chunks_acknowledged": chunks_acknowledged, "chunks_total": chunks_total,
                                "characters_acknowledged": characters_acknowledged,
                                "all_chunks_acknowledged": chunks_acknowledged == chunks_total}}


def validate_app_query(name: str) -> None:
    """Validate search input before a batch can send its first command."""
    if not 1 <= len(name) <= APP_SEARCH_MAX or name != name.strip() or not name.isascii() or not name.isprintable():
        raise ValueError("App 搜索目前只接受 1–64 个可打印 ASCII 字符，请使用英文 App 名。")


def _accepts_stage(callback) -> bool:
    return _accepts_keywords(callback, "stage")


def _accepts_keywords(callback, *names) -> bool:
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return False
    return all(name in parameters for name in names) or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


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
        self._pointer_pair_verified = False
        self._pointer_overlay = None
        self._pointer_reference = None
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
    @bounded_operation
    async def start_calibration(self) -> bool:
        """Find the pointer on screen at two known distances from the corner and fit the mapping from both.

        Two points give the scale and the offset, which one point cannot (iOS pointer motion is not exactly
        proportional). Returns True when it calibrated itself; otherwise the user clicks the pointer
        (finish_calibration).
        """
        self._pos = None
        epoch = getattr(self.device, "abort_epoch", 0)
        await self._pull_pace()
        stops = BLE_CALIBRATION_STOPS if self._pace == BLE_PACE else CALIBRATION_STOPS
        await self._send(epoch, "home")
        seen = []
        for units in stops:
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
            move = BLE_CALIBRATION_MOVE if self._pace == BLE_PACE else CALIBRATION_MOVE
            await self._send(epoch, "home")
            await self._send(epoch, f"move {move} {move}")
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
                            quick: bool = False, required: bool = False) -> tuple[float, float] | None:
        """Wiggle the pointer and find where it rests (the span may still be unknown).

        The mirror lags and stays silent while the screen is still, so the frame taken before the nudge may
        predate the last move. Every frame that arrives is kept and each new one is compared with the ones
        before it; the pointer is the round blob that jumped sideways by about the nudge. If the nudge never
        shows up, the way back is watched the same way. Both waits are bounded (POINTER_WAIT).
        """
        if epoch is None:
            epoch = getattr(self.device, "abort_epoch", 0)
        if quick:
            return await self._quick_pointer(near, epoch, required=required)
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
        generation = baseline.generation if baseline is not None else None
        strict = self._pace == BLE_PACE
        found = await self._watch(epoch, history, near, shift, sent, conservative, True, self._mono_clock,
                                  generation=generation, strict=strict)
        # An interrupted or expired search must not issue a cleanup movement.
        back = await self._send(epoch, f"move {-nudge} 0")
        if found is None and not self._stopped(epoch):
            # the nudge was not seen: the pointer rests where the way back leaves it
            found = await self._watch(epoch, history, near, -shift, back, conservative, False, self._mono_clock,
                                      generation=generation, strict=strict)
        return found if found and 0.03 < found[0] < 0.97 and 0.03 < found[1] < 0.97 else None

    async def _quick_pointer(self, near: tuple[float, float], epoch: int, *,
                             required: bool = False) -> tuple[float, float] | None:
        """Observe motion first; wake only an idle pointer when a search is necessary."""
        if self._clock_link != getattr(self.device, "links", 0):
            await self._board_clock()
        freshness.require_fresh(self.screen)
        reference = (self._position_before, None) if self._pace == BLE_PACE else None
        if time.monotonic() - self._last_motion < POINTER_FADE_SECONDS:
            found = await self._observe_landing(near, epoch)
            if found is not None or self._tap_blind_allowed or not required:
                return found
            return await self._localize_pointer(near, epoch, reference=reference)
        if self._tap_blind_allowed:
            return None  # an already bounded observed target does not need an idle-pointer search
        if required:
            return await self._localize_pointer(near, epoch, reference=reference)
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

    async def _localize_pointer(self, near, epoch, *, reference=None, probe=None, before_probe=None):
        """Required localization pairs a deliberate move and return, never a lone round UI element."""
        if before_probe is not None:
            before_probe()
        position, self._pos = self._pos, None
        ble = self._pace == BLE_PACE
        # Probe toward the blank canvas past a bottom corner control so the pointer's
        # outward position clears the icon and the screen edge.
        vertical = ble and near[1] >= .90 and (near[0] <= .16 or near[0] >= .86)
        nudge = -2 * NUDGE if vertical else (NUDGE if near[0] < .9 else -NUDGE)
        if probe is not None:
            vertical, nudge = probe[0] == 1, probe[1]
        dx, dy = (0, nudge) if vertical else (nudge, 0)
        shift = nudge / self.span["h" if vertical else "w"]
        sample_limit = 3 if ble else 1
        probe_reach = min(.09, abs(shift) * 2.2)
        reach = (BLE_PAIRED_MAX_MISS + .033 + (0 if vertical else probe_reach),
                 BLE_PAIRED_MAX_MISS + .025 + (probe_reach if vertical else 0)) if ble else (
                     MAX_MISS + .04, MAX_MISS + .03)
        observed = freshness.require_fresh(self.screen)
        link = getattr(self.device, "links", 0)

        def same_session():
            if before_probe is not None:
                before_probe()
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            if (freshness.require_fresh(self.screen).generation != observed.generation
                    or getattr(self.device, "links", 0) != link):
                raise RuntimeError("指针探测期间设备或画面连接已变化，未继续移动。")
            self._pointer_context_regions()

        baseline = observed.frame
        diagnostics = PointerDiagnostics.from_env()
        self._pointer_diagnostics = diagnostics
        if diagnostics:
            diagnostics.capture("baseline", baseline, observed.updated)
            target_reference = getattr(self, "_tap_target_frame", None)
            if target_reference is not None:
                diagnostics.capture("target_reference", target_reference)
            if reference is not None and reference[0] is not None:
                diagnostics.capture("position_reference", reference[0])
            diagnostics.event("probe", near=near, shift=shift, axis=int(vertical),
                              kind="escape_target" if probe is not None else "standard")

        async def capture_after(sent, select, *, on_arrival=None, phase="return"):
            deadline = time.monotonic() + feedback.MIRROR_LAG_SECONDS + .3
            cursor = sent
            while time.monotonic() < deadline:
                if self._stopped(epoch):
                    raise DeviceError("已紧急停止，后面的内容没有发出。")
                same_session()
                current = freshness.fresh_frame(self.screen)
                if current is not None and current.updated > cursor:
                    if diagnostics:
                        diagnostics.capture(phase, current.frame, current.updated)
                    if current.generation != observed.generation:
                        raise RuntimeError("指针定位期间画面连接已变化，请重新观察。")
                    if on_arrival is not None:
                        if (selected := on_arrival(current)) is not None:
                            return selected
                    if freshness.fresh_frame(self.screen, since=sent) == current:
                        if (selected := select(current.frame)) is not None:
                            return selected
                    cursor = current.updated
                await self.screen.fresh(cursor, min(.1, max(0, deadline - time.monotonic())))
                await asyncio.sleep(.005)
            return None

        outward = []
        early = deque(maxlen=4)

        def remember(frame):
            if frame not in outward:
                outward.append(frame)
            return frame if len(outward) >= sample_limit else None

        def remember_early(snapshot):
            if snapshot.frame_ts is None and (not early or snapshot.frame != early[-1]):
                early.append(snapshot.frame)

        def outward_visible():
            # This hypothetical return to the *observed* pre-probe frame only
            # decides whether a full outward candidate was exposed. It never
            # authorizes a landing; the real fresh return is still paired below.
            prior_frame, prior_pointer = reference or (None, None)
            if prior_frame is None:
                return False
            return any(screen_compare.cursor_overlay_pair(
                prior_frame, frame, baseline, near, -shift, reach,
                reference_pointer=prior_pointer, axis=int(vertical),
                caret_frames=getattr(self.screen, "recent_frames", ()),
                context_regions=self._pointer_context_regions()) is not None
                for frame in dict.fromkeys([*reversed(outward), *reversed(early)]))

        sent = await self._send(epoch, f"move {dx} {dy}", before_write=same_session)
        return_allowed = True
        try:
            outward_started = time.time() - (time.monotonic() - sent) if ble else time.time()
            await capture_after(outward_started, remember, on_arrival=remember_early if ble else None,
                                phase="outward")
            if ble and probe is not None and (outward or early) and not outward_visible():
                extended = self._extend_escape_probe(near, int(vertical), nudge)
                if extended is not None:
                    extra = extended - nudge
                    ex, ey = (0, extra) if vertical else (extra, 0)
                    # An unacknowledged extra move has an unknown physical
                    # outcome. Stop with position invalidated instead of sending
                    # a guessed inverse. All successful legs return cumulatively.
                    return_allowed = False
                    sent = await self._send(epoch, f"move {ex} {ey}", before_write=same_session)
                    nudge, return_allowed = extended, True
                    dx, dy = (0, nudge) if vertical else (nudge, 0)
                    shift = nudge / self.span["h" if vertical else "w"]
                    probe_reach = min(.09, abs(shift) * 2.2)
                    reach = (BLE_PAIRED_MAX_MISS + .033 + (0 if vertical else probe_reach),
                             BLE_PAIRED_MAX_MISS + .025 + (probe_reach if vertical else 0))
                    outward.clear()
                    early.clear()
                    if diagnostics:
                        diagnostics.event("probe_extension", shift=shift, axis=int(vertical))
                    outward_started = time.time() - (time.monotonic() - sent)
                    await capture_after(outward_started, remember, on_arrival=remember_early,
                                        phase="outward_extended")
        finally:
            if return_allowed and not self._stopped(epoch):
                sent = await self._send(epoch, f"move {-dx} {-dy}", before_write=same_session)
                if not ble:
                    self._pos = position
        if not (outward or early) or self._stopped(epoch):
            if diagnostics:
                diagnostics.event("result", pointer=None, reason="no_outward_or_stopped")
            return None

        def locate(returned):
            context_regions = self._pointer_context_regions()
            if not ble and screen_compare.observation_changed(
                baseline, returned,
                caret_frames=getattr(self.screen, "recent_frames", ()), context_regions=context_regions):
                return None
            for frame in dict.fromkeys([*reversed(outward), *reversed(early)]):
                pair = None if vertical else vision.pointer_move(frame, returned, near, -shift, reach, strict=True)
                prior_frame, prior_pointer = reference or (None, None)
                if ble and prior_frame is not None:
                    if pair is None:
                        evidence = screen_compare.cursor_overlay_pair(
                            prior_frame, frame, returned, near, -shift, reach,
                            reference_pointer=prior_pointer, report_reference=True, axis=int(vertical),
                            caret_frames=getattr(self.screen, "recent_frames", ()), context_regions=context_regions)
                        if evidence is not None:
                            pair, prior_pointer = evidence
                    else:
                        stable, detected = screen_compare.verify_cursor_reference(
                            prior_frame, frame, returned, pair, prior_pointer,
                            caret_frames=getattr(self.screen, "recent_frames", ()), context_regions=context_regions)
                        prior_pointer = detected if stable else None
                if pair is None:
                    if diagnostics:
                        diagnostics.event("pair", matched=False)
                    continue
                if diagnostics:
                    diagnostics.event("pair", matched=True, coordinates=pair)
                if ble:
                    if (any(abs(p - t) > BLE_PAIRED_MAX_MISS for p, t in zip(pair[1], near, strict=True))
                            or screen_compare.observation_changed(
                                frame, returned, moved_pointer=pair,
                                caret_frames=getattr(self.screen, "recent_frames", ()),
                                context_regions=context_regions)):
                        continue
                    self._pointer_pair_verified = True
                    self._pos = position
                    self._pointer_overlay = pair[1], returned
                    if self._pointer_reference is None and prior_pointer is not None:
                        self._pointer_reference = prior_pointer, prior_frame, returned
                return pair[1]
            return None

        if sample_limit == 1:
            returned = await capture_after(time.time(), lambda frame: frame)
            result = locate(returned) if returned is not None else None
            if diagnostics:
                diagnostics.event("result", pointer=result)
            return result
        return_started = time.time() - (time.monotonic() - sent)

        def find_early_return(snapshot):
            if snapshot.frame_ts is None:
                return locate(snapshot.frame)
            return None

        result = await capture_after(return_started, locate, on_arrival=find_early_return)
        if diagnostics:
            diagnostics.event("result", pointer=result)
        return result

    def _pointer_context_regions(self):
        """Only a scoped, detector-issued navigation context can narrow pointer checks."""
        context = getattr(self, "_navigation_context", None)
        if context is None:
            return None
        if freshness.require_fresh(self.screen).generation != context.observation.generation:
            raise RuntimeError("导航期间画面连接已变化，未继续移动。")
        return context.regions

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
        generation = freshness.require_fresh(self.screen).generation
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
            if snap.generation != generation:
                raise RuntimeError("指针定位期间画面连接已变化，请重新观察。")
            with Image.open(io.BytesIO(snap.frame)) as image:
                current = image.convert("RGB")
            if current.size != before.size or screen_compare.observation_changed(
                self._position_before, snap.frame,
                caret_frames=getattr(self.screen, "recent_frames", ()),
                context_regions=self._pointer_context_regions()):
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
                landing = (px + bounds[0]) / width, (py + bounds[1]) / height
                if self._pace == BLE_PACE:
                    self._pointer_overlay = landing, snap.frame
                return landing
        return None

    async def _watch(self, epoch: int, history: list, near, shift: float, since: float,
                     conservative: bool, at_start: bool, clock: float | None = None,
                     *, generation: int | None = None, strict: bool = False) -> tuple[float, float] | None:
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
                    # A pre-command frame can become a newer baseline, but it cannot prove the nudge,
                    # and it must not erase the frame that shows where the pointer rested.
                    if not history or after != history[-1]:
                        history.append(after)
                    await asyncio.sleep(.02)
                    continue
                priors = [item for item in dict.fromkeys(history) if item != after]
                if priors:
                    # the frame before the nudge, and the latest one (the before frame may predate the last move);
                    # each comparison is tens of ms of pure Python, and hogging the GIL starves the video thread
                    for before in dict.fromkeys(priors[:1] + priors[-1:]):
                        for look, reach in searches:
                            moved = vision.pointer_move(before, after, look, shift, reach, strict=strict)
                            if moved:
                                return moved[0] if at_start else moved[1]
                            await asyncio.sleep(0)
                if not history or after != history[-1]:
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
        frame = None if self.screen is None else getattr(self.screen, "frame", None)
        if isinstance(snap, FrameSnapshot) and frame is not None:
            board_ts = getattr(self.screen, "frame_ts", snap.board_ts)
            generation = getattr(self.screen, "generation", snap.generation)
            # A mirror can replace the image, its board stamp, or the session generation
            # without publishing a new arrival object. Keep the arrival when nothing changed.
            if snap.frame is frame and snap.board_ts == board_ts and snap.generation == generation:
                return snap
            updated = getattr(self.screen, "updated", snap.updated)
            arrived = time.monotonic() - max(0.0, time.time() - updated)
            return FrameSnapshot(frame, board_ts, arrived, updated, generation, snap.observed_live)
        if isinstance(snap, FrameSnapshot):
            return snap
        if frame is None:
            return None
        updated = getattr(self.screen, "updated", time.time())
        arrived = time.monotonic() - max(0.0, time.time() - updated)
        return FrameSnapshot(frame, getattr(self.screen, "frame_ts", None), arrived, updated,
                             getattr(self.screen, "generation", 0))

    async def _fresh_snapshot(self, since: float, timeout: float) -> FrameSnapshot | None:
        fresh = getattr(self.screen, "fresh_snapshot", None)
        if fresh is not None:
            return await fresh(since, timeout)
        wall_since = time.time() - (time.monotonic() - since)
        after = await self.screen.fresh(wall_since, timeout)
        return self._snapshot() if after else None

    @bounded_operation
    @_serialized
    async def observe_host_pointer(self, timeout: float = OBSERVATION_REFRESH_WAIT) -> HostObservation:
        """Wait for a causal source image without moving or waking the pointer."""
        if not math.isfinite(timeout) or not 0 < timeout <= OBSERVATION_REFRESH_WAIT:
            raise ValueError("passive observation timeout must be within 0..3.5 seconds")
        device = self.device
        original = device.links, device.abort_epoch, freshness.snapshot(self.screen).generation
        deadline = time.monotonic() + timeout
        lease = device.acquire_host_observation_lease()
        try:
            while device._idle_nudge_inflight:
                check_operation()
                if (self.device is not device
                        or original != (device.links, device.abort_epoch, freshness.snapshot(self.screen).generation)):
                    raise RuntimeError("被动观察期间设备或画面连接已变化，请重新观察。")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("后台保活移动尚未完成，未签发宿主观察。")
                await asyncio.sleep(min(.02, remaining))
            if (self.device is not device
                    or original != (device.links, device.abort_epoch, freshness.snapshot(self.screen).generation)):
                raise RuntimeError("被动观察期间设备或画面连接已变化，请重新观察。")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("被动观察已超时，未签发宿主观察。")
            return await self._observe_host_pointer(remaining, lease)
        except BaseException:
            lease.release()
            raise

    async def _observe_host_pointer(self, timeout, lease) -> HostObservation:
        deadline = time.monotonic() + timeout
        initial = freshness.snapshot(self.screen)
        device = self.device
        values = {"device": device, "links": getattr(device, "links", None),
                  "abort_epoch": getattr(device, "abort_epoch", None),
                  "input_sequence": getattr(device, "input_sequence", None),
                  "last_input_at": getattr(device, "last_input_at", None),
                  "session_started_at": getattr(device, "session_started_at", None)}
        if (any(type(values[key]) is not int or values[key] < 0
                for key in ("links", "abort_epoch", "input_sequence"))
                or not all(isinstance(values[key], (int, float)) and math.isfinite(values[key])
                           for key in ("last_input_at", "session_started_at"))):
            raise RuntimeError("宿主观察缺少设备输入会话，未发送任何操作。")

        def same_session():
            check_operation()
            if (self.device is not device or any(getattr(device, key, None) != values[key]
                                                for key in values if key != "device")
                    or self._stopped(values["abort_epoch"])
                    or freshness.snapshot(self.screen).generation != initial.generation):
                raise RuntimeError("被动观察期间设备、画面或输入会话已变化，请重新观察。")

        async with self._observation_lock:
            same_session()
            if self._clock_link != device.links or self._clock is None:
                await self._board_clock()
            same_session()
            boundary = max(values["last_input_at"], values["session_started_at"])
            while True:
                same_session()
                if time.monotonic() > deadline:
                    raise RuntimeError("被动等待后仍没有超过镜像延迟的新源画面，未发送任何操作。")
                current = freshness.snapshot(self.screen)
                if not current.live:
                    raise RuntimeError("屏幕镜像已断开，未获得宿主观察。")
                accepted = freshness.fresh_frame(self.screen, since=boundary)
                if accepted is not None and accepted.updated > boundary + freshness.MIRROR_LAG_SECONDS:
                    same_session()
                    qualified = object.__new__(HostObservation)
                    for key, value in {**values, "snapshot": accepted, "issued_monotonic": time.monotonic(),
                                       "expires_monotonic": lease.expires_monotonic, "lease": lease,
                                       "_phone": self, "_seal": _HOST_OBSERVATION_ISSUER}.items():
                        object.__setattr__(qualified, key, value)
                    return qualified
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("被动等待后仍没有超过镜像延迟的新源画面，未发送任何操作。")
                await self.screen.fresh(current.updated, min(.1, remaining))
                await asyncio.sleep(min(.005, max(0, deadline - time.monotonic())))

    @_serialized
    async def ensure_observation(self, *, before_refresh: Callable[[], None] | None = None):
        """Reacquire a silent live stream with one bounded, serialized pointer wake."""
        observed = freshness.snapshot(self.screen)
        device = self.device
        link, epoch = getattr(device, "links", 0), getattr(device, "abort_epoch", 0)

        def same_session():
            self._check_operation()
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            if (self.device is not device or getattr(device, "links", 0) != link
                    or freshness.snapshot(self.screen).generation != observed.generation):
                raise RuntimeError("唤醒期间设备或画面连接已变化，未继续移动。")

        def original_context():
            same_session()
            if before_refresh is not None:
                before_refresh()
                same_session()

        async with self._observation_lock:
            original_context()
            if self._clock_link != getattr(self.device, "links", 0) or self._clock is None:
                await self._board_clock()
            original_context()
            current = freshness.fresh_frame(self.screen, max_age=OBSERVATION_ENTRY_MAX_AGE)
            if current is not None:
                return current
            # A stale host receipt may be a healthy still screen. Replay or a
            # disconnected stream cannot use this exception to authorize motion.
            stale = freshness.require_fresh(self.screen, max_age=float("inf"))
            try:
                await asyncio.wait_for(self._refresh_observation(stale, epoch, before_write=original_context),
                                       OBSERVATION_REFRESH_WAIT)
                original_context()
                current = freshness.require_fresh(self.screen, max_age=OBSERVATION_ENTRY_MAX_AGE)
                return current
            except asyncio.TimeoutError as exc:
                raise RuntimeError("唤醒后仍没有足够新鲜的画面，未发送点击；请检查屏幕镜像。") from exc

    async def _refresh_observation(self, stale, epoch, *, before_write=None):
        device, link = self.device, getattr(self.device, "links", 0)

        def same_session():
            self._check_operation()
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            if (self.device is not device or getattr(device, "links", 0) != link
                    or freshness.snapshot(self.screen).generation != stale.generation):
                raise RuntimeError("唤醒期间设备或画面连接已变化，未继续移动。")
            # A wake exists precisely for a silent old receipt. Keep its replay
            # and live checks, but do not require the normal action-age budget.
            freshness.require_fresh(self.screen, max_age=float("inf"))

        def forward_context():
            same_session()
            if before_write is not None:
                before_write()
                same_session()

        self._pos = None  # clipping a small edge nudge can invalidate a known position
        # Unknown delivery never authorizes a guessed inverse. Only a successful
        # forward ACK reaches the one bounded return below.
        await self._send(epoch, f"move {WAKE_NUDGE} 0", before_write=forward_context)
        try:
            await asyncio.sleep(WAKE_OUTWARD_HOLD)
        finally:
            if not self._stopped(epoch):
                # The forward's own pointer pixels must not prevent this known
                # inverse. Session, stop and operation checks still run in the
                # actual write lock; original layout is checked after new video.
                await self._send(epoch, f"move {-WAKE_NUDGE} 0", before_write=same_session)
        returned_at = time.time()  # completion/ACK, not forward send or host receipt
        cursor = returned_at
        while True:
            same_session()
            accepted = freshness.fresh_frame(self.screen, since=returned_at)
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
        move = BLE_CALIBRATION_MOVE if self._pace == BLE_PACE else CALIBRATION_MOVE
        span = {"w": round(move / x), "h": round(move / y)}
        origin = {"x": 0.0, "y": 0.0}
        aspect = self.screen.aspect if self.screen is not None and getattr(self.screen, "frame", None) else None
        if aspect:
            w = round(move * aspect / y)
            x0 = x - move / w
            if 0 <= x0 < 0.25:  # otherwise the point is odd; fall back to the plain one point scale
                span["w"], origin["x"] = w, x0
        self.span, self.origin = span, origin
        for samples in self._seen.values():
            samples.clear()
        self._pos = (move, move)
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

    @bounded_operation
    @_serialized
    async def point_verified(self, x: float, y: float, *, target_box=None) -> tuple[float, float]:
        """Use measured landings and verify every correction before it can authorize a click."""
        observed = freshness.require_fresh(self.screen)
        epoch = getattr(self.device, "abort_epoch", 0)
        link = getattr(self.device, "links", 0)

        def same_session():
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            if (freshness.require_fresh(self.screen).generation != observed.generation
                    or getattr(self.device, "links", 0) != link):
                raise RuntimeError("指针定位期间设备或画面连接已变化，请重新观察。")

        self._pointer_seen = self._pointer_confirmed = False
        self._pointer_escape_attempted = False
        self._pointer_pair_verified = False
        self._pointer_overlay = None
        self._pointer_reference = None
        await self.point(x, y)
        same_session()
        self._pointer_spots = ((x, y),)
        found = await self._find_pointer((x, y), epoch=epoch, quick=True, required=True)
        same_session()
        # A circle overlapping a textured icon may expose only a crescent. Once,
        # leave the observed target by a full cursor radius, then return and use
        # the same strict paired-motion detector. This does not permit a click
        # without a measured landing or bypass the original target comparison.
        async def escape_target(reference):
            if self._pace != BLE_PACE or target_box is None or self._pointer_escape_attempted:
                return None
            probe = self._target_escape_probe((x, y), target_box)
            if probe is None:
                return None
            self._pointer_escape_attempted = True
            measured = await self._localize_pointer(
                (x, y), epoch, reference=reference, probe=probe, before_probe=same_session)
            same_session()
            return measured

        if found is None:
            found = await escape_target((self._position_before, None))
        miss = max(abs(found[0] - x), abs(found[1] - y)) if found is not None and all(
            math.isfinite(value) for value in found) else math.inf
        ble = self._pace == BLE_PACE
        paired_ble = ble and self._pointer_pair_verified
        if miss > (BLE_PAIRED_MAX_MISS if paired_ble else MAX_MISS):
            return x, y
        self._pointer_seen = True
        self._pointer_spots += (found,)
        if miss <= MAX_MISS:
            fx, fy = found
            self._seen["x"].append((self._fix(x, "x"), fx))
            self._seen["y"].append((self._fix(y, "y"), fy))
            self._save()

        def on_target(point):
            px, py = point
            if abs(px - x) <= AIM_TOLERANCE and abs(py - y) <= AIM_TOLERANCE:
                return True
            if not paired_ble or target_box is None:
                return False
            left, top, right, bottom = target_box
            # Each measured axis can independently satisfy the established
            # center tolerance or the target's safe interior. A narrow icon need
            # not force a second correction when the other axis is already safe.
            return ((abs(px - x) <= AIM_TOLERANCE or left + AIM_TOLERANCE < px < right - AIM_TOLERANCE)
                    and (abs(py - y) <= AIM_TOLERANCE or top + AIM_TOLERANCE < py < bottom - AIM_TOLERANCE))

        relative_corrected = False
        for attempt in range(3 if ble else 2):
            if found is None:
                break
            if on_target(found):
                self._pointer_confirmed = True
                return found
            if attempt >= (2 if paired_ble else 1):
                break
            correction_guard = None
            cursor, reference = self._pointer_overlay, self._pointer_reference
            if self._pos is None:
                # A failed probe invalidates the absolute accumulator, even if
                # a later paired probe recovers the real pointer. One relative
                # correction needs that measured pose, not an invented origin.
                if (relative_corrected or not paired_ble or cursor is None or cursor[0] != found
                        or target_box is None or len(target_box) != 4
                        or not all(math.isfinite(value) for value in target_box)
                        or not (0 <= target_box[0] < x < target_box[2] <= 1
                                and 0 <= target_box[1] < y < target_box[3] <= 1)):
                    break
                correction_guard = TextTargetGuard(
                    self.screen, self._tap_target_frame or observed.frame,
                    SimpleNamespace(center=(x, y), box=target_box), observation=observed,
                    navigation_context=self._navigation_context)
                relative_corrected = True

            def before_correction(guard=correction_guard, cursor=cursor, reference=reference):
                same_session()
                if guard is not None:
                    guard(cursor=cursor, reference=reference)

            fx, fy = found
            dx, dy = round((x - fx) * self.span["w"]), round((y - fy) * self.span["h"])
            if not (dx or dy):
                break
            position, self._pos = self._pos, None
            self._position_before, self._position_from = self.screen.frame, found
            self._pointer_overlay = None
            await self._send(epoch, f"move {dx} {dy}", before_write=before_correction)
            if position is not None:
                self._pos = (position[0] + dx, position[1] + dy)
            self._position_completed = time.time()
            if self._tap_blind_allowed and not ble:
                break
            self._tap_blind_allowed = False
            found = await self._observe_landing((x, y), epoch)
            same_session()
            if found is None and ble:
                reference = self._position_before, (fx, fy)
                found = await self._localize_pointer((x, y), epoch, reference=reference, before_probe=same_session)
                same_session()
                if found is None:
                    found = await escape_target(reference)
                paired_ble = self._pointer_pair_verified
            if found is not None:
                limit = BLE_PAIRED_MAX_MISS if paired_ble else MAX_MISS
                if not all(math.isfinite(actual) and abs(actual - requested) <= limit
                           for actual, requested in zip(found, (x, y), strict=True)):
                    break
                self._pointer_spots += (found,)
        return x, y

    def _target_escape_probe(self, near, box):
        """One screen-bounded probe, at most 18% of width, sized to a known target."""
        if (len(box) != 4 or not all(math.isfinite(value) for value in box)
                or not (0 <= box[0] < near[0] < box[2] <= 1 and 0 <= box[1] < near[1] < box[3] <= 1)):
            return None
        with Image.open(io.BytesIO(self.screen.frame)) as image:
            dimensions = image.size
        radius = .055 * dimensions[0]  # cursor radius plus a small clear-background margin
        candidates = []
        for axis in (0, 1):
            span, size = self.span["wh"[axis]], dimensions[axis]
            for side in (-1, 1):
                edge = box[axis if side < 0 else axis + 2]
                distance = (edge - near[axis]) * size + side * radius
                destination = near[axis] * size + distance
                if not radius <= destination <= size - radius or abs(distance) > .18 * dimensions[0]:
                    continue
                units = side * math.ceil(abs(distance) * span / size)
                actual = near[axis] * size + units * size / span
                if radius <= actual <= size - radius and abs(actual - near[axis] * size) <= .18 * dimensions[0]:
                    candidates.append((abs(distance), axis, units))
        if not candidates:
            return None
        shortest = min(item[0] for item in candidates)
        normal_vertical = near[1] >= .90 and (near[0] <= .16 or near[0] >= .86)
        alternate = 0 if normal_vertical else 1
        practical = [item for item in candidates if item[0] <= 1.35 * shortest]
        _, axis, units = min(practical, key=lambda item: (item[1] != alternate, item[0]))
        return self._home_menu_probe_direction(near, box, dimensions, practical, (axis, units))

    def _home_menu_probe_direction(self, near, box, dimensions, candidates, default):
        """Rank existing same-axis candidates, never infer a landing from clear pixels."""
        from .navigation_context import NavigationContext

        context = getattr(self, "_navigation_context", None)
        if (not isinstance(context, NavigationContext) or context.action != "home" or context.kind != "menu"
                or tuple(box) != context.target.box or len(context.regions) != 1):
            return default
        options = [item for item in candidates if item[1] == default[0]]
        if len(options) != 2 or options[0][2] * options[1][2] >= 0:
            return default
        try:
            context.validate_target(getattr(self, "_tap_target_frame", None),
                                    freshness.snapshot(self.screen).generation, near)
            with Image.open(io.BytesIO(context.observation.frame)) as source:
                image = source.convert("RGB")
            if image.size != dimensions:
                return default
            width, height = dimensions
            # Cover the existing maximum pointer diameter (.067 * width), with
            # no extra search area. Both footprints must lie inside the menu.
            radius = math.ceil(.034 * width)
            panel = tuple(value * dimensions[index % 2] for index, value in enumerate(context.regions[0]))
            scores = []
            for _, axis, units in options:
                point = [near[0] * width, near[1] * height]
                point[axis] += units * dimensions[axis] / self.span["wh"[axis]]
                x, y = map(round, point)
                bounds = (x-radius, y-radius, x+radius+1, y+radius+1)
                if not (0 <= panel[0] <= bounds[0] < bounds[2] <= panel[2] <= width
                        and 0 <= panel[1] <= bounds[1] < bounds[3] <= panel[3] <= height):
                    return default
                patch = image.crop(bounds)
                median = ImageStat.Stat(patch).median
                pixels = patch.load()
                textured = sum(max(abs(value-background)
                                   for value, background in zip(pixels[x, y], median, strict=True)) > 12
                               for y in range(patch.height) for x in range(patch.width)) / (patch.width * patch.height)
                scores.append((textured, axis, units))
            scores.sort()
            # Only a clearly flat/label-textured contrast changes the old order.
            # Similar backgrounds retain the original geometric tie-breaker.
            if scores[0][0] <= .02 and scores[1][0] >= .10:
                return scores[0][1], scores[0][2]
        except (OSError, ValueError, RuntimeError):
            pass
        return default

    def _extend_escape_probe(self, near, axis, units):
        """One extra cursor clearance, keeping the entire outward leg within its original bound."""
        with Image.open(io.BytesIO(self.screen.frame)) as image:
            dimensions = image.size
        size, span = dimensions[axis], self.span["wh"[axis]]
        radius = .055 * dimensions[0]  # same clearance as the initial target escape
        side = 1 if units > 0 else -1
        edge_room = size - radius - near[axis] * size if side > 0 else near[axis] * size - radius
        limit = math.floor(min(.18 * dimensions[0], edge_room) * span / size)
        extra = min(math.ceil(radius * span / size), limit - abs(units))
        return side * (abs(units) + extra) if extra > 0 else None

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
            raise PointerLandingError("无法确认指针落点：需要当前观察中的已知目标及其大小，未发送点击。")
        if len(target_box) != 4 or not all(math.isfinite(value) for value in target_box):
            raise PointerLandingError("目标大小无效，未发送点击。")
        left, top, right, bottom = target_box
        if not (0 <= left < x < right <= 1 and 0 <= top < y < bottom <= 1):
            raise PointerLandingError("点击坐标不在已观察目标内，未发送点击。")
        error = self._landing_error(x, y)
        if error is None or error[0] * 2 > min(x - left, right - x) or error[1] * 2 > min(y - top, bottom - y):
            raise PointerLandingError("校准与已测落点不足，或落点误差相对目标过大，未发送点击。")

    async def click_observed(self, *, observation: freshness.FrameSnapshot,
                             pointer_xy, target_box, expected_device, expected_links: int,
                             expected_abort_epoch: int, expected_input_seq: int,
                             before_click: Callable[[], None] | None = None,
                             host_observation: HostObservation | None = None) -> dict:
        try:
            return await self._click_observed(
                observation=observation, pointer_xy=pointer_xy, target_box=target_box,
                expected_device=expected_device, expected_links=expected_links,
                expected_abort_epoch=expected_abort_epoch, expected_input_seq=expected_input_seq,
                before_click=before_click, host_observation=host_observation)
        finally:
            if isinstance(host_observation, HostObservation):
                lease = getattr(host_observation, "lease", None)
                if lease is not None:
                    lease.release()

    @bounded_operation
    @_serialized
    async def _click_observed(self, *, observation: freshness.FrameSnapshot,
                              pointer_xy, target_box, expected_device, expected_links: int,
                              expected_abort_epoch: int, expected_input_seq: int,
                              before_click: Callable[[], None] | None = None,
                              host_observation: HostObservation | None = None) -> dict:
        """One explicit host-observed contact at the unmoved pointer; never a CV fallback."""
        if (not isinstance(observation, freshness.FrameSnapshot) or not observation.frame
                or len(pointer_xy) != 2 or len(target_box) != 4
                or not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                           and math.isfinite(value) and 0 <= value <= 1
                           for value in (*pointer_xy, *target_box))):
            raise ValueError("host-observed click requires an original frame and finite normalized coordinates")
        x, y = pointer_xy
        left, top, right, bottom = target_box
        if not (left < x < right and top < y < bottom):
            raise ValueError("host-observed pointer must lie strictly inside its observed target box")
        if any(type(value) is not int or value < 0
               for value in (expected_links, expected_abort_epoch, expected_input_seq)):
            raise ValueError("host-observed click requires the original device session and input sequence")
        if host_observation is not None:
            if not isinstance(host_observation, HostObservation):
                raise ValueError("host observation qualification must be internally issued")
            host_observation.validate(self)
            if (host_observation.snapshot != observation or host_observation.device is not expected_device
                    or host_observation.links != expected_links or host_observation.abort_epoch != expected_abort_epoch
                    or host_observation.input_sequence != expected_input_seq):
                raise ValueError("host observation qualification does not belong to this original evidence")
        began = time.monotonic()
        self._effect_frame = self._effect_snapshot = None

        def same_session(sequence):
            if (self.device is not expected_device or self._stopped(expected_abort_epoch)
                    or getattr(self.device, "links", None) != expected_links
                    or getattr(self.device, "input_sequence", None) != sequence
                    or freshness.snapshot(self.screen).generation != observation.generation):
                raise RuntimeError("宿主观察后的设备、画面或输入会话已变化，未继续操作。")

        def validate_observation():
            same_session(expected_input_seq)
            if host_observation is not None:
                host_observation.validate(self)
            current = freshness.require_fresh(self.screen)
            if not _host_observed_pixels_match(observation.frame, current.frame, pointer_xy):
                raise RuntimeError("宿主观察的原画面已变化或过期，未发送原地点击。")
            if host_observation is not None:
                # Admission proved the immutable original image was fresh and
                # causal. The host may spend up to the ticket TTL reading it;
                # only a genuinely fresh, unchanged source with bounded native
                # RGB compression noise at the observed pointer can act.
                return
            if (not observation.live or not math.isfinite(observation.updated)
                    or not -.1 <= time.time() - observation.updated <= freshness.MAX_FRAME_AGE):
                raise RuntimeError("宿主观察的原画面已变化或过期，未发送原地点击。")
            last_input = getattr(self.device, "last_input_at", None)
            connected_at = getattr(self.device, "session_started_at", None)
            if (not all(isinstance(value, (int, float)) and math.isfinite(value)
                        for value in (last_input, connected_at))):
                raise RuntimeError("宿主观察缺少输入或连接时点，未发送原地点击。")
            boundary = max(last_input, connected_at)
            original = SimpleNamespace(snapshot=observation, _freshness_phone=self)
            if (observation.updated <= boundary + freshness.MIRROR_LAG_SECONDS
                    or freshness.fresh_frame(original, since=boundary) is None):
                raise RuntimeError("宿主观察尚未超过上次输入的镜像延迟，请重新观察指针后再点击。")

        def validate_click():
            validate_observation()
            if before_click is not None:
                before_click()
                validate_observation()

        # No ensure_observation, nudge, calibrated estimate or pointer flag may
        # alter the host's evidence before this actual, single write boundary.
        if host_observation is not None:
            deadline = min(time.monotonic() + OBSERVATION_REFRESH_WAIT,
                           host_observation.expires_monotonic,
                           host_observation.issued_monotonic + HOST_OBSERVATION_TTL)
            while True:
                host_observation.validate(self)
                same_session(expected_input_seq)
                if time.monotonic() > deadline:
                    raise RuntimeError("被动等待后仍没有足够新鲜的同一画面，未发送点击。")
                if before_click is not None:
                    before_click()
                current = freshness.snapshot(self.screen)
                # Once a different page is observed, never wait for it to turn
                # back into A. We retain no history of unseen inference frames.
                if (not current.live
                        or not _host_observed_pixels_match(observation.frame, current.frame, pointer_xy)):
                    raise RuntimeError("等待点击期间原画面已变化或断开，请重新观察。")
                current_source = SimpleNamespace(snapshot=current, _freshness_phone=self)
                if freshness.fresh_frame(current_source) is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("被动等待后仍没有足够新鲜的同一画面，未发送点击。")
                await self.screen.fresh(current.updated, min(.1, remaining))
                await asyncio.sleep(min(.005, max(0, deadline - time.monotonic())))
        validate_click()
        await self._send(expected_abort_epoch, "click", before_write=validate_click)
        completed_at = time.time()
        same_session(expected_input_seq + 1)  # our own click advances the sequence exactly once
        changed, fresh = await self._tap_effect(observation.frame, completed_at, expected_abort_epoch, (), ())
        same_session(expected_input_seq + 1)
        return {"clicked": True, "click_count": 1, "evidence_source": "host_observed",
                "pointer_seen": False, "effect_verified": False, "screen_changed": changed if fresh else None,
                "effect_observed": changed if fresh else None, "fresh": fresh, "completed_at": completed_at,
                "latency": round(time.monotonic() - began, 3),
                "_after_frame": self._effect_frame, "_after_snapshot": self._effect_snapshot,
                "warning": "已按宿主看到的指针位置原地单击；请查看结果，勿自动重试。"}

    @bounded_operation
    @_serialized
    async def tap_verified(self, x: float, y: float, *,
                           before_click: Callable[[], None] | None = None,
                           target_box: tuple[float, float, float, float] | None = None,
                           target_frame: bytes | None = None,
                           allow_estimated_landing: bool = True,
                           duration: float = 0, click_count: int = 1, navigation_context=None) -> dict:
        """Perform a single observed contact gesture, then return its visual evidence."""
        if (click_count not in (1, 2) or isinstance(click_count, bool)
                or not math.isfinite(duration) or duration < 0 or duration > 3
                or (duration and (duration < .3 or click_count != 1))):
            raise ValueError("contact requires one/two clicks or a single 0.3..3 second hold")
        # Both contacts must reuse a measured landing. A calibration-only
        # estimate can race a delayed frame from the positioning move.
        allow_estimated_landing = allow_estimated_landing and click_count == 1
        began = time.monotonic()
        self._tap_timing = {"positioning": 0.0, "firmware": 0.0, "wait_for_frames": 0.0, "diff": 0.0}
        timing = self._tap_timing
        device = self.device
        epoch, link = getattr(device, "abort_epoch", 0), getattr(device, "links", 0)
        generation = freshness.snapshot(self.screen).generation

        def same_session():
            self._check_operation()
            if (self.device is not device or getattr(device, "links", 0) != link
                    or getattr(device, "abort_epoch", 0) != epoch):
                raise DeviceError("点击期间设备连接已变化或已停止，未继续操作；请重新观察。")
            if freshness.snapshot(self.screen).generation != generation:
                raise RuntimeError("点击期间画面连接已变化，未继续操作；请重新观察。")

        previous = self._pointer_spots + tuple(p for p in (self._pointer_position(),) if p is not None)
        hover = self._pointer_spots[:1] + ((x, y),)
        self._pointer_seen = self._pointer_confirmed = False
        self._pointer_escape_attempted = False
        self.last_tap = None
        self._pointer_diagnostics = None
        self._tap_target_frame = None
        self._navigation_context = None
        try:
            with input_attempt(before_write=same_session, inherit=True,
                               write_lock=getattr(self.screen, "_frame_lock", None)):
                same_session()
                observed = await self.ensure_observation()
                same_session()
                self._tap_target_frame = target_frame or observed.frame
                if navigation_context is not None:
                    from .navigation_context import NavigationContext

                    if not isinstance(navigation_context, NavigationContext) or duration or click_count != 1:
                        raise ValueError("navigation context only supports one observed system navigation click")
                    navigation_context.validate_target(target_frame or observed.frame, observed.generation, (x, y))
                    self._navigation_context = navigation_context
                    target_box = navigation_context.target.box
                    allow_estimated_landing = False
                target = SimpleNamespace(center=(x, y))
                if target_box is not None:
                    target.box = target_box
                target_guard = TextTargetGuard(self.screen, target_frame or observed.frame, target,
                                              navigation_context=navigation_context)
                self._tap_blind_allowed = False
                if allow_estimated_landing:
                    try:
                        self._guard_blind_landing(x, y, target_box, target_frame)
                    except PointerLandingError:
                        pass
                    else:
                        self._tap_blind_allowed = True
                if self._pace == BLE_PACE and target_box is not None:
                    landed = await self.point_verified(x, y, target_box=target_box)
                else:
                    landed = await self.point_verified(x, y)
                same_session()
                freshness.require_fresh(self.screen)
                if self._pointer_confirmed and target_box is not None:
                    self._pointer_confirmed = bool(landed and len(target_box) == 4
                        and target_box[0] < landed[0] < target_box[2]
                        and target_box[1] < landed[1] < target_box[3])
                if not self._pointer_confirmed:
                    if self._pointer_escape_attempted:
                        raise PointerLandingError("离开目标再返回后仍未确认指针落点，未发送点击。")
                    if self._pace == BLE_PACE and self._pointer_pair_verified:
                        raise PointerLandingError("蓝牙指针纠偏后未能确认落点在目标内，未发送点击。")
                    if not allow_estimated_landing:
                        raise PointerLandingError("没有观察到指针落在目标内，未发送点击。")
                    self._guard_blind_landing(x, y, target_box, target_frame)
                before = None

                def validate_click():
                    nonlocal before
                    # Legacy web taps do not carry an outer Controls/Agent input
                    # guard. Recheck their original session inside the serial lock.
                    same_session()
                    current = freshness.require_fresh(self.screen)
                    if current.generation != observed.generation:
                        raise RuntimeError("点击前画面连接已变化，未发送点击；请重新观察。")
                    cursor = self._pointer_overlay if self._pointer_confirmed else None
                    reference = self._pointer_reference if cursor else None
                    diagnostics = self._pointer_diagnostics
                    if diagnostics:
                        diagnostics.capture("target_reference", target_frame or observed.frame)
                        diagnostics.capture("write_candidate", current.frame, current.updated)
                    try:
                        target_guard(cursor=cursor, reference=reference)
                        if before_click is not None:
                            if isinstance(before_click, TextTargetGuard):
                                before_click(cursor=cursor, reference=reference)
                            else:
                                before_click()
                    except RuntimeError as exc:
                        if diagnostics:
                            diagnostics.event("write_validation", accepted=False, reason=type(exc).__name__)
                        raise
                    if diagnostics:
                        diagnostics.event("write_validation", accepted=True)
                    before = freshness.require_fresh(self.screen).frame

                timing["positioning"] = (time.monotonic() - began) * 1000 - timing["firmware"]
                if duration:
                    try:
                        await self._send(epoch, "press", before_write=validate_click)
                        await asyncio.sleep(duration)
                    finally:
                        await shield_cleanup(asyncio.wait_for(self._command("release"), RELEASE_TIMEOUT))
                else:
                    for index in range(click_count):
                        if index:
                            await asyncio.sleep(.08)
                        # Never send a second contact after an observed navigation or disconnect.
                        await self._send(epoch, "click", before_write=validate_click)
                same_session()
                sent = time.time()
                ignore = previous + self._pointer_spots + ((x, y),)
                changed, fresh = await self._tap_effect(before, sent, epoch, ignore, hover)
                same_session()
                timing["total"] = (time.monotonic() - began) * 1000
                result = {"clicked": True, "pointer_seen": self._pointer_seen, "screen_changed": changed,
                          "click_count": click_count if not duration else 0, "hold_seconds": duration,
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
            self._navigation_context = None
            self._tap_target_frame = None

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

    async def _move_to(self, tx: int, ty: int, rehome: bool = True, *, before_write=None) -> None:
        if before_write is not None:
            before_write()
        if not rehome and (self._pos is None or self._link != self.device.links):
            raise DeviceError("拖动期间指针位置或设备连接已变化，未继续移动。")
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
            await self._send(epoch, f"move {wx} {wy}", before_write=before_write)
            await self._send(epoch, f"move {tx - wx} 0", before_write=before_write)
            await self._send(epoch, f"move 0 {ty - wy}", before_write=before_write)
        elif (tx, ty) != (x, y):
            await self._send(epoch, f"move {tx - x} {ty - y}", before_write=before_write)
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
                  before_click: Callable[[], None] | None = None, navigation_context=None) -> float:
        """All public taps share the guarded path; retain the legacy ACK timestamp return."""
        options = {"navigation_context": navigation_context} if navigation_context is not None else {}
        result = await self.tap_verified(x, y, target_box=target_box, target_frame=target_frame,
                                         before_click=before_click, **options)
        return result["completed_at"]

    @_serialized
    async def swipe(self, x1: float, y1: float, x2: float, y2: float, hold: bool = True, *,
                    before_action: Callable[[], None] | None = None) -> None:
        await self.drag([(x1, y1), (x2, y2)], hold, before_action=before_action, verify_start=True)

    def _gesture_guard(self, point, before_action, *, verified_pointer=False):
        if before_action is not None:
            before_action()
        if self.screen is None:
            return before_action
        observed = freshness.require_fresh(self.screen)
        guard = TextTargetGuard(self.screen, observed.frame, SimpleNamespace(center=point), observation=observed)

        def validate(*, include_context=True):
            cursor = self._pointer_overlay if verified_pointer and self._pointer_confirmed else None
            reference = self._pointer_reference if cursor else None
            guard(cursor=cursor, reference=reference)
            if include_context and before_action is not None:
                if _accepts_keywords(before_action, "cursor", "reference"):
                    before_action(cursor=cursor, reference=reference)
                else:
                    before_action()

        return validate

    @_serialized
    @bounded_operation
    async def drag(self, points: list[tuple[float, float]], hold: bool = True, *,
                   before_action: Callable[[], None] | None = None, verify_start: bool = False) -> None:
        """Press at the first point, move through the rest, release: one continuous stroke.

        hold=True rests at the end first, so lists and pickers stop exactly there instead of flinging on.
        System gestures (home, back) need the flick, so they pass hold=False.
        """
        if not 2 <= len(points) <= 256 or any(
                not math.isfinite(v) or not 0 <= v <= 1 for point in points for v in point):
            raise ValueError("drag requires 2..256 finite normalized points")
        epoch = getattr(self.device, "abort_epoch", 0)
        link = getattr(self.device, "links", 0)
        generation = freshness.require_fresh(self.screen).generation if self.screen is not None else None

        def same_session():
            self._check_operation()
            if self._stopped(epoch) or getattr(self.device, "links", 0) != link:
                raise DeviceError("拖动期间设备连接已变化或已停止，未继续移动。")
            if self.screen is not None and freshness.require_fresh(self.screen).generation != generation:
                raise RuntimeError("拖动期间画面连接已变化，未继续移动。")

        validate = self._gesture_guard(points[0], before_action, verified_pointer=verify_start)
        if verify_start:
            await self.point_verified(*points[0])
            if not self._pointer_confirmed:
                raise PointerLandingError("未确认绘图起点的指针，未按下；请重新观察。")
        else:
            await self.point(*points[0])
        if self.screen is not None:
            try:
                validate(include_context=False)
            except TargetChangedError:
                # AirPlay can briefly show an in-flight pointer between the old and new
                # positions. Recheck a fresh observation before pressing, with the original guard.
                if await feedback.wait_after_action(self.screen, self._position_completed) is None:
                    raise
                validate(include_context=False)
        # A measured landing may include a correction that differs from the
        # absolute calibration model. Keep that acquired board pose as the
        # stroke origin; returning to theoretical coordinates would draw the
        # correction backwards into the first segment while the button is down.
        anchor = self._pos
        if anchor is None or self._link != link:
            raise DeviceError("拖动起点的指针位置或设备连接已变化，未按下。")
        origin = self._dx(points[0][0]), self._dy(points[0][1])
        targets = [(anchor[0] + self._dx(x) - origin[0], anchor[1] + self._dy(y) - origin[1])
                   for x, y in points[1:]]

        def validate_press():
            same_session()
            if validate is not None:
                validate()

        try:
            await self._send(epoch, "press", before_write=validate_press)
            for tx, ty in targets:
                # Check again under the device write lock: a reconnect can happen
                # while waiting for that lock, after the preceding segment ACK.
                await self._move_to(tx, ty, rehome=False, before_write=same_session)
            if hold:
                await asyncio.sleep(HOLD)
            same_session()
        finally:
            try:
                await shield_cleanup(asyncio.wait_for(self._command("release"), RELEASE_TIMEOUT))
            except asyncio.TimeoutError as exc:
                raise DeviceError("release cleanup timed out") from exc

    @_serialized
    @bounded_operation
    async def scroll(self, x: float, y: float, lines: int, *,
                     before_action: Callable[[], None] | None = None) -> None:
        epoch = getattr(self.device, "abort_epoch", 0)
        link = getattr(self.device, "links", 0)
        validate = self._gesture_guard((x, y), before_action, verified_pointer=True)
        await self.point_verified(x, y)
        if not self._pointer_confirmed:
            raise PointerLandingError("未确认滚动起点的指针，未滚动；请重新观察。")

        def validate_scroll():
            if self._stopped(epoch) or getattr(self.device, "links", 0) != link:
                raise DeviceError("滚动前设备连接已变化或已停止，未滚动。")
            if validate is not None:
                validate()

        await self._send(epoch, f"scroll {lines} 0", before_write=validate_scroll)

    @_serialized
    async def type(self, text: str, mode: TypeMode = "auto", *,
                   before_action: Callable[[], None] | None = None) -> None:
        """优先确认剪贴板后粘贴；未配置时仅 ASCII 可退回逐键输入。"""
        try:
            selected = select_type_mode(text, mode, **clipboard_mode_options(self.clipboard))
        except ValueError as exc:
            raise DeviceError(str(exc)) from exc
        epoch = getattr(self.device, "abort_epoch", 0)
        parts = type_chunks(text) if selected == "keys" else []
        async with self._typing:
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            if before_action is not None:
                before_action()  # serialization may have waited while the observed context changed
            chunks_sent = False
            chunks_acknowledged = characters_acknowledged = 0

            def invoke(stage: str) -> None:
                if before_action is None:
                    return
                if _accepts_stage(before_action):
                    before_action(stage=stage)
                elif stage == "accept":
                    return
                else:
                    before_action()

            async def send_input(command):
                nonlocal chunks_sent, chunks_acknowledged, characters_acknowledged
                paste_command = selected == "paste" and command == "key cmd+v"
                stage = "chunk" if command.startswith("type ") and chunks_sent else "context"
                validate_paste = getattr(self.clipboard, "validate_paste", None) if paste_command else None
                if paste_command and before_action is not None:
                    # Cmd+V still needs the original field, after a Shortcut alert has left the mirror.
                    deadline = time.monotonic() + PASTE_CONTEXT_WAIT
                    while True:
                        if self._stopped(epoch):
                            raise DeviceError("已紧急停止，后面的内容没有发出。")
                        try:
                            invoke("context")
                            break
                        except (RuntimeError, ValueError) as exc:
                            if type(exc).__name__ == "BlindError":
                                raise
                            if time.monotonic() >= deadline:
                                raise DeviceError("等待原输入画面恢复超时，没有粘贴；"
                                                  "请重新观察并核对快捷指令。") from exc
                        await asyncio.sleep(.05)

                def validate_input():
                    # Re-check at the write, including while this task waited on the device lock.
                    invoke("context" if paste_command else stage)
                    if self._stopped(epoch):
                        raise DeviceError("已紧急停止，后面的内容没有发出。")
                    if validate_paste is not None:
                        validate_paste()

                guarded = before_action is not None or validate_paste is not None
                await self._send(epoch, command, before_write=validate_input if guarded else None)
                if command.startswith("type "):
                    # Only a successful _send establishes a positive ACK. A
                    # later visual rejection must not erase that fact or retry.
                    completed_at = time.time()
                    chunks_acknowledged += 1
                    characters_acknowledged += len(command.removeprefix("type "))
                    chunks_sent = True
                    # Confirm that only the original input field changed during the ACK wait.
                    try:
                        invoke("accept")
                    except OperationDeadline:
                        raise
                    except (RuntimeError, ValueError) as exc:
                        raise TypingVerificationError(
                            str(exc), completed_at=completed_at, chunks_acknowledged=chunks_acknowledged,
                            chunks_total=len(parts), characters_acknowledged=characters_acknowledged) from exc

            if selected == "paste":
                await self.clipboard.paste(text, stopped=lambda: self._stopped(epoch),
                                           send=send_input)
            else:
                for part in parts:
                    await send_input(f"type {part}")

    @_serialized
    async def key(self, combo: str, *, before_action: Callable[[], None] | None = None) -> None:
        if before_action is not None:
            before_action()
        await self._send(getattr(self.device, "abort_epoch", 0), f"key {combo}", before_write=before_action)

    @property
    def app_search_available(self) -> bool:
        hid = (getattr(self.device, "board_status", None) or {}).get("hid") or {}
        return (os.environ.get("HIDAGENT_SPOTLIGHT_BLE_VERIFIED") == "1"
                and getattr(self.device, "connected", False) and hid.get("path") == "ble"
                and ocr.available())

    @bounded_operation
    @_serialized
    async def open_app_search(self, name: str, *, before_action: Callable[[], None] | None = None
                              ) -> tuple[float, float, float, float]:
        """Reach verified Home, open Spotlight, then fill its observed input field."""
        if not self.app_search_available:
            raise AppSearchError("App 搜索快捷键尚未在当前蓝牙设备上验证，或本地文字识别不可用。")
        try:
            validate_app_query(name)
        except ValueError as exc:
            raise AppSearchError(str(exc)) from exc

        try:
            original = freshness.require_fresh(self.screen)
            device = self.device
            link, generation = device.links, original.generation
            epoch = getattr(device, "abort_epoch", 0)
            opened_at = 0.0

            def validate_session():
                self._check_operation()
                if self._stopped(epoch):
                    raise AppSearchError("App 搜索期间已紧急停止，未继续输入。")
                if self.device is not device or not self.app_search_available or device.links != link:
                    raise AppSearchError("App 搜索期间蓝牙连接已变化。")
                current = freshness.require_fresh(self.screen)
                if current.generation != generation:
                    raise AppSearchError("App 搜索期间画面连接已变化。")
                return current

            # Consume the caller's original page once. The intentional Home
            # transition gets a new visual anchor, never a new link/epoch/session.
            validate_session()
            if before_action is not None:
                before_action()
            home = validate_session()
            if not assistive_vision.is_home_screen(home.frame):
                await self.go_home()
                home = validate_session()
                if not assistive_vision.is_home_screen(home.frame):
                    raise AppSearchError("没有确认返回主屏幕，未发送搜索快捷键或 App 名。")

            def validate_shortcut():
                nonlocal opened_at
                current = validate_session()
                if screen_compare.observation_changed(
                        home.frame, current.frame, caret_frames=getattr(self.screen, "recent_frames", ())):
                    raise AppSearchError("App 搜索前画面已变化。")
                opened_at = time.time()

            await self.key("cmd+space", before_action=validate_shortcut)
            if await feedback.wait_after_action(self.screen, opened_at) is None:
                raise AppSearchError("快捷键已发送，但没有收到新画面来确认 Spotlight。")
            validate_session()
            focused = freshness.require_fresh(self.screen, since=opened_at)
            if (focused.generation != generation
                    or not screen_compare.observation_changed(
                        home.frame, focused.frame,
                        caret_frames=getattr(self.screen, "recent_frames", ()))):
                raise AppSearchError("没有观察到进入 Spotlight，未输入 App 名。")

            async def read_focus(observed, anchors=None):
                labels = [(ocr.normalize_text(hit.text), hit.box)
                          for hit in await ocr.read_text_async(observed.frame) if hit.conf >= .8]
                validate_session()

                def same_label(label, box):
                    # Associate the same system labels, allowing only small OCR
                    # edge jitter. This does not authorize any input region.
                    return anchors is None or any(label == old_label and max(
                        abs(a-b) for a, b in zip(box, old_box, strict=True)) <= .01
                        for old_label, old_box in anchors)

                def search_field(region):
                    left, top, right, bottom = region
                    placeholder = any(label in SPOTLIGHT_PLACEHOLDERS and left <= box[0] < box[2] <= right
                                      and top <= box[1] < box[3] <= bottom and same_label(label, box)
                                      for label, box in labels)
                    marker = any(label in SPOTLIGHT_MARKERS and box[3] < top and same_label(label, box)
                                 for label, box in labels)
                    return placeholder and marker

                # Both label positions and the unique bounded caret must come
                # from this observation, never from an earlier keyboard/frame.
                field = InputContextGuard(self.screen, observed, allow_white=any(
                    label in SPOTLIGHT_MARKERS for label, _ in labels), accept_field=search_field)
                return labels, field

            labels, field = await read_focus(focused)
            known_spotlight = any(marker in SPOTLIGHT_MARKERS and placeholder in SPOTLIGHT_PLACEHOLDERS
                                  and marker_box[3] < placeholder_box[1]
                                  for marker, marker_box in labels for placeholder, placeholder_box in labels)
            if field.region is None and known_spotlight:
                # A first eligible frame may arrive during the caret's dark
                # phase. Observe once more without any HID, preserving the
                # original shortcut/session and requiring independent focus.
                validate_session()
                try:
                    later = await asyncio.wait_for(self.screen.fresh(focused.updated, SPOTLIGHT_FOCUS_WAIT),
                                                   SPOTLIGHT_FOCUS_WAIT)
                except asyncio.TimeoutError:
                    later = None
                validate_session()
                if later is not None:
                    alternate = freshness.require_fresh(self.screen, since=opened_at)
                    with Image.open(io.BytesIO(focused.frame)) as first_image, \
                            Image.open(io.BytesIO(alternate.frame)) as next_image:
                        same_size = first_image.size == next_image.size
                    if alternate.updated > focused.updated and same_size:
                        _, field = await read_focus(alternate, labels)
            if field.region is None:
                raise AppSearchError("无法确认 Spotlight 搜索框获得焦点，未输入 App 名。")

            def validate_field(*, stage="context"):
                validate_session()
                return field(stage=stage)

            await self.type(name, mode="keys", before_action=validate_field)
            return field.region
        except (AppSearchError, TypingVerificationError):
            raise
        except (RuntimeError, ValueError) as exc:
            raise AppSearchError(f"未能验证 App 搜索流程：{exc}") from exc

    @_serialized
    async def go_home(self, *, navigation_context=None, report_unconfirmed=False) -> None:
        options = {"navigation_context": navigation_context} if navigation_context is not None else {}
        if report_unconfirmed:
            options["report_unconfirmed"] = True
        await self.assistive_touch.go_home(**options)

    @_serialized
    async def go_back(self, *, before_action: Callable[[], None] | None = None, navigation_context=None) -> None:
        from .navigation_context import NavigationContext, from_navigation

        epoch, link = getattr(self.device, "abort_epoch", 0), getattr(self.device, "links", 0)
        if navigation_context is not None:
            if not isinstance(navigation_context, NavigationContext) or navigation_context.action != "back":
                raise ValueError("back requires a detected Back navigation context")
            navigation_context.validate_current(self.screen)
        else:
            navigation_context = from_navigation("back", freshness.require_fresh(self.screen))
        if navigation_context is None:
            await self.swipe(0.005, 0.45, 0.6, 0.45, hold=False, before_action=before_action)
            return
        observed, target = navigation_context.observation, navigation_context.target

        def validate_back(*, positioned=False):
            self._check_operation()
            current = freshness.require_fresh(self.screen)
            if (self._stopped(epoch) or getattr(self.device, "links", 0) != link
                    or current.generation != observed.generation):
                raise RuntimeError("返回前设备或画面连接已变化，未发送点击；请重新观察。")
            # At entry these fields can still describe the previous page's tap.
            # Only this Back gesture's completed positioning supplies evidence
            # for the final write; the initial check uses the observed nav bar.
            cursor = self._pointer_overlay if positioned and self._pointer_confirmed else None
            reference = self._pointer_reference if cursor else None
            navigation_context.validate_current(self.screen, cursor=cursor, reference=reference)
            if before_action is not None:
                if _accepts_keywords(before_action, "cursor", "reference"):
                    before_action(cursor=cursor, reference=reference)
                else:
                    before_action()

        validate_back()
        # A visible navigation control avoids a clipped pointer at the physical
        # edge. Its actual target and original session still guard the HID write;
        # an uncertain click never triggers a second fallback gesture.
        await self.tap_verified(*target.center, target_box=target.box,
                                target_frame=observed.frame, before_click=lambda: validate_back(positioned=True),
                                navigation_context=navigation_context)

    def _dx(self, x: float) -> int:
        return round((self._fix(x, "x") - self.origin["x"]) * self.span["w"])

    def _dy(self, y: float) -> int:
        return round((self._fix(y, "y") - self.origin["y"]) * self.span["h"])
