"""手机画面。默认从板子 TCP 7201 收 H.264，用 PyAV 解成 JPEG。

对外仍是原来的 Screen：start/stop、live、frame、updated、aspect、settled、fresh。
上面的手势和网页不用知道传输换过。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

from . import freshness, video7201, vision

log = logging.getLogger(__name__)
_REAL_SLEEP = time.sleep
PREVIEW_HOLD_S = 15.0


@dataclass(frozen=True)
class FrameSnapshot:
    """One published image and its clocks; readers retain this object across awaits.

    Calling the snapshot returns the shared observation record (wall-clock updated,
    board timestamp, generation). Attribute access stays the monotonic arrival record.
    """

    frame: bytes | None
    board_ts: int | None
    arrived: float  # host monotonic seconds, never wall-clock time
    updated: float = 0.0
    generation: int = 0
    observed_live: bool = False
    # Seconds between iPhone capture and arrival at this host (HAFE v2);
    # None when the frame carries no capture time (board path, legacy
    # UxPlay, HAFE/1). `updated - capture_age_s` is the capture wall time.
    capture_age_s: float | None = None

    def __call__(self):
        from .freshness import FrameSnapshot as ObservedFrame

        return ObservedFrame(self.frame, self.updated, self.board_ts, self.generation, self.observed_live,
                             self.capture_age_s)


@dataclass(frozen=True)
class PreviewStatus:
    """The display state and corresponding pixels, independent of action eligibility."""

    state: Literal["connecting", "waiting", "live", "idle", "replaying", "disconnected", "stalled", "unverified"]
    frame_age_s: float | None
    replay_lag_s: float | None
    heartbeat_age_s: float | None
    generation: int
    frame: bytes | None = None


class RecentFrameHistory:
    """Bounded, session-local pixel history shared by board and Mac receivers."""

    def _remember_snapshot(self, frame: FrameSnapshot) -> None:
        """Retain only a few recent immutable frames for caret animation evidence."""
        with self._frame_lock:
            if self._recent_frames and self._recent_frames[-1].generation != frame.generation:
                self._recent_frames.clear()
            if frame.frame and frame.observed_live:
                prev = self._recent_frames[-1] if self._recent_frames else None
                if prev is None or frame.arrived > prev.arrived or (
                        frame.arrived == prev.arrived
                        and (frame.updated > prev.updated or frame.frame != prev.frame)):
                    self._recent_frames.append(frame)
            while self._recent_frames and frame.arrived - self._recent_frames[0].arrived > 4:
                self._recent_frames.popleft()

    @property
    def recent_frames(self) -> tuple[FrameSnapshot, ...]:
        with self._frame_lock:
            now = time.monotonic()
            while self._recent_frames and (self._recent_frames[0].generation != self.generation
                    or now - self._recent_frames[0].arrived > 4):
                self._recent_frames.popleft()
            return tuple(frame for frame in self._recent_frames if frame.generation == self.generation
                         and 0 <= now - frame.arrived <= 4)


def _parse_fps(raw: str | None, default: int = 8) -> int:
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("invalid VIDEO_FPS=%r; using default %d", raw, default)
        return default
    if value < 1 or value > 60:
        clamped = max(1, min(60, value))
        log.warning("VIDEO_FPS=%d out of range 1..60; clamped to %d", value, clamped)
        return clamped
    return value


class Screen(RecentFrameHistory):
    def __init__(self, host: str = "", token: str = "", port: int = 7201,
                 fps: int | None = None, quality: int = 75):
        self._fixed_host = host
        self._fixed_token = token
        self.port = port
        self.quality = quality
        self.fps = fps if fps is not None else _parse_fps(os.environ.get("VIDEO_FPS"))
        self.frame: bytes | None = None
        self.updated = 0.0
        # board millis() of the access unit behind `frame` (None: unknown). See video7201.run_tcp.
        self.frame_ts: int | None = None
        self._next_ts: int | None = None
        self._snapshot: FrameSnapshot | None = None
        self._frame_lock = threading.RLock()
        self._recent_frames = deque(maxlen=32)
        self.generation = 0
        # one decoder for the whole stream from this board, so a reconnect can go on from its last AU
        self._decoder = None
        self._decoder_for: tuple[str, str] | None = None
        self.last_error = ""
        self._device = None
        self._started = False
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._session_ok = False
        self._transport_stalled = False
        self._last_heartbeat_at: float | None = None
        self._clock_not_before = float("-inf")
        self._preview_lag_bounds: tuple[float, float] | None = None
        self._stop = threading.Event()
        self._sockets: list = []

    def bind(self, device) -> None:
        self._device = device

    def start(self) -> None:
        with self._frame_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._started = True
            self._stopping = False
            self._stop.clear()
            self._thread = threading.Thread(target=self._supervise, name="screen-7201", daemon=True)
            thread = self._thread
        thread.start()

    def stop(self) -> None:
        # Eligibility changes share the same lock as final HID validation/write.
        # A receiver/stop thread must not invalidate an already validated frame
        # between that check and the synchronous link.write call.
        with self._frame_lock:
            self._stopping = True
            self._stop.set()
            self._session_ok = False
            thread = self._thread
        for sock in list(self._sockets):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._frame_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = None

    def _endpoint(self) -> tuple[str, str]:
        host = self._fixed_host or os.environ.get("BOARD_HOST", "").strip()
        token = self._fixed_token or os.environ.get("BOARD_VTOK", "").strip()
        device = self._device
        if not host and device is not None:
            host = getattr(device, "ip", "") or ""
        if not token and device is not None:
            token = getattr(device, "vtok", "") or ""
        if host in ("0.0.0.0", "0.0.0.0/0"):
            host = ""
        return host, token

    def _wait_retry(self, seconds: float) -> None:
        if time.sleep is not _REAL_SLEEP:
            time.sleep(seconds)
            return
        self._stop.wait(seconds)

    def _supervise(self) -> None:
        try:
            while not self._stopping:
                host, token = self._endpoint()
                if not host or not token:
                    self._on_disconnect()
                    self.last_error = "还没有板子的 IP 或 vtok。COM 口连上之后，服务会从 0 netinfo 里读。"
                    self._wait_retry(1)
                    continue
                with self._frame_lock:
                    if self._stopping:
                        return
                    self._stop.clear()
                    if self._decoder_for != (host, token):  # another board or a rebooted one
                        self._decoder, self._decoder_for = video7201.H264Decoder(), (host, token)
                        self._clock_not_before = time.monotonic()
                try:
                    video7201.run_tcp(
                        host, self.port, token, self._on_jpeg, self._stop,
                        quality=self.quality, max_fps=max(1, self.fps),
                        on_ready=self._on_ready, on_stall=self._on_stall, sockets=self._sockets,
                        on_ts=self._on_ts, decoder=self._decoder, on_heartbeat=self._on_heartbeat,
                    )
                    self.last_error = "7201 连接结束了，正在重连。"
                except Exception as exc:
                    self.last_error = _friendly_video_error(exc)
                    log.info("screen: %s", self.last_error)
                self._on_disconnect()
                if not self._stopping:
                    self._wait_retry(1)
        finally:
            with self._frame_lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _on_disconnect(self) -> None:
        with self._frame_lock:
            self._session_ok = False

    def _on_ready(self) -> None:
        with self._frame_lock:
            if self._stopping:
                return
            self.generation += 1
            self.frame = None
            self.updated = 0.0
            self.frame_ts = None
            self._next_ts = None
            self._session_ok = True
            self._transport_stalled = False
            self._last_heartbeat_at = time.monotonic()
            self._snapshot = FrameSnapshot(None, None, self._last_heartbeat_at, 0.0, self.generation, False)
            self._preview_lag_bounds = None
        self.last_error = ""

    def _on_stall(self) -> None:
        with self._frame_lock:
            self._transport_stalled = True
            self._session_ok = False

    def _on_heartbeat(self, arrived: float) -> None:
        with self._frame_lock:
            if self._session_ok:
                self._last_heartbeat_at = arrived

    def _on_ts(self, ts) -> None:
        with self._frame_lock:
            self._next_ts = ts

    def _on_jpeg(self, jpeg: bytes) -> None:
        arrived = time.monotonic()
        with self._frame_lock:
            board_ts = self._next_ts
            # Legacy fields update before the wall clock is read. A concurrent reader that
            # does not take this lock can still be holding the previous immutable snapshot.
            self.frame_ts, self._next_ts = board_ts, None
            self.frame = jpeg
            updated = time.time()
            self.updated = updated
            # A test can publish a frame before start(); that screen is live, matching live().
            observed_live = jpeg is not None and not self._stopping and (not self._started or self._session_ok)
            self._snapshot = FrameSnapshot(
                jpeg, board_ts, arrived, updated, self.generation, observed_live)
            self._remember_snapshot(self._snapshot)
            self._preview_lag_bounds = self._preview_lag(self._snapshot, arrived)

    @property
    def snapshot(self) -> FrameSnapshot | None:
        return self._snapshot

    @property
    def live(self) -> bool:
        with self._frame_lock:
            if self.frame is None or self._stopping:
                return False
            # 测试直接塞 frame、没有启动线程时，当作画面可用。
            if not self._started:
                return True
            # 静止画面时 AirPlay 不推新帧。会话还在、最近 90 秒内有过帧，就算活着。
            snapshot = self._snapshot
            return self._session_ok and snapshot is not None and time.monotonic() - snapshot.arrived < 90

    def _preview_lag(self, snapshot: FrameSnapshot, now: float) -> tuple[float, float] | None:
        """Bound frame age at arrival using the interval enclosing a serial uptime sample."""
        device = self._device
        sample = getattr(device, "board_clock_sample", None)
        if sample is None or snapshot.board_ts is None:
            return None
        device_host = getattr(device, "ip", "")
        if device_host and self._decoder_for is not None and device_host != self._decoder_for[0]:
            return None
        up_ms, sent, received = sample
        if sent < self._clock_not_before or not (0 <= received - sent <= 1 and 0 <= now - received <= PREVIEW_HOLD_S):
            return None
        lower = freshness.board_delta(up_ms + (snapshot.arrived - received) * 1000, snapshot.board_ts) / 1000
        upper = lower + received - sent
        # A substantially future stamp indicates an incompatible clock, e.g. around a board reboot.
        if upper < -.1:
            return None
        return lower, upper

    def preview(self) -> PreviewStatus:
        with self._frame_lock:
            now = time.monotonic()
            snapshot = self._snapshot
            frame_age = max(0.0, now - snapshot.arrived) if snapshot and snapshot.frame else None
            heartbeat = self._last_heartbeat_at
            heartbeat_age = max(0.0, now - heartbeat) if heartbeat is not None else None
            lag = self._preview_lag_bounds
            if snapshot and snapshot.frame and (lag is None or lag[0] <= freshness.MAX_REPLAY_LAG < lag[1]):
                # A later serial sample can classify a frame that arrived before the clock was known.
                # Retain a definite result for the frame even if serial status becomes unavailable.
                lag = self._preview_lag(snapshot, now) or lag
                self._preview_lag_bounds = lag
            delay = max(0.0, sum(lag) / 2) if lag is not None else None
            details = (frame_age, delay, heartbeat_age, self.generation)

            if self._stopping:
                return PreviewStatus("disconnected", *details)
            if self._transport_stalled:
                return PreviewStatus("stalled", *details)
            if self._started and not self._session_ok:
                return PreviewStatus("disconnected" if self.generation or self.last_error else "connecting", *details)
            if heartbeat is not None and video7201.stream_is_dead(
                    now, snapshot.arrived if frame_age is not None else heartbeat, heartbeat):
                return PreviewStatus("stalled", *details)
            if frame_age is None or frame_age > PREVIEW_HOLD_S:
                return PreviewStatus("waiting" if self._session_ok else "connecting", *details)
            if lag is not None and lag[0] > freshness.MAX_REPLAY_LAG:
                return PreviewStatus("replaying", *details)
            if lag is None or lag[1] > freshness.MAX_REPLAY_LAG:
                return PreviewStatus("unverified", *details)
            return PreviewStatus("live" if frame_age <= freshness.MAX_FRAME_AGE else "idle", *details, snapshot.frame)

    @property
    def aspect(self) -> float:
        """Frame width / height, read from the JPEG header (portrait iPhone as a fallback)."""
        blob = self.frame or b""
        i = blob.find(b"\xff\xc0")
        if i < 0 or len(blob) < i + 9:
            return 9 / 19.5
        return int.from_bytes(blob[i + 7:i + 9], "big") / int.from_bytes(blob[i + 5:i + 7], "big")

    async def settled(self, quiet: float = 0.35, least: float = 0.25, most: float = 4,
                      transport: bool = False) -> None:
        """Wait for quiet image content, not quiet transport: AirPlay may repeat identical frames."""
        if transport:
            started = time.monotonic()
            await asyncio.sleep(least)
            while ((snapshot := self.snapshot) is not None and time.monotonic() - snapshot.arrived < quiet
                   and time.monotonic() - started < most):
                await asyncio.sleep(.1)
            return
        started = last_change = time.monotonic()
        previous = self.frame
        while True:
            now, current = time.monotonic(), self.frame
            if current != previous:
                if vision.changed(previous, current):
                    last_change = now
                previous = current
            if now - started >= most or (now - started >= least and now - last_change >= quiet):
                return
            await asyncio.sleep(min(0.05, max(0, most - (now - started))))

    async def fresh(self, since: float, timeout: float = 0.8) -> bytes | None:
        """The first frame received after `since`, or None if none arrives in time."""
        deadline = time.monotonic() + timeout
        while self.updated <= since:
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.02)
        return self.frame

    async def fresh_snapshot(self, since: float, timeout: float = 0.8) -> FrameSnapshot | None:
        """A coherent frame received after monotonic `since`, or None within `timeout`."""
        deadline = time.monotonic() + timeout
        while True:
            snapshot = self.snapshot
            if snapshot is not None and snapshot.arrived > since:
                return snapshot
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.02)


def _friendly_video_error(exc: Exception) -> str:
    text = str(exc)
    if isinstance(exc, video7201.StreamStalled):
        return "板子的 7201 视频连接没有响应，正在自动重连。"
    if isinstance(exc, UnicodeEncodeError) or "ascii" in text.lower():
        return "vtok 里有非 ASCII 字符，hello 发不出去。请重新读一次 netinfo。"
    if "timed out" in text or "Timeout" in text:
        return "连不上板子的 7201 端口。确认电脑和板子在同一个局域网，并且手机已经开始屏幕镜像。"
    if "refused" in text or "10061" in text:
        return "板子拒绝了 7201。它可能还没连上 Wi-Fi，或镜像接收器没起来。看串口 0 status 里的 ap 字段。"
    if "closed" in text:
        return "7201 被关掉了。vtok 每次重启都会变，服务会重新读串口；若一直这样，请重新选一次屏幕镜像。"
    return "画面连接失败。请重新选一次屏幕镜像；若 iOS 16/17 的列表里没有 ESP32 Agent，看 docs/troubleshooting.md。"
