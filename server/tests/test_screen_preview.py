"""The board preview distinguishes frame age from the health of its TCP session."""

from types import SimpleNamespace

import pytest

from hidagent import screen as screen_module
from hidagent.screen import Screen


@pytest.fixture
def mirror(monkeypatch):
    clock = [200.0]
    monkeypatch.setattr(screen_module.time, "monotonic", lambda: clock[0])
    device = SimpleNamespace(board_clock_sample=(160_000, 199.9, 200.0))
    screen = Screen()
    screen.bind(device)
    screen._started = True
    screen._on_ready()
    return screen, device, clock


def publish(screen, board_ts, pixels):
    screen._on_ts(board_ts)
    screen._on_jpeg(pixels)


def test_recently_received_replay_is_never_shown_as_live(mirror):
    screen, _device, _clock = mirror
    publish(screen, 40_000, b"old GOP")
    preview = screen.preview()
    assert (preview.state, preview.frame, preview.frame_age_s) == ("replaying", None, 0)
    assert preview.replay_lag_s == pytest.approx(120.05)
    assert screen.live  # action freshness is still a separate check

    publish(screen, 160_000, b"new image")
    preview = screen.preview()
    assert (preview.state, preview.frame) == ("live", b"new image")


def test_classified_replay_stays_hidden_when_clock_sample_disappears(mirror):
    screen, device, _clock = mirror
    publish(screen, 40_000, b"old GOP")
    device.board_clock_sample = None
    assert (screen.preview().state, screen.preview().frame) == ("replaying", None)


def test_heartbeat_only_keeps_short_idle_preview_without_renewing_frame(mirror):
    screen, device, clock = mirror
    publish(screen, 160_000, b"still page")
    clock[0] = 207.0
    device.board_clock_sample = (167_000, 206.9, 207.0)
    screen._on_heartbeat(clock[0])
    preview = screen.preview()
    assert (preview.state, preview.frame, preview.frame_age_s) == ("idle", b"still page", 7)
    assert preview.heartbeat_age_s == 0

    clock[0] = 216.0
    device.board_clock_sample = (176_000, 215.9, 216.0)
    screen._on_heartbeat(clock[0])
    preview = screen.preview()
    assert (preview.state, preview.frame, preview.frame_age_s) == ("waiting", None, 16)
    assert preview.heartbeat_age_s == 0


def test_disconnect_and_new_session_do_not_show_previous_frame(mirror):
    screen, _device, clock = mirror
    publish(screen, 160_000, b"prior session")
    generation = screen.preview().generation
    screen._on_disconnect()
    assert screen.preview().state == "disconnected"
    assert screen.preview().frame is None

    clock[0] += 1
    screen._on_ready()
    preview = screen.preview()
    assert (preview.state, preview.frame, preview.frame_age_s) == ("waiting", None, None)
    assert preview.generation == generation + 1


def test_replaced_board_does_not_use_previous_boards_clock(monkeypatch, mirror):
    screen, device, clock = mirror
    device.ip = "board-a"
    source = ["fake-token-a"]
    previews = []
    monkeypatch.setattr(screen, "_endpoint", lambda: (device.ip, source[0]))
    monkeypatch.setattr(screen_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(screen_module.video7201, "H264Decoder", object)

    def connect(host, _port, _token, _emit, _stop, *, on_ready, **_kwargs):
        on_ready()
        previews.append(screen.preview())
        if host == "board-a":
            clock[0] = 201.0
            device.board_clock_sample = (161_000, 200.9, 201.0)
            publish(screen, 161_000, b"board A")
            previews.append(screen.preview())
            device.ip, source[0] = "board-b", "fake-token-b"
            clock[0] = 203.0
        else:
            # The old sample would predict the right number by chance at this monotonic time.
            publish(screen, 163_000, b"board B")
            previews.append(screen.preview())
            device.board_clock_sample = (163_000, 203.0, 203.0)
            previews.append(screen.preview())
            screen._stopping = True

    monkeypatch.setattr(screen_module.video7201, "run_tcp", connect)
    screen._supervise()
    assert [(p.state, p.frame) for p in previews] == [
        ("waiting", None), ("live", b"board A"),
        ("waiting", None), ("unverified", None), ("live", b"board B"),
    ]
    assert screen.generation == 3


def test_no_heartbeat_and_no_frames_marks_transport_stalled(mirror):
    screen, _device, clock = mirror
    publish(screen, 160_000, b"last frame")
    clock[0] += 13
    preview = screen.preview()
    assert (preview.state, preview.frame, preview.heartbeat_age_s) == ("stalled", None, 13)


def test_unknown_clock_does_not_mark_image_live(mirror):
    screen, device, clock = mirror
    del device.board_clock_sample
    publish(screen, 160_000, b"clock unknown")
    preview = screen.preview()
    assert (preview.state, preview.frame, preview.replay_lag_s) == ("unverified", None, None)

    clock[0] += 16
    screen._on_heartbeat(clock[0])
    assert (screen.preview().state, screen.preview().frame) == ("waiting", None)
