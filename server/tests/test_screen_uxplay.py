"""UxPlay frames must obey the shared observation and receiver lifetime rules."""

import os

import pytest
from fake_clock import ManualClock

from hidagent.freshness import MAX_FRAME_AGE, fresh_frame, require_fresh
from hidagent.screen_uxplay import UxPlayScreen


class ReceiverProcess:
    pid = 123

    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self):
        self.returncode = 0
        return self.returncode

    def terminate(self):
        # A real child can remain alive while its termination is in progress.
        self.terminated = True


def test_uxplay_publishes_fresh_wall_timestamp_without_a_board_clock(monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)
    screen = UxPlayScreen()
    screen._proc = ReceiverProcess()
    monkeypatch.setattr(screen, "_clients", lambda: None)

    screen._on_jpeg(b"current JPEG")

    observed = require_fresh(screen)
    assert observed.frame == b"current JPEG"
    assert observed.updated == clock.time()
    assert observed.frame_ts is None
    assert screen.snapshot.arrived == clock.monotonic()
    assert fresh_frame(screen, since=observed.updated) is None

    clock.advance(MAX_FRAME_AGE + 1)
    assert screen.live  # A preview can still display the last frame.
    assert fresh_frame(screen) is None  # Reading the preview did not renew it.


def test_uxplay_stop_invalidates_cached_jpeg_and_rejects_late_jpeg(monkeypatch):
    screen = UxPlayScreen()
    screen._proc = ReceiverProcess()
    monkeypatch.setattr(screen, "_clients", lambda: None)
    screen._on_jpeg(b"current JPEG")
    assert screen.live

    screen.stop()
    assert screen._proc.terminated
    assert screen._proc.poll() is None
    screen._on_jpeg(b"late JPEG")

    assert not screen.live
    assert screen.frame is None
    assert fresh_frame(screen) is None


def test_uxplay_preview_holds_idle_jpeg_for_only_fifteen_seconds(monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)
    screen = UxPlayScreen()
    screen._proc = ReceiverProcess()
    monkeypatch.setattr(screen, "_clients", lambda: None)
    screen._on_jpeg(b"mirror JPEG")

    preview = screen.preview()
    assert (preview.state, preview.frame, preview.frame_age_s) == ("live", b"mirror JPEG", 0)
    assert preview.heartbeat_age_s is None
    assert preview.replay_lag_s is None

    clock.advance(6)
    preview = screen.preview()
    assert (preview.state, preview.frame, preview.frame_age_s) == ("idle", b"mirror JPEG", 6)
    clock.advance(10)
    preview = screen.preview()
    assert (preview.state, preview.frame, preview.frame_age_s) == ("waiting", None, 16)

    screen._on_jpeg(b"new JPEG")
    screen._proc.returncode = 0
    preview = screen.preview()
    assert (preview.state, preview.frame) == ("disconnected", None)


def test_native_stale_frame_is_dropped_and_fresh_frame_reports_its_capture_age(monkeypatch):
    from hidagent.uxplay_frames import BEGIN, FrameRecord

    clock = ManualClock()
    clock.install(monkeypatch)
    screen = UxPlayScreen()
    screen._proc = ReceiverProcess()
    monkeypatch.setattr(screen, "_clients", lambda: None)

    def now_ms():
        return int(clock.monotonic() * 1000) & 0xFFFFFFFF

    screen._on_native_record(FrameRecord(BEGIN, 1, 0, b""))
    # A frame that spent 5s in the decode queue must not be published as fresh.
    screen._on_native_record(FrameRecord(2, 1, 1, b"stale JPEG", (now_ms() - 5000) & 0xFFFFFFFF))
    assert screen.frame is None
    assert screen.snapshot is None
    assert screen.preview().replay_lag_s is None

    screen._on_native_record(FrameRecord(2, 1, 2, b"fresh JPEG", now_ms()))
    assert screen.frame == b"fresh JPEG"
    preview = screen.preview()
    assert preview.replay_lag_s is not None and preview.replay_lag_s < 0.1

    # A later stale frame must not evict the fresh one or renew its age.
    screen._on_native_record(FrameRecord(2, 1, 3, b"older JPEG", (now_ms() - 3000) & 0xFFFFFFFF))
    assert screen.frame == b"fresh JPEG"
    assert screen.preview().replay_lag_s < 0.1


def test_native_end_then_new_begin_recovers_with_a_new_generation(monkeypatch):
    from hidagent.uxplay_frames import BEGIN, END, FrameRecord

    clock = ManualClock()
    clock.install(monkeypatch)
    screen = UxPlayScreen()
    screen._proc = ReceiverProcess()
    monkeypatch.setattr(screen, "_clients", lambda: None)

    screen._on_native_record(FrameRecord(BEGIN, 1, 0, b""))
    screen._on_native_record(FrameRecord(2, 1, 1, b"first JPEG"))
    assert screen.frame == b"first JPEG"
    first_generation = screen.generation

    screen._on_native_record(FrameRecord(END, 1, 0, b""))
    assert screen.frame is None
    # Same-epoch JPEGs after END are terminally rejected; the screen must
    # not publish them either.
    screen._on_native_record(FrameRecord(2, 1, 2, b"late JPEG"))
    assert screen.frame is None

    screen._on_native_record(FrameRecord(BEGIN, 2, 0, b""))
    screen._on_native_record(FrameRecord(2, 2, 1, b"recovered JPEG"))
    assert screen.frame == b"recovered JPEG"
    assert screen.generation > first_generation


def test_uxplay_receiver_restart_invalidates_previous_observation(monkeypatch):
    screen = UxPlayScreen()
    observations = []
    between_receivers = []
    monkeypatch.setattr(screen, "_clients", lambda: None)
    monkeypatch.setattr("hidagent.screen_uxplay.subprocess.Popen", lambda *a, **kw: ReceiverProcess())
    monkeypatch.setattr("hidagent.screen_uxplay.time.sleep", lambda _: between_receivers.append(fresh_frame(screen)))

    def read_one_frame(fd):
        # Reuse identical pixels to ensure the generation is tied to the receiver.
        os.close(fd)
        screen._on_jpeg(b"same JPEG")
        observations.append(require_fresh(screen))
        if len(observations) == 2:
            screen.stop()

    monkeypatch.setattr(screen, "_read", read_one_frame)
    screen._supervise()

    assert observations[0].frame == observations[1].frame
    assert observations[1].generation > observations[0].generation
    assert between_receivers[0] is None
    with pytest.raises(RuntimeError, match="新鲜"):
        require_fresh(screen)
