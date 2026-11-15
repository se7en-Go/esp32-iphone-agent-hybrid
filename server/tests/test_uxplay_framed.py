"""Native session boundaries invalidate identical pixels across the existing IPC."""

import os
import sys
import threading
from pathlib import Path

import pytest
from fake_clock import ManualClock
from test_screen_uxplay import ReceiverProcess as LegacyReceiverProcess
from test_uxplay_frames import JPEG, record

from hidagent.control import Controls
from hidagent.freshness import fresh_frame, require_fresh
from hidagent.screen_uxplay import UxPlayScreen
from hidagent.uxplay_frames import FrameProtocolError, FrameStreamParser


class ReceiverProcess(LegacyReceiverProcess):
    def wait(self, timeout=None):
        return super().wait()


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setenv("HIDAGENT_UXPLAY_FRAMED", "1")
    clock = ManualClock()
    clock.install(monkeypatch)
    screen = UxPlayScreen()
    screen._proc = ReceiverProcess()
    parser = FrameStreamParser()

    def feed(wire):
        for item in parser.feed(wire):
            screen._on_native_record(item)

    return screen, clock, feed


def test_begin_and_end_clear_frames_under_original_publication_lock(native):
    screen, _, feed = native
    feed(record(1) + record(2, sequence=1, payload=JPEG))
    original = require_fresh(screen)
    assert screen.live
    feed(record(3))
    assert screen.generation > original.generation
    assert not screen.live and screen.frame is None and screen.updated == 0
    assert screen.snapshot is None and screen.recent_frames == ()
    feed(record(1, epoch=2))
    assert not screen.live and screen.frame is None
    feed(record(2, epoch=2, sequence=1, payload=JPEG))
    assert require_fresh(screen).generation > original.generation


def test_retired_callbacks_do_not_republish_or_renew_native_frame(native):
    screen, clock, feed = native
    feed(record(1) + record(3) + record(1, epoch=2) + record(2, epoch=2, sequence=1, payload=JPEG))
    observed = screen.snapshot
    clock.advance(1)
    feed(record(1) + record(2, sequence=500, payload=JPEG) + record(3))
    assert screen.snapshot is observed
    assert screen.updated == observed.updated and screen.generation == observed.generation


def test_framed_mode_rejects_direct_legacy_jpeg_publication(native):
    screen, _, _ = native
    screen._on_jpeg(JPEG)
    assert screen.frame is None and not screen.live


@pytest.mark.parametrize("tail", [b"raw JPEG bytes", record(2, sequence=1, payload=JPEG)[:-1], b""])
def test_wire_failure_or_eof_immediately_invalidates_cached_frame(native, tail):
    screen, _, feed = native
    feed(record(1) + record(2, sequence=1, payload=JPEG))
    generation = screen.generation
    read_fd, write_fd = os.pipe()
    os.write(write_fd, tail)
    os.close(write_fd)
    if tail:
        with pytest.raises(FrameProtocolError):
            screen._read(read_fd)
    else:
        screen._read(read_fd)
    assert screen.generation > generation and screen.frame is None and not screen.live
    assert fresh_frame(screen) is None


def test_end_blocks_while_guarded_write_holds_frame_lock(native):
    screen, _, feed = native
    feed(record(1) + record(2, sequence=1, payload=JPEG))
    done = threading.Event()

    def ending():
        feed(record(3))
        done.set()

    with screen._frame_lock:
        thread = threading.Thread(target=ending)
        thread.start()
        assert not done.wait(.05)
        assert screen.frame == JPEG
    thread.join(timeout=1)
    assert done.is_set() and screen.frame is None


