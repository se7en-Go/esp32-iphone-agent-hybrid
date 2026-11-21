"""Receiver/worker isolation and original-frame clocks over the actual Unix protocol."""

import asyncio
import io
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

# The persistent receiver uses Unix locks and sockets; Windows keeps its native board backend.
pytest.importorskip("fcntl", reason="persistent AirPlay receiver is Unix-only")

from hidagent.airplay_receiver import HEADER, MAX_FRAME, ReceiverServer, read_snapshot
from hidagent.freshness import fresh_frame
from hidagent.screen import FrameSnapshot
from hidagent.screen_uxplay import UxPlayScreen


def jpeg():
    output = io.BytesIO()
    Image.new("RGB", (50, 108), "white").save(output, "JPEG")
    return output.getvalue()


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    owner = UxPlayScreen()
    process = SimpleNamespace(returncode=None, poll=lambda: process.returncode)
    owner._proc = process
    monkeypatch.setattr(owner, "_clients", lambda: 1)
    owner.generation = 7
    owner._on_jpeg(jpeg())
    socket_path = tmp_path / "airplay.sock"
    # pytest's macOS temp directory can exceed sockaddr_un's 103-byte limit.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="airplay-ipc-", dir="/tmp") as short:
        socket_path = Path(short) / "airplay.sock"
        with ReceiverServer(socket_path, owner) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            yield owner, process, server, socket_path
            server.shutdown()
            thread.join(timeout=2)


def test_two_workers_read_same_frame_without_renewing_it(receiver):
    owner, _, _, path = receiver
    old_wall, old_mono = time.time() - 8, time.monotonic() - 8
    owner.updated = old_wall
    owner._snapshot = FrameSnapshot(owner.frame, None, old_mono, old_wall, owner.generation, True)
    first = UxPlayScreen(socket_path=path)
    first._accept_remote(*read_snapshot(path))
    second = UxPlayScreen(socket_path=path)
    second._accept_remote(*read_snapshot(path))
    for screen in (first, second):
        assert screen.frame == owner.frame
        assert screen.updated == old_wall
        assert screen.snapshot.arrived == old_mono
        assert screen.live
        assert screen.preview().state == "idle"
        assert fresh_frame(screen) is None
        assert asyncio.run(screen.fresh(old_wall, timeout=.01)) is None


def test_worker_stop_keeps_receiver_and_next_worker_can_read(receiver):
    owner, process, _, path = receiver
    first = UxPlayScreen(socket_path=path)
    first._accept_remote(*read_snapshot(path))
    original = fresh_frame(first)
    assert original is not None
    first.stop()
    assert first.frame is None and not first.live
    assert process.returncode is None and not owner._stopping
    second = UxPlayScreen(socket_path=path)
    second._accept_remote(*read_snapshot(path))
    assert fresh_frame(second).updated == original.updated


def test_receiver_restart_and_ipc_disconnect_invalidate_identical_pixels(receiver):
    owner, process, server, path = receiver
    subscriber = UxPlayScreen(socket_path=path)
    subscriber._accept_remote(*read_snapshot(path))
    original = fresh_frame(subscriber)
    process.returncode = 0
    subscriber._accept_remote(*read_snapshot(path))
    assert not subscriber.live and subscriber.frame is None
    process.returncode = None
    owner.generation += 1
    owner._on_jpeg(jpeg())
    subscriber._accept_remote(*read_snapshot(path))
    assert fresh_frame(subscriber).generation > original.generation
    before_disconnect = subscriber.generation
    subscriber._remote_disconnected()
    assert subscriber.updated == 0 and subscriber.frame is None and not subscriber.live
    subscriber._accept_remote(*read_snapshot(path))
    assert subscriber.generation > before_disconnect
    before_restart = subscriber.generation
    server.instance = "b" * 32
    subscriber._accept_remote(*read_snapshot(path))
    assert subscriber.generation > before_restart


def test_cached_ipc_snapshot_expires_when_daemon_stops_responding(receiver):
    _, _, _, path = receiver
    subscriber = UxPlayScreen(socket_path=path)
    subscriber._accept_remote(*read_snapshot(path))
    subscriber._ipc_received -= 3
    assert fresh_frame(subscriber) is None
    assert subscriber.preview().frame is None
    assert subscriber.preview().state == "disconnected"


