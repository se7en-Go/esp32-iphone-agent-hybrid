"""A process-owned AirPlay receiver with a local latest-frame Unix socket.

The API worker is a subscriber. Restarting it does not stop UxPlay or its pipe.
Snapshots carry their original clocks and generation, including when read repeatedly.
"""

import argparse
import fcntl
import json
import math
import os
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .screen_uxplay import UxPlayScreen

HEADER = struct.Struct("!II")
MAX_METADATA = 2048
MAX_FRAME = 8 * 1024 * 1024
PROTOCOL = 1


def _receive(connection, length, *, deadline=None):
    chunks = bytearray()
    while len(chunks) < length:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("receiver snapshot timed out")
            connection.settimeout(remaining)
        chunk = connection.recv(length - len(chunks))
        if not chunk:
            raise OSError("receiver closed snapshot connection")
        chunks.extend(chunk)
    return bytes(chunks)


def validate_snapshot(metadata, frame):
    if (not isinstance(metadata, dict) or metadata.get("protocol") != PROTOCOL
            or not isinstance(metadata.get("instance"), str) or len(metadata["instance"]) != 32
            or type(metadata.get("generation")) is not int or metadata["generation"] < 0
            or type(metadata.get("pid")) is not int or metadata["pid"] < 1
            or type(metadata.get("running")) is not bool or type(metadata.get("live")) is not bool):
        raise ValueError("invalid receiver snapshot")
    for name, now in (("updated", time.time()), ("arrived", time.monotonic())):
        value = metadata.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= now + .1:
            raise ValueError("invalid receiver clock")
    age = metadata.get("capture_age_s")
    if age is not None and (type(age) not in (int, float) or not math.isfinite(age) or age < 0):
        raise ValueError("invalid receiver capture age")
    if frame and (not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9")
                  or not metadata["updated"] or not metadata["arrived"]):
        raise ValueError("invalid receiver image")
    if metadata["live"] and not frame:
        raise ValueError("live receiver response has no image")


def read_snapshot(path, *, timeout=1.0):
    """Read one response, bounded by size/time and restricted to this user's socket."""
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("receiver socket must be owned by this user with mode 0600")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        deadline = time.monotonic() + timeout
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(b"F")
        meta_size, frame_size = HEADER.unpack(_receive(connection, HEADER.size, deadline=deadline))
        if not 1 <= meta_size <= MAX_METADATA or frame_size > MAX_FRAME:
            raise ValueError("receiver response exceeds limit")
        metadata = json.loads(_receive(connection, meta_size, deadline=deadline))
        frame = _receive(connection, frame_size, deadline=deadline)
        validate_snapshot(metadata, frame)
        return metadata, frame


class ReceiverServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, path, screen, instance=None):
        self.screen = screen
        self.instance = instance or uuid.uuid4().hex
        super().__init__(str(path), SnapshotHandler, bind_and_activate=False)
        self.server_bind()
        os.chmod(path, 0o600)
        self.server_activate()

    def snapshot(self):
        screen = self.screen
        with screen._frame_lock:
            snapshot = screen.snapshot
            running = (not screen._stopping and screen._proc is not None and screen._proc.poll() is None)
            live = screen.live
            frame = snapshot.frame if snapshot and live else b""
            metadata = {"protocol": PROTOCOL, "instance": self.instance, "pid": os.getpid(),
                        "generation": screen.generation, "running": running, "live": live,
                        "updated": snapshot.updated if snapshot else 0,
                        "arrived": snapshot.arrived if snapshot else 0,
                        "capture_age_s": screen._last_capture_age}
            return metadata, frame or b""


class SnapshotHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(1)
        try:
            if _receive(self.request, 1) != b"F":
                return
            metadata, frame = self.server.snapshot()
            encoded = json.dumps(metadata, separators=(",", ":")).encode()
            if len(frame) > MAX_FRAME:
                return
            self.request.sendall(HEADER.pack(len(encoded), len(frame)) + encoded + frame)
        except (OSError, ValueError):
            return


def serve(path, name, instance=None):
    path = Path(path).resolve()
    if len(os.fsencode(path)) > 103:
        raise RuntimeError("AirPlay socket path is too long; set AIRPLAY_SOCKET to a shorter local path")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Only the lock owner can remove a stale socket or start a receiver.
    with open(path.with_suffix(".lock"), "a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("AirPlay receiver is already running") from None
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("receiver path is occupied by a different file")
            path.unlink()
        screen = UxPlayScreen(name)
        with ReceiverServer(path, screen, instance=instance) as server:
            def stop(_signum, _frame):
                threading.Thread(target=server.shutdown, daemon=True).start()

            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            screen.start()
            try:
                server.serve_forever(poll_interval=.1)
            finally:
                screen.stop()
                process = screen._proc
                if process is not None:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            pass
                path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="持久 Mac AirPlay 接收器")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--name", default=os.environ.get("AIRPLAY_NAME", "ESP32 Agent Mac"))
    parser.add_argument("--instance", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.instance is not None and (len(args.instance) != 32
                                     or any(char not in "0123456789abcdef" for char in args.instance)):
        parser.error("invalid receiver instance")
    serve(args.socket, args.name, args.instance)


if __name__ == "__main__":
    main()