def test_quick_reconnect_between_worker_polls_invalidates_original_ticket(native, tmp_path):
    # Reuse the real IPC serialization and consumer. No daemon restart and no
    # intermediate empty snapshot need be observed by the worker.
    from test_airplay_receiver import jpeg

    from hidagent.airplay_receiver import ReceiverServer

    screen, clock, feed = native
    pixels = jpeg()
    feed(record(1) + record(2, sequence=1, payload=pixels))
    import tempfile
    with tempfile.TemporaryDirectory(prefix="framed-ipc-", dir="/tmp") as short:
        with ReceiverServer(Path(short) / "airplay.sock", screen) as server:
            worker = UxPlayScreen(socket_path=Path(short) / "airplay.sock")
            worker._accept_remote(*server.snapshot())
            controls = Controls(None, worker)
            ticket = controls.observation()["screen_id"]
            old = worker.snapshot
            clock.advance(.2)
            feed(record(3) + record(1, epoch=2) + record(2, epoch=2, sequence=1, payload=pixels))
            worker._accept_remote(*server.snapshot())
            assert worker.snapshot.frame == old.frame
            assert worker.generation > old.generation
            assert worker.updated == screen.updated
            with pytest.raises(ValueError, match="screen changed"):
                controls.consume(ticket)


@pytest.mark.parametrize("mode", ["", "true", "2", "framed-v1"])
def test_unknown_activation_does_not_fall_back_to_stock(monkeypatch, mode):
    monkeypatch.setenv("HIDAGENT_UXPLAY_FRAMED", mode)
    with pytest.raises(ValueError, match="HIDAGENT_UXPLAY_FRAMED"):
        UxPlayScreen()


@pytest.mark.parametrize("framed", [False, True])
def test_launch_uses_only_selected_transport_and_sanitizes_inherited_fd(monkeypatch, framed):
    monkeypatch.setenv("HIDAGENT_UXPLAY_FRAMED", "1" if framed else "0")
    monkeypatch.setenv("HIDAGENT_UXPLAY_FRAMED_FD", "12345")
    launched = []
    screen = UxPlayScreen()

    def spawn(command, **kwargs):
        launched.append((command, kwargs))
        return ReceiverProcess()

    def no_real_reader(fd):
        os.close(fd)
        screen.stop()

    monkeypatch.setattr("hidagent.screen_uxplay.subprocess.Popen", spawn)
    monkeypatch.setattr(screen, "_read", no_real_reader)
    screen._supervise()
    command, options = launched[0]
    assert ("-vs" in command) is not framed
    if framed:
        assert options["env"]["HIDAGENT_UXPLAY_FRAMED_FD"] == str(options["pass_fds"][0])
    else:
        assert "HIDAGENT_UXPLAY_FRAMED_FD" not in options["env"]
        assert "jpegenc" in command[-1] and "fdsink" in command[-1]


def test_stop_rejects_later_native_records(native):
    screen, _, feed = native
    feed(record(1) + record(2, sequence=1, payload=JPEG))
    screen.stop()
    feed(record(3) + record(1, epoch=2) + record(2, epoch=2, sequence=1, payload=JPEG))
    assert screen.snapshot is None and not screen.live


def test_framed_preview_uses_protocol_session_not_tcp_count(native, monkeypatch):
    screen, _, feed = native
    monkeypatch.setattr(screen, "_clients", lambda: pytest.fail("framed mode must use native events"))
    feed(record(1) + record(2, sequence=1, payload=JPEG))
    assert screen.preview().state == "live"
    feed(record(3))
    assert screen.preview().state == "disconnected"
    screen._native_epoch = 0
    screen._proc.returncode = 1
    assert screen.preview().state == "disconnected"


