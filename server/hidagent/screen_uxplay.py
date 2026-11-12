"""Mac 退路：电脑上的 UxPlay。默认路径不要用它。

Windows 没有 pass_fds，也不能把 GStreamer 当成小白的安装步骤。
只有 iOS 16/17 镜像不到板载接收器、并且人在 Mac 上时，才设置 SCREEN_BACKEND=uxplay。
"""

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time

from . import messages, vision
from .screen import FrameSnapshot

log = logging.getLogger(__name__)


class UxPlayScreen:
    """Runs UxPlay with a GStreamer sink that writes JPEGs into a pipe; keeps the latest one."""

    def __init__(self, name: str = "ESP32 to iPhone", fps: int = 8, quality: int = 75):
        self.name, self.fps, self.quality = name, fps, quality
        self.frame: bytes | None = None
        self.updated = 0.0
        self._snapshot: FrameSnapshot | None = None
        self._proc = None
        self._clients_at, self._clients_n = 0.0, None
        self._stopping = False

    def start(self) -> None:
        if sys.platform == "win32":
            raise RuntimeError(messages.UXPLAY_WINDOWS)
        threading.Thread(target=self._supervise, daemon=True).start()

    def stop(self) -> None:
        self._stopping = True
        if self._proc:
            self._proc.terminate()

    def _supervise(self) -> None:
        """Keep UxPlay running: it exits on some disconnects, so restart it every time."""
        while not self._stopping:
            read_fd, write_fd = os.pipe()
            sink = (f"videorate drop-only=true ! video/x-raw,framerate={self.fps}/1 ! "
                    f"jpegenc quality={self.quality} ! fdsink fd={write_fd}")
            self._proc = subprocess.Popen(
                ["uxplay", "-n", self.name, "-nh", "-a", "-reset", "0", "-vs", sink],
                pass_fds=(write_fd,))  # UxPlay logs to stdout: keep them in ours
            os.close(write_fd)
            log.info("AirPlay receiver '%s' started (pid %d)", self.name, self._proc.pid)
            self._read(read_fd)  # returns when UxPlay closes the pipe
            code = self._proc.wait()
            self.frame = None
            self._snapshot = None
            log.warning("AirPlay receiver exited (code %s), restarting", code)
            time.sleep(1)

    @property
    def live(self) -> bool:
        running = self._proc is not None and self._proc.poll() is None
        if not running or self.frame is None:
            return False
        clients = self._clients()
        if clients is not None:  # Linux: is a phone connected to UxPlay right now?
            return clients > 0
        snapshot = self.snapshot
        return snapshot is not None and time.monotonic() - snapshot.arrived < 90

    def _clients(self) -> int | None:
        """Established TCP connections of the UxPlay process (a mirroring phone keeps some open).

        Read from /proc, cached for a second; None where /proc is not available (e.g. macOS).
        """
        if time.monotonic() - self._clients_at < 1:
            return self._clients_n
        try:
            fds = os.listdir(f"/proc/{self._proc.pid}/fd")
            inodes = set()
            for fd in fds:
                link = os.readlink(f"/proc/{self._proc.pid}/fd/{fd}")
                if link.startswith("socket:["):
                    inodes.add(link[8:-1])
            n = 0
            for table in ("/proc/net/tcp", "/proc/net/tcp6"):
                with open(table) as f:
                    for row in f.readlines()[1:]:
                        cols = row.split()
                        n += cols[3] == "01" and cols[9] in inodes  # 01 = ESTABLISHED
        except OSError:
            n = None
        self._clients_at, self._clients_n = time.monotonic(), n
        return n

    @property
    def aspect(self) -> float:
        """Frame width / height, read from the JPEG header (portrait iPhone as a fallback)."""
        f = self.frame or b""
        i = f.find(b"\xff\xc0")  # baseline start-of-frame: ... height(2) width(2)
        if i < 0 or len(f) < i + 9:
            return 9 / 19.5
        return int.from_bytes(f[i + 7:i + 9], "big") / int.from_bytes(f[i + 5:i + 7], "big")

    async def settled(self, quiet: float = 0.35, least: float = 0.25, most: float = 4,
                      transport: bool = False) -> None:
        """Wait for quiet image content, not quiet transport: AirPlay may repeat identical frames."""
        if transport:  # conservative pointer fallback: drain queued video, preserving conservative timing
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

    @property
    def snapshot(self) -> FrameSnapshot | None:
        return self._snapshot

    async def fresh_snapshot(self, since: float, timeout: float = 0.8) -> FrameSnapshot | None:
        deadline = time.monotonic() + timeout
        while True:
            snapshot = self.snapshot
            if snapshot is not None and snapshot.arrived > since:
                return snapshot
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.02)

    def _on_jpeg(self, jpeg: bytes) -> None:
        snapshot = FrameSnapshot(jpeg, None, time.monotonic())
        self.frame, self.updated = jpeg, time.time()
        self._snapshot = snapshot

    def _read(self, fd: int) -> None:
        buf = b""
        with os.fdopen(fd, "rb", buffering=0) as pipe:
            while chunk := pipe.read(1 << 16):
                buf += chunk
                while (start := buf.find(b"\xff\xd8")) >= 0 and (end := buf.find(b"\xff\xd9", start)) >= 0:
                    self._on_jpeg(buf[start:end + 2])
                    buf = buf[end + 2:]
