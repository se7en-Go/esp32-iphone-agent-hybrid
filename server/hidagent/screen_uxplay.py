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
from collections import deque
from pathlib import Path

from . import freshness, messages, vision
from .screen import PREVIEW_HOLD_S, FrameSnapshot, PreviewStatus, RecentFrameHistory
from .uxplay_frames import BEGIN, END, MAX_JPEG, FrameProtocolError, FrameStreamParser

log = logging.getLogger(__name__)
_REAL_SLEEP = time.sleep


class UxPlayScreen(RecentFrameHistory):
    """Runs UxPlay with a GStreamer sink that writes JPEGs into a pipe; keeps the latest one."""

    def __init__(self, name: str = "ESP32 to iPhone", fps: int = 8, quality: int = 75, *, socket_path=None):
        self.name, self.fps, self.quality = name, fps, quality
        self.socket_path = Path(socket_path) if socket_path else None
        self.frame: bytes | None = None
        self.updated = 0.0
        self._snapshot: FrameSnapshot | None = None
        self._frame_lock = threading.RLock()
        self._recent_frames = deque(maxlen=32)
        self.generation = 0
        self._proc = None
        self._clients_at, self._clients_n = 0.0, None
        self._stopping = False
        self._ipc_connected = False
        self._ipc_live = False
        self._ipc_received = 0.0
        self._ipc_source = None
        self._stop_event = threading.Event()
        mode = os.environ.get("HIDAGENT_UXPLAY_FRAMED", "0")
        if mode not in {"0", "1"}:
            raise ValueError("HIDAGENT_UXPLAY_FRAMED must be 0 (stock) or 1 (native framed)")
        self._framed = mode == "1"
        self._native_epoch = 0
        self._native_active = False
        self._session_received_frame = False
        # Capture age (seconds) of the most recent accepted native frame,
        # from its HAFE v2 timestamp; None when the frame carried no timestamp.
        self._last_capture_age = None
        self.last_error = ""

    def start(self) -> None:
        if sys.platform == "win32":
            raise RuntimeError(messages.UXPLAY_WINDOWS)
        if self._framed and not self.socket_path:
            self._check_native_protocol()
        target = self._subscribe if self.socket_path else self._supervise
        threading.Thread(target=target, daemon=True).start()

    def _check_native_protocol(self) -> None:
        env = os.environ.copy()
        env.pop("HIDAGENT_UXPLAY_FRAMED_FD", None)
        try:
            result = subprocess.run(["uxplay", "--hafe-protocol"], env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=2, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Native UxPlay HAFE support could not be verified") from exc
        if result.returncode != 0 or result.stdout.strip() not in (b"HAFE/1", b"HAFE/2"):
            raise RuntimeError("Selected UxPlay does not support native HAFE; framed mode was not started")

    def stop(self) -> None:
        # A guarded HID write holds this same lock until it finishes validating.
        with self._frame_lock:
            self._stopping = True
            self._stop_event.set()
            self._ipc_connected = False
            self.frame, self.updated, self._snapshot = None, 0.0, None
            self._last_capture_age = None
            if self._proc is not None:
                self._proc.terminate()

    def _subscribe(self) -> None:
        from .airplay_receiver import read_snapshot

        while not self._stopping:
            try:
                metadata, jpeg = read_snapshot(self.socket_path)
                self._accept_remote(metadata, jpeg)
            except (OSError, ValueError):
                self._remote_disconnected()
            self._stop_event.wait(1 / max(1, self.fps))

    def _remote_disconnected(self) -> None:
        with self._frame_lock:
            if self._ipc_connected:
                self.generation += 1
            self._ipc_connected = self._ipc_live = False
            self.frame, self.updated, self._snapshot = None, 0.0, None
            self._last_capture_age = None
            self.last_error = "Mac 镜像接收器连接中，请检查本地 receiver 服务。"

    def _accept_remote(self, metadata, jpeg) -> None:
        source = metadata["instance"], metadata["generation"]
        live = metadata["running"] and metadata["live"]
        with self._frame_lock:
            if self._stopping:
                return
            # A sender can disconnect while the receiver process stays up. Once
            # that boundary is observed, old action tickets must remain invalid
            # even if the receiver publishes identical pixels on reconnect.
            if (not self._ipc_connected or source != self._ipc_source
                    or (self._ipc_live and not live)):
                self.generation += 1
                self.frame, self.updated, self._snapshot = None, 0.0, None
                self._last_capture_age = None
            self._ipc_source = source
            self._ipc_connected, self._ipc_live = True, live
            self._ipc_received = time.monotonic()
            self.last_error = ""
            if not live:
                self.frame, self.updated, self._snapshot = None, 0.0, None
                self._last_capture_age = None
                return
            if jpeg:
                # Keep receiver publication clocks. Polling an unchanged snapshot must
                # never make old pixels eligible for a new input operation.
                # The receiver stamps the capture age at publication; add the
                # IPC transit since then, so `updated - capture_age_s` stays
                # the capture wall time (never later than the true capture).
                age = metadata.get("capture_age_s")
                if type(age) in (int, float) and age >= 0:
                    age = age + max(0.0, time.monotonic() - metadata["arrived"])
                else:
                    age = None
                self.frame, self.updated = jpeg, metadata["updated"]
                self._snapshot = FrameSnapshot(jpeg, None, metadata["arrived"], self.updated,
                                               self.generation, True, capture_age_s=age)
                self._remember_snapshot(self._snapshot)
                self._last_capture_age = age

    def _wait_retry(self, delay: float) -> None:
        if time.sleep is not _REAL_SLEEP:
            time.sleep(delay)
            return
        self._stop_event.wait(delay)

    def _supervise(self) -> None:
        """Keep UxPlay running: it exits on some disconnects, so restart it every time."""
        failures = 0
        while True:
            spawn_error = False
            with self._frame_lock:
                if self._stopping:
                    return
                read_fd, write_fd = os.pipe()
                # With UxPlay video sync disabled, frames may have no timestamps.
                # Negotiate the frame rate with the sender so the sink can accept those frames.
                sink = f"jpegenc quality={self.quality} ! fdsink fd={write_fd}"
                command = ["uxplay", "-n", self.name, "-nh", "-a", "-reset", "0", "-fps", str(self.fps)]
                env = os.environ.copy()
                env.pop("HIDAGENT_UXPLAY_FRAMED_FD", None)
                if self._framed:
                    env["HIDAGENT_UXPLAY_FRAMED_FD"] = str(write_fd)
                else:
                    command += ["-vs", sink]
                try:
                    proc = subprocess.Popen(
                        command, env=env, pass_fds=(write_fd,))  # logs remain separate from the frame pipe
                except OSError as exc:
                    os.close(read_fd)
                    self.last_error = f"AirPlay receiver failed to start: {exc}"
                    log.warning("%s", self.last_error)
                    if self._stopping:
                        return
                    spawn_error = True
                finally:
                    os.close(write_fd)
                if not spawn_error:
                    self._proc = proc
                    self.generation += 1
                    self._native_epoch, self._native_active = 0, False
                    self._session_received_frame = False
                    self._clients_at, self._clients_n = 0.0, None
            if spawn_error:
                failures += 1
                delay = min(16.0, float(1 << min(failures - 1, 4)))
                self._wait_retry(delay)
                continue
            log.info("AirPlay receiver '%s' started (pid %d)", self.name, proc.pid)
            read_failed = False
            try:
                self._read(read_fd)  # returns when UxPlay closes the pipe
            except (OSError, FrameProtocolError) as exc:
                read_failed = True
                if self._framed:
                    log.warning("AirPlay frame stream rejected: %s", exc)
                else:
                    self.last_error = f"AirPlay frame stream error: {exc}"
                    log.warning("%s", self.last_error)
            with self._frame_lock:
                received_frame = self._session_received_frame or self._snapshot is not None
                self.frame, self.updated, self._snapshot = None, 0.0, None
            if self._framed:
                try:
                    code = self._reap_native(proc)
                except (OSError, subprocess.TimeoutExpired):
                    self.last_error = "Native UxPlay did not stop after its frame stream closed."
                    log.error(self.last_error)
                    return  # Never start a second owner while the failed child may still exist.
            else:
                try:
                    code = self._reap_legacy(proc)
                except (OSError, subprocess.TimeoutExpired):
                    self.last_error = "UxPlay did not stop after its frame stream closed."
                    log.error(self.last_error)
                    return  # Never start a second owner while the failed child may still exist.
            if self._stopping:
                return
            if received_frame and not read_failed:
                failures = 0
                self.last_error = ""
            else:
                failures += 1
            delay = 1 if failures <= 1 else min(16, 1 << min(failures - 1, 4))
            log.warning("AirPlay receiver exited (code %s), restarting in %ss", code, delay)
            self._wait_retry(delay)

    @staticmethod
    def _reap_native(proc):
        # Even a clean EOF can leave the RAOP server alive after its writer
        # failed. Reap it before restarting; do not wait forever on a closed pipe.
        try:
            if proc.poll() is None:
                proc.terminate()
        except ProcessLookupError:
            pass
        try:
            return proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return proc.wait(timeout=2)

    @staticmethod
    def _reap_legacy(proc):
        try:
            if proc.poll() is None:
                proc.terminate()
        except ProcessLookupError:
            pass
        try:
            return proc.wait(timeout=2)
        except TypeError:
            return proc.wait()
        except subprocess.TimeoutExpired:
            kill = getattr(proc, "kill", None)
            if kill is not None:
                try:
                    kill()
                except ProcessLookupError:
                    pass
            return proc.wait(timeout=2)

    @property
    def live(self) -> bool:
        with self._frame_lock:
            if self.socket_path:
                return (not self._stopping and self._ipc_connected and self._ipc_live
                        and self._snapshot is not None
                        and time.monotonic() - self._ipc_received < 2
                        and time.monotonic() - self._snapshot.arrived < 90)
            running = self._proc is not None and self._proc.poll() is None
            if self._stopping or not running or self._snapshot is None:
                return False
            if self._framed:
                return self._native_active and time.monotonic() - self._snapshot.arrived < 90
            clients = self._clients()
            if clients is not None:  # Linux: is a phone connected to UxPlay right now?
                return clients > 0
            return time.monotonic() - self._snapshot.arrived < 90

    def preview(self) -> PreviewStatus:
        with self._frame_lock:
            snapshot = self._snapshot
            age = max(0.0, time.monotonic() - snapshot.arrived) if snapshot and snapshot.frame else None
            details = (age, self._last_capture_age, None, self.generation)
            if self._stopping:
                return PreviewStatus("disconnected", *details)
            if self.socket_path:
                if not self._ipc_connected or time.monotonic() - self._ipc_received >= 2:
                    return PreviewStatus("disconnected", *details)
                if not self._ipc_live or age is None or age > PREVIEW_HOLD_S:
                    return PreviewStatus("waiting", *details)
                state = "live" if age <= freshness.MAX_FRAME_AGE else "idle"
                return PreviewStatus(state, *details, snapshot.frame)
            if self._proc is None:
                return PreviewStatus("connecting", *details)
            if self._proc.poll() is not None:
                return PreviewStatus("disconnected", *details)
            if self._framed:
                if not self._native_active:
                    return PreviewStatus("disconnected" if self._native_epoch else "waiting", *details)
            elif self._clients() == 0:
                return PreviewStatus("disconnected", *details)
            if age is None or age > PREVIEW_HOLD_S:
                return PreviewStatus("waiting", *details)
            state = "live" if age <= freshness.MAX_FRAME_AGE else "idle"
            return PreviewStatus(state, *details, snapshot.frame)

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
        arrived, updated = time.monotonic(), time.time()
        with self._frame_lock:
            if self._framed or self._stopping or self._proc is None or self._proc.poll() is not None:
                return
            self.frame, self.updated = jpeg, updated
            self._snapshot = FrameSnapshot(jpeg, None, arrived, updated, self.generation, True)
            self._session_received_frame = True
            self._remember_snapshot(self._snapshot)

    def _read(self, fd: int) -> None:
        if self._framed:
            self._read_framed(fd)
        else:
            self._read_legacy(fd)

    def _native_boundary(self):
        self.generation += 1
        self.frame, self.updated, self._snapshot = None, 0.0, None
        self._last_capture_age = None
        self._recent_frames.clear()

    def _on_native_record(self, record):
        with self._frame_lock:
            if self._stopping or self._proc is None or self._proc.poll() is not None:
                return
            if record.kind == BEGIN:
                self._native_boundary()
                self._native_epoch, self._native_active = record.epoch, True
            elif record.epoch == self._native_epoch:
                if record.kind == END:
                    self._native_boundary()
                    self._native_active = False
                elif self._native_active:
                    # HAFE v2: the record carries its capture time (monotonic ms
                    # at push). A frame that waited in the decode queue is old
                    # even though it arrived just now; drop it instead of
                    # publishing it as fresh. 0 / absent = no timestamp.
                    capture_age = None
                    if record.capture_ms:
                        now_ms = int(time.monotonic() * 1000) & 0xFFFFFFFF
                        capture_age = freshness.board_delta(now_ms, record.capture_ms) / 1000
                        if capture_age > freshness.MAX_REPLAY_LAG or capture_age < -0.5:
                            log.warning("Dropping stale native frame (capture age %.2fs)", capture_age)
                            return
                    self._last_capture_age = capture_age
                    arrived, updated = time.monotonic(), time.time()
                    self.frame, self.updated = record.payload, updated
                    self._snapshot = FrameSnapshot(record.payload, None, arrived, updated, self.generation,
                                                   True, capture_age_s=capture_age)
                    self._session_received_frame = True
                    self._remember_snapshot(self._snapshot)

    def _read_framed(self, fd):
        parser = FrameStreamParser()
        try:
            with os.fdopen(fd, "rb", buffering=0) as pipe:
                while chunk := pipe.read(1 << 16):
                    records = parser.feed(chunk)
                    # A complete accepted END/BEGIN in this batch already retires
                    # preceding JPEGs. Do not expose those with a renewed clock.
                    with self._frame_lock:
                        for record in records:
                            self._on_native_record(record)
                parser.finish()
        finally:
            with self._frame_lock:
                self._native_boundary()
                self._native_active = False

    def _read_legacy(self, fd: int) -> None:
        buf = b""
        with os.fdopen(fd, "rb", buffering=0) as pipe:
            while chunk := pipe.read(1 << 16):
                buf += chunk
                while (start := buf.find(b"\xff\xd8")) >= 0 and (end := buf.find(b"\xff\xd9", start)) >= 0:
                    frame = buf[start:end + 2]
                    buf = buf[end + 2:]
                    if len(frame) > MAX_JPEG:
                        log.warning("AirPlay legacy JPEG exceeded %d bytes (%d); dropping frame",
                                    MAX_JPEG, len(frame))
                        continue
                    self._on_jpeg(frame)
                if len(buf) > MAX_JPEG:
                    last_start = buf.rfind(b"\xff\xd8")
                    if last_start >= 0 and len(buf) - last_start <= MAX_JPEG:
                        buf = buf[last_start:]
                    else:
                        log.warning("AirPlay legacy stream exceeded %d bytes without a complete JPEG; dropping buffer",
                                    MAX_JPEG)
                        buf = b""
