"""Touch-like gestures on the phone, built from relative mouse moves.

The pointer is relative, so the server tracks where it is (in mouse units) and moves
from there; every few gestures it slams it into the top-left corner (`home`) again to
cancel drift. Coordinates are fractions of the screen (0..1, origin top-left), so they
work for any frame size.
"""

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

from . import vision
from .device import RELEASE_TIMEOUT, Device, DeviceError, exchange_timing
from .operation import OPERATION_TIMEOUT, bounded_operation, check_operation, shield_cleanup
from .screen import FrameSnapshot

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
# A first connection or an expired resume point can replay video since the last IDR (26 minutes of frames
# and 45 s to catch up on the test phone). Arrival lag above REPLAY_LAG signals replay; shorter lag still
# needs the per-nudge timestamp check. Normal mirror lag is 0.15–1.5 s; a still screen keeps its last frame.
REPLAY_LAG = 5          # seconds
CATCH_UP_WAIT = 15      # leave time for verification and a response inside the total operation budget
AIM_TOLERANCE = 0.008   # screen fraction; a bigger miss is corrected before pressing
MAX_MISS = 0.06         # bigger misses are still corrected, but not learned from (they may be misreadings)
USB_PACE = 3            # firmware HID_PACE_USB. A saved file with no pace number is this schedule.