@pytest.mark.parametrize("outcome", [b"", b"UxPlay stock\n", b"HAFE/2\n", "timeout"])
@pytest.mark.skipif(sys.platform == "win32", reason="native UxPlay owner is POSIX-only")
def test_framed_owner_refuses_unsupported_binary_before_starting_receiver(monkeypatch, outcome):
    import subprocess
    from types import SimpleNamespace

    monkeypatch.setenv("HIDAGENT_UXPLAY_FRAMED", "1")
    monkeypatch.setenv("HIDAGENT_UXPLAY_FRAMED_FD", "12345")
    screen = UxPlayScreen()
    calls = []

    def probe(command, **options):
        calls.append(command)
        assert command == ["uxplay", "--hafe-protocol"]
        assert options["timeout"] == 2
        assert "HIDAGENT_UXPLAY_FRAMED_FD" not in options["env"]
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 2)
        return SimpleNamespace(returncode=0, stdout=outcome)

    monkeypatch.setattr("hidagent.screen_uxplay.subprocess.run", probe)
    monkeypatch.setattr("hidagent.screen_uxplay.threading.Thread", lambda **kw: pytest.fail("receiver started"))
    with pytest.raises(RuntimeError, match="HAFE/1"):
        screen.start()
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="native UxPlay owner is POSIX-only")
def test_exact_native_capability_allows_only_thread_start_without_network(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("HIDAGENT_UXPLAY_FRAMED", "1")
    monkeypatch.setattr("hidagent.screen_uxplay.subprocess.run",
                        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b"HAFE/1\n"))
    started = []
    monkeypatch.setattr("hidagent.screen_uxplay.threading.Thread",
                        lambda **kw: SimpleNamespace(start=lambda: started.append(kw["target"])))
    screen = UxPlayScreen()
    screen.start()
    assert started == [screen._supervise]


@pytest.mark.parametrize("ipc", [False, True])
@pytest.mark.skipif(sys.platform == "win32", reason="native UxPlay owner is POSIX-only")
def test_legacy_and_ipc_subscribers_do_not_probe_native_binary(monkeypatch, tmp_path, ipc):
    from types import SimpleNamespace

    monkeypatch.setenv("HIDAGENT_UXPLAY_FRAMED", "1" if ipc else "0")
    monkeypatch.setattr("hidagent.screen_uxplay.subprocess.run", lambda *a, **kw: pytest.fail("unexpected probe"))
    started = []
    monkeypatch.setattr("hidagent.screen_uxplay.threading.Thread",
                        lambda **kw: SimpleNamespace(start=lambda: started.append(kw["target"])))
    screen = UxPlayScreen(socket_path=tmp_path / "receiver.sock" if ipc else None)
    screen.start()
    assert started == [screen._subscribe if ipc else screen._supervise]


def test_malformed_stream_terminates_child_and_restarts_only_framed(monkeypatch):
    monkeypatch.setenv("HIDAGENT_UXPLAY_FRAMED", "1")
    screen = UxPlayScreen()
    children, commands, publications = [], [], []
    original = screen._on_native_record

    def capture(item):
        original(item)
        if item.kind == 2:
            publications.append(screen.snapshot)

    def spawn(command, **options):
        child = ReceiverProcess()
        children.append(child)
        commands.append(command)
        wire = record(1) + record(2, sequence=1, payload=JPEG)
        wire += b"bad" if len(children) == 1 else record(3)
        os.write(options["pass_fds"][0], wire)
        if len(children) == 2:
            original_wait = child.wait

            def finish(timeout=None):
                screen.stop()
                return original_wait(timeout=timeout)

            child.wait = finish
        return child

    monkeypatch.setattr(screen, "_on_native_record", capture)
    monkeypatch.setattr("hidagent.screen_uxplay.subprocess.Popen", spawn)
    monkeypatch.setattr("hidagent.screen_uxplay.time.sleep", lambda _: None)
    screen._supervise()
    assert len(children) == 2 and children[0].terminated
    assert all("-vs" not in command for command in commands)
    assert len(publications) == 2
    assert publications[1].generation > publications[0].generation
    assert screen.snapshot is None and not screen.live


@pytest.mark.parametrize("wire", [b"", record(1) + record(3)])
@pytest.mark.parametrize("shutdown", ["normal", "hung", "already_gone"])
def test_every_framed_eof_stops_live_child_with_bounded_reap(monkeypatch, wire, shutdown):
    import subprocess

    monkeypatch.setenv("HIDAGENT_UXPLAY_FRAMED", "1")
    screen = UxPlayScreen()

    class Child(ReceiverProcess):
        killed = False

        def terminate(self):
            self.terminated = True
            if shutdown == "already_gone":
                raise ProcessLookupError()

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            assert self.terminated, "closed frame pipe must stop a still-live native server"
            assert timeout == 2
            if shutdown == "hung" and not self.killed:
                raise subprocess.TimeoutExpired("uxplay", timeout)
            screen._stopping = True
            return 0

    child = Child()

    def spawn(command, **options):
        os.write(options["pass_fds"][0], wire)
        return child

    monkeypatch.setattr("hidagent.screen_uxplay.subprocess.Popen", spawn)
    screen._supervise()
    assert child.killed is (shutdown == "hung")
    assert screen.snapshot is None and not screen.live