@pytest.mark.parametrize("field", ["live", "running"])
def test_receiver_not_live_invalidates_ticket_without_process_restart(receiver, field):
    from hidagent.control import Controls

    owner, _, _, path = receiver
    subscriber = UxPlayScreen(socket_path=path)
    subscriber._accept_remote(*read_snapshot(path))
    controls = Controls(None, subscriber)
    ticket = controls.observation()["screen_id"]
    original_generation = subscriber.generation
    metadata, _ = read_snapshot(path)
    subscriber._accept_remote({**metadata, field: False}, b"")
    assert subscriber.generation > original_generation
    assert subscriber.frame is None and subscriber.snapshot is None
    disconnected_generation = subscriber.generation
    subscriber._accept_remote({**metadata, field: False}, b"")
    assert subscriber.generation == disconnected_generation
    # Same daemon and sender generation, even identical pixels, is still a new context.
    owner._on_jpeg(jpeg())
    subscriber._accept_remote(*read_snapshot(path))
    assert fresh_frame(subscriber) is not None
    with pytest.raises(ValueError, match="screen changed"):
        controls.consume(ticket)


def test_subscription_loop_recovers_without_restarting_receiver(receiver, monkeypatch):
    owner, _, _, path = receiver
    subscriber = UxPlayScreen(socket_path=path)
    subscriber.start()
    deadline = time.monotonic() + 2
    while fresh_frame(subscriber) is None and time.monotonic() < deadline:
        time.sleep(.01)
    assert fresh_frame(subscriber) is not None
    generation = subscriber.generation
    # A temporarily inaccessible socket invalidates the worker without touching UxPlay.
    os.chmod(path, 0o666)
    deadline = time.monotonic() + 2
    while subscriber.frame is not None and time.monotonic() < deadline:
        time.sleep(.01)
    assert subscriber.frame is None and not subscriber.live
    os.chmod(path, 0o600)
    owner._on_jpeg(jpeg())
    deadline = time.monotonic() + 2
    while fresh_frame(subscriber) is None and time.monotonic() < deadline:
        time.sleep(.01)
    assert fresh_frame(subscriber).generation > generation
    subscriber.stop()
    assert not owner._stopping


def test_socket_rejects_other_user_permissions(receiver):
    _, _, _, path = receiver
    os.chmod(path, 0o666)
    with pytest.raises(ValueError, match="0600"):
        read_snapshot(path)
    os.chmod(path, 0o600)


def test_socket_rejects_oversized_frame_before_reading_body(tmp_path):
    import tempfile
    with tempfile.TemporaryDirectory(prefix="airplay-limit-", dir="/tmp") as short:
        path = Path(short) / "test.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            os.chmod(path, 0o600)
            listener.listen(1)

            def reply():
                connection, _ = listener.accept()
                with connection:
                    connection.recv(1)
                    connection.sendall(HEADER.pack(10, MAX_FRAME + 1))

            thread = threading.Thread(target=reply, daemon=True)
            thread.start()
            with pytest.raises(ValueError, match="limit"):
                read_snapshot(path)
            thread.join(timeout=2)


def test_serve_unlinks_socket_even_if_killed_child_wait_times_out(monkeypatch):
    import subprocess
    import tempfile

    from hidagent import airplay_receiver

    with tempfile.TemporaryDirectory(prefix="airplay-unlink-", dir="/tmp") as short:
        sock = Path(short) / "airplay.sock"

        class StuckChild:
            killed = False

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("uxplay", timeout)

            def kill(self):
                self.killed = True

        child = StuckChild()

        class FakeScreen:
            def __init__(self, _name):
                self._proc = child

            def start(self):
                pass

            def stop(self):
                pass

        class FakeServer:
            def __init__(self, path, _screen, instance=None):
                self.path = path

            def __enter__(self):
                self.path.write_text("sock")
                return self

            def __exit__(self, *_args):
                pass

            def serve_forever(self, poll_interval=0.1):
                return None

        monkeypatch.setattr(airplay_receiver, "UxPlayScreen", FakeScreen)
        monkeypatch.setattr(airplay_receiver, "ReceiverServer", FakeServer)
        monkeypatch.setattr(airplay_receiver.signal, "signal", lambda *_a: None)
        airplay_receiver.serve(sock, "Test")
        assert child.killed is True
        assert not sock.exists()