class Phone:
    operation_timeout = OPERATION_TIMEOUT

    def __init__(self, device: Device, calibration_file: Path, screen=None):
        self.device, self._file, self.screen = device, calibration_file, screen
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

    @property
    def calibrated(self) -> bool:
        return self.span is not None

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

    def _stopped(self, epoch: int) -> bool:
        return getattr(self.device, "abort_epoch", 0) != epoch

    @staticmethod
    def _check_operation() -> None:
        check_operation()

    async def _command(self, command: str) -> None:
        if command != "release":
            self._check_operation()
        await self.device.send(command)

    async def _send(self, epoch: int, command: str) -> float:
        if self._stopped(epoch):
            raise DeviceError("已紧急停止，后面的内容没有发出。")
        started = time.monotonic()
        with exchange_timing() as timing:
            await self._command(command)
        return timing.sent_at if timing.sent_at is not None else started

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
                            conservative: bool = False, epoch: int | None = None) -> tuple[float, float] | None:
        """Wiggle the pointer and find where it rests (the span may still be unknown).

        The mirror lags and stays silent while the screen is still, so the frame taken before the nudge may
        predate the last move. Every frame that arrives is kept and each new one is compared with the ones
        before it; the pointer is the round blob that jumped sideways by about the nudge. If the nudge never
        shows up, the way back is watched the same way. Both waits are bounded (POINTER_WAIT).
        """
        if epoch is None:
            epoch = getattr(self.device, "abort_epoch", 0)
        clock = await self._catch_up(epoch)
        await self._wait_screen(epoch, self.screen.settled(
            quiet=0.2, least=0.3, most=1.5, transport=conservative))
        if self._stopped(epoch):
            raise DeviceError("已紧急停止，后面的内容没有发出。")
        nudge = NUDGE if near is None or near[0] < 0.9 else -NUDGE  # stay clear of the right edge
        shift = nudge / self.span["w"] if self.span else 0.025 * (1 if nudge > 0 else -1)
        # Rebuild the baseline after catching up and settling, from one coherent publication.
        baseline = self._snapshot()
        history = [baseline.frame] if baseline else []
        sent = await self._send(epoch, f"move {nudge} 0")
        found = await self._watch(epoch, history, near, shift, sent, conservative, True, clock)
        # An interrupted/expired search must never issue a cleanup HID movement.
        back = await self._send(epoch, f"move {-nudge} 0")
        if found is None and not self._stopped(epoch):
            # the nudge was not seen: the pointer rests where the way back leaves it
            found = await self._watch(epoch, history, near, -shift, back, conservative, False, clock)
        return found if found and 0.03 < found[0] < 0.97 and 0.03 < found[1] < 0.97 else None

    async def _watch(self, epoch: int, history: list, near, shift: float, since: float,
                     conservative: bool, at_start: bool, clock: float | None = None) -> tuple[float, float] | None:
        """Watch new frames until one shows the pointer nudged by `shift` from an earlier frame.

        Returns where the pointer was before the nudge (at_start) or after it.
        """
        deadline = time.monotonic() + POINTER_WAIT + (0.5 if conservative else 0)
        sent = since  # fixed command boundary; `since` advances as publications are consumed
        searches = ([(near, (0.12, 0.08))] if near else []) + [((0.5, 0.5), (0.5, 0.5))]
        while time.monotonic() < deadline:
            self._check_operation()
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            snapshot = await self._fresh_snapshot(since, min(.2, max(0, deadline - time.monotonic())))
            if snapshot:
                after, since = snapshot.frame, snapshot.arrived
                age = self._frame_age(clock, snapshot)
                if age is not None and age > REPLAY_LAG:  # replayed old video, not the nudge
                    history.clear()
                    await asyncio.sleep(.02)
                    continue
                if (clock is not None and snapshot.board_ts is not None
                        and self._board_delta(snapshot.board_ts, sent * 1000 + clock) <= 0):
                    # A delayed pre-nudge landing can establish the baseline (normal AirPlay lag),
                    # but it cannot be evidence of this nudge, even within the last five seconds.
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
        """Board millis() minus monotonic ms; None for mirrors without board timestamps.

        Use the query's actual write time, giving an upper bound on the board clock at each HID send. A midpoint
        estimate can underestimate it by half an RTT and accidentally admit frames preceding the command.
        """
        query = getattr(self.device, "query", None)
        snapshot = self._snapshot()
        if query is None or snapshot is None or snapshot.board_ts is None:
            return None
        try:
            sent = time.monotonic()
            with exchange_timing() as timing:
                up = json.loads(await query("status"))["up"]
            sent = timing.sent_at if timing.sent_at is not None else sent
            return up + 1 - sent * 1000  # millis() truncates: include its sub-millisecond uncertainty
        except (DeviceError, OSError, ValueError, KeyError, TypeError) as exc:
            raise DeviceError("无法确认画面对应的板子时间；没有继续验证或点击，请重试。") from exc

    @staticmethod
    def _board_delta(later: float, earlier: float) -> float:
        """Signed u32 millis distance, including a wrap (timestamps within half the 49.7 day period)."""
        return (later - earlier + 2**31) % 2**32 - 2**31

    def _snapshot(self) -> FrameSnapshot | None:
        if hasattr(self.screen, "snapshot"):
            return self.screen.snapshot
        # Compatibility for legacy, untimestamped screen providers. Production mirrors publish snapshots.
        frame = self.screen.frame
        if frame is None:
            return None
        arrived = time.monotonic() - max(0, time.time() - self.screen.updated)
        return FrameSnapshot(frame, getattr(self.screen, "frame_ts", None), arrived)

    async def _fresh_snapshot(self, since: float, timeout: float) -> FrameSnapshot | None:
        if hasattr(self.screen, "fresh_snapshot"):
            return await self.screen.fresh_snapshot(since, timeout)
        after = await self.screen.fresh(time.time() - (time.monotonic() - since), timeout)
        return self._snapshot() if after else None

    def _frame_age(self, clock: float | None, snapshot: FrameSnapshot | None = None) -> float | None:
        """Seconds the shown frame was already behind the board's clock when it arrived; None when unknown.

        Not its age now: a still screen keeps its last live frame for minutes, and that is the screen as it is.
        """
        snapshot = snapshot or self._snapshot()
        if clock is None or snapshot is None or snapshot.board_ts is None:
            return None
        return self._board_delta(snapshot.arrived * 1000 + clock, snapshot.board_ts) / 1000

    async def _catch_up(self, epoch: int) -> float | None:
        """Wait (bounded) until the mirror shows the phone as it is now, not the board's replay of old video.

        While frames keep arriving far behind the board's clock the replay is running. A pause of 2 s ends the
        wait (the screen may simply be still); frames that come late after that are still ignored by _watch.
        Returns the board clock offset.
        """
        clock = await self._board_clock()
        if clock is None:
            return None
        deadline, logged = time.monotonic() + CATCH_UP_WAIT, False
        while time.monotonic() < deadline:
            self._check_operation()
            if self._stopped(epoch):
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            snapshot = self._snapshot()
            age = self._frame_age(clock, snapshot)
            if age is None or age < REPLAY_LAG or (snapshot and time.monotonic() - snapshot.arrived > 2):
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
                self._check_operation()
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

    async def point(self, x: float, y: float, aim: bool = False) -> None:
        self.adopt_pace(getattr(self.device, "pace_version", None))
        if not self.calibrated:
            raise RuntimeError("pointer not calibrated")
        await self._move_to(self._dx(x), self._dy(y))
        if aim and self.screen is not None:
            await self._aim(x, y)

    @bounded_operation
    async def point_verified(self, x: float, y: float) -> tuple[float, float]:
        """Locate and correct the real pointer; never silently accept a blind move."""
        if self.screen is None or not self.screen.live:
            raise RuntimeError("live screen required to locate pointer")
        cold = (self._pos is None or self._moves >= REHOME_EVERY
                or self._link != getattr(self.device, "links", self._link))
        await self.point(x, y)
        for attempt in range(3):
            found = (await self._find_pointer((x, y), conservative=True) if cold or attempt == 2
                     else await self._find_pointer((x, y)))
            if found is None:
                continue
            fx, fy = found
            if attempt == 0 and self.span and max(abs(fx - x), abs(fy - y)) <= MAX_MISS:
                # long moves go further than the calibration stops (iOS accelerates): 422 units across landed
                # 30 px right of the fit on the 15 Pro Max; learn it so the next move starts closer
                self._seen["x"].append((self._fix(x, "x"), fx))
                self._seen["y"].append((self._fix(y, "y"), fy))
                self._save()
            if abs(fx - x) <= AIM_TOLERANCE and abs(fy - y) <= AIM_TOLERANCE:
                return found
            dx, dy = round((x - fx) * self.span["w"]), round((y - fy) * self.span["h"])
            await self._command(f"move {dx} {dy}")
            self._pos = (self._pos[0] + dx, self._pos[1] + dy)
        raise RuntimeError("pointer could not be verified; no click sent; observe or recalibrate")

    @bounded_operation
    async def tap_verified(self, x: float, y: float) -> None:
        await self.point_verified(x, y)
        await self._command("click")
        await asyncio.sleep(TAP_GAP)

    async def _aim(self, x: float, y: float) -> None:
        """Closed loop: find the real pointer on screen, fix a miss, and learn the error for later moves."""
        found = await self._find_pointer((x, y))
        if not found:
            log.debug("aim (%.3f, %.3f): pointer not found", x, y)
            return
        fx, fy = found
        log.debug("aim (%.3f, %.3f): pointer at (%.3f, %.3f)", x, y, fx, fy)
        if max(abs(fx - x), abs(fy - y)) <= MAX_MISS:  # learn from normal drift only, never from a stray reading
            self._seen["x"].append((self._fix(x, "x"), fx))
            self._seen["y"].append((self._fix(y, "y"), fy))
            self._save()
        if abs(fx - x) <= AIM_TOLERANCE and abs(fy - y) <= AIM_TOLERANCE:
            return
        dx, dy = round((x - fx) * self.span["w"]), round((y - fy) * self.span["h"])  # fix this miss right away
        await self._command(f"move {dx} {dy}")
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
            await self._command("release")
            self._pos, self._link = None, self.device.links
        if self._pos is None or (rehome and self._moves >= REHOME_EVERY):
            await self._command("home")
            self._pos, self._moves = (0, 0), 0
        x, y = self._pos
        self._pos = None  # unknown until the move succeeds
        await self._command(f"move {tx - x} {ty - y}")
        self._pos, self._moves = (tx, ty), self._moves + 1

    async def reset(self) -> None:
        """Let go of the button and re-home the pointer, whatever state a gesture left."""
        self._pos = None
        await self._command("release")
        await self._command("home")
        self._pos, self._moves = (0, 0), 0

    async def tap(self, x: float, y: float, aim: bool = False) -> None:
        """aim=True finds the pointer on screen first (slower); worth it when a tap just missed."""
        await self.point(x, y, aim)
        await self._command("click")
        await asyncio.sleep(TAP_GAP)

    async def swipe(self, x1: float, y1: float, x2: float, y2: float, hold: bool = True) -> None:
        await self.drag([(x1, y1), (x2, y2)], hold)

    async def drag(self, points: list[tuple[float, float]], hold: bool = True) -> None:
        """Press at the first point, move through the rest, release: one continuous stroke.

        hold=True rests at the end first, so lists and pickers stop exactly there instead of flinging on.
        System gestures (home, back) need the flick, so they pass hold=False.
        """
        await self.point(*points[0])
        try:
            await self._command("press")
            for x, y in points[1:]:
                await self._move_to(self._dx(x), self._dy(y), rehome=False)  # never home mid-drag
            if hold:
                await asyncio.sleep(HOLD)
        finally:
            try:
                await shield_cleanup(asyncio.wait_for(self._command("release"), RELEASE_TIMEOUT))
            except asyncio.TimeoutError as exc:
                raise DeviceError("release cleanup timed out") from exc

    async def scroll(self, x: float, y: float, lines: int) -> None:
        await self.point(x, y)
        await self._command(f"scroll {lines} 0")

    async def type(self, text: str) -> None:
        # 固件串口一行最多 512 字节，含 "<id> type "。超长就切开，每段都要等到 ok。
        epoch = getattr(self.device, "abort_epoch", 0)
        for part in type_chunks(text):
            if getattr(self.device, "abort_epoch", 0) != epoch:
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            await self._command(f"type {part}")

    async def key(self, combo: str) -> None:
        await self._command(f"key {combo}")

    async def go_home(self) -> None:
        await self.swipe(0.5, 0.995, 0.5, 0.6, hold=False)  # resting here would open the app switcher

    async def go_back(self) -> None:
        await self.swipe(0.005, 0.45, 0.6, 0.45, hold=False)

    def _dx(self, x: float) -> int:
        return round(min(max(self._fix(x, "x") - self.origin["x"], 0), 1) * self.span["w"])

    def _dy(self, y: float) -> int:
        return round(min(max(self._fix(y, "y") - self.origin["y"], 0), 1) * self.span["h"])