def test_native_kill_process_lookup_race_is_reaped():
    import subprocess

    class GoneDuringKill(ReceiverProcess):
        waits = 0

        def wait(self, timeout=None):
            assert timeout == 2
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("uxplay", timeout)
            return -9

        def kill(self):
            raise ProcessLookupError()

    assert UxPlayScreen._reap_native(GoneDuringKill()) == -9


def test_unreaped_native_child_never_starts_a_second_owner(monkeypatch):
    import subprocess

    monkeypatch.setenv("HIDAGENT_UXPLAY_FRAMED", "1")
    screen = UxPlayScreen()
    spawned = []

    def spawn(*args, **options):
        spawned.append(ReceiverProcess())
        return spawned[-1]

    def fail_reap(proc):
        raise subprocess.TimeoutExpired("uxplay", 2)

    monkeypatch.setattr("hidagent.screen_uxplay.subprocess.Popen", spawn)
    monkeypatch.setattr(screen, "_reap_native", fail_reap)
    screen._supervise()
    assert len(spawned) == 1 and "did not stop" in screen.last_error
    assert not screen.live and screen.snapshot is None


def test_parsed_reconnect_batch_never_exposes_refreshed_retired_jpeg(native, monkeypatch):
    screen, _, _feed = native
    observed, exposed_mid_batch = [], []
    done = threading.Event()
    readers = []
    original = screen._on_native_record
    new_jpeg = b"\xff\xd8new-epoch\xff\xd9"

    def read_snapshot():
        # Match ReceiverServer.snapshot / actual HID admission publication lock.
        with screen._frame_lock:
            observed.append(screen.snapshot)
        done.set()

    def capture(item):
        original(item)
        if item.kind == 2 and item.epoch == 1:
            reader = threading.Thread(target=read_snapshot)
            readers.append(reader)
            reader.start()
            exposed_mid_batch.append(done.wait(.05))

    class Pipe:
        index = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            self.index += 1
            if self.index == 1:
                return (record(1) + record(2, sequence=1, payload=JPEG) + record(3)
                        + record(1, epoch=2) + record(2, epoch=2, sequence=1, payload=new_jpeg))
            if self.index == 2:
                assert done.wait(1), "subscriber must proceed after the accepted batch"
                return record(3, epoch=2)
            return b""

    monkeypatch.setattr(screen, "_on_native_record", capture)
    monkeypatch.setattr("hidagent.screen_uxplay.os.fdopen", lambda *a, **kw: Pipe())
    screen._read_framed(-1)
    for reader in readers:
        reader.join(timeout=1)
    assert exposed_mid_batch == [False]
    assert len(observed) == 1 and observed[0].frame == new_jpeg
    assert observed[0].generation == 3


@pytest.mark.skipif(sys.platform == "win32", reason="local runtime uses POSIX process ownership")
@pytest.mark.parametrize("from_file", [False, True])
def test_runtime_passes_framed_activation_without_an_additional_allowlist(tmp_path, from_file):
    from hidagent.local_runtime import runtime_environment

    source = {"HIDAGENT_DATA": str(tmp_path), "SCREEN_BACKEND": "uxplay"}
    if from_file:
        (tmp_path / "runtime.env").write_text("HIDAGENT_UXPLAY_FRAMED=1\n")
    else:
        source["HIDAGENT_UXPLAY_FRAMED"] = "1"
    env = runtime_environment(source)
    assert env["HIDAGENT_UXPLAY_FRAMED"] == "1"
    assert env["SCREEN_BACKEND"] == "uxplay"
