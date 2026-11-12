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
from dataclasses import dataclass

from . import video7201, vision

log = logging.getLogger(__name__)


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

    def __call__(self):
        from .freshness import FrameSnapshot as ObservedFrame

        return ObservedFrame(self.frame, self.updated, self.board_ts, self.generation, self.observed_live)


class Screen:
    def __init__(self, host: str = "", token: str = "", port: int = 7201,
                 fps: int | None = None, quality: int = 75):
        self._fixed_host = host
        self._fixed_token = token
        self.port = port
        self.quality = quality
        self.fps = fps if fps is not None else int(os.environ.get("VIDEO_FPS", "8"))
        self.frame: bytes | None = None
        self.updated = 0.0
        # board millis() of the access unit behind `frame` (None: unknown). See video7201.run_tcp.
        self.frame_ts: int | None = None
        self._next_ts: int | None = None
        self._snapshot: FrameSnapshot | None = None
        self._frame_lock = threading.RLock()
        self.generation = 0
        # one decoder for the whole stream from this board, so a reconnect can go on from its last AU
        self._decoder = None
        self._decoder_for: tuple[str, str] | None = None
        self.last_error = ""
        self._device = None
        self._started = False
        self._stopping = False
        self._session_ok = False
        self._stop = threading.Event()
        self._sockets: list = []

    def bind(self, device) -> None:
        self._device = device

    def start(self) -> None:
        with self._frame_lock:
            if self._started:
                return
            self._started = True
            self._stopping = False
            self._stop.clear()
        threading.Thread(target=self._supervise, name="screen-7201", daemon=True).start()

    def stop(self) -> None:
        # Eligibility changes share the same lock as final HID validation/write.
        # A receiver/stop thread must not invalidate an already validated frame
        # between that check and the synchronous link.write call.
        with self._frame_lock:
            self._stopping = True
            self._stop.set()
            self._session_ok = False
        for sock in list(self._sockets):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

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

    def _supervise(self) -> None:
        while not self._stopping:
            host, token = self._endpoint()
            if not host or not token:
                self._on_disconnect()
                self.last_error = "还没有板子的 IP 或 vtok。COM 口连上之后，服务会从 0 netinfo 里读。"
                time.sleep(1)
                continue
            with self._frame_lock:
                if self._stopping:
                    return
                self._stop.clear()
            if self._decoder_for != (host, token):  # another board or a rebooted one: nothing to go on from
                self._decoder, self._decoder_for = video7201.H264Decoder(), (host, token)
            try:
                video7201.run_tcp(
                    host, self.port, token, self._on_jpeg, self._stop,
                    quality=self.quality, max_fps=max(1, self.fps),
                    on_ready=self._on_ready, on_stall=self._on_stall, sockets=self._sockets,
                    on_ts=self._on_ts, decoder=self._decoder,
                )
                self.last_error = "7201 连接结束了，正在重连。"
            except Exception as exc:
                self.last_error = _friendly_video_error(exc)
                log.info("screen: %s", self.last_error)
            self._on_disconnect()
            if not self._stopping:
                time.sleep(1)

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
            self._snapshot = FrameSnapshot(None, None, time.monotonic(), 0.0, self.generation, False)
        self.last_error = ""

    def _on_stall(self) -> None:
        self.last_error = "画面停住了。请在控制中心重新选一次「屏幕镜像 → ESP32 Agent」。板子可能还留着旧缓冲。"

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
            self._snapshot = FrameSnapshot(
                jpeg, board_ts, arrived, updated, self.generation, self._session_ok and jpeg is not None)
        if self.last_error.startswith("画面停住了"):
            self.last_error = ""

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
    if isinstance(exc, UnicodeEncodeError) or "ascii" in text.lower():
        return "vtok 里有非 ASCII 字符，hello 发不出去。请重新读一次 netinfo。"
    if "timed out" in text or "Timeout" in text:
        return "连不上板子的 7201 端口。确认电脑和板子在同一个局域网，并且手机已经开始屏幕镜像。"
    if "refused" in text or "10061" in text:
        return "板子拒绝了 7201。它可能还没连上 Wi-Fi，或镜像接收器没起来。看串口 0 status 里的 ap 字段。"
    if "closed" in text:
        return "7201 被关掉了。vtok 每次重启都会变，服务会重新读串口；若一直这样，请重新选一次屏幕镜像。"
    return "画面连接失败。请重新选一次屏幕镜像；若 iOS 16/17 的列表里没有 ESP32 Agent，看 docs/troubleshooting.md。"
