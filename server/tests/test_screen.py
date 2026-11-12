import asyncio
import io
import time

from PIL import Image

from hidagent.screen import Screen


def frame(color):
    buffer = io.BytesIO()
    Image.new("RGB", (100, 200), color).save(buffer, "JPEG")
    return buffer.getvalue()


def test_repeated_identical_video_frames_do_not_force_maximum_wait():
    async def scenario():
        screen = Screen()
        screen.frame = frame("white")

        async def duplicate_frames():
            while True:
                screen.updated = time.time()
                screen.frame = bytes(bytearray(screen.frame))
                await asyncio.sleep(.01)

        task = asyncio.create_task(duplicate_frames())
        try:
            started = time.monotonic()
            await screen.settled(quiet=.05, least=.01, most=.3)
            assert .04 <= time.monotonic() - started < .2
        finally:
            task.cancel()
    asyncio.run(scenario())


def test_real_content_change_restarts_quiet_window():
    async def scenario():
        screen = Screen()
        screen.frame = frame("white")

        async def change():
            await asyncio.sleep(.03)
            screen.frame = frame("black")
            screen.updated = time.time()

        task = asyncio.create_task(change())
        started = time.monotonic()
        await screen.settled(quiet=.06, least=.01, most=.4)
        assert time.monotonic() - started >= .10
        await task
    asyncio.run(scenario())


def test_animation_still_obeys_maximum_wait():
    async def scenario():
        screen = Screen()
        screen.frame = frame("white")

        async def animate():
            # A black/white toggle can land back on the frame last sampled.
            # Windows sleeps in ~15.6ms ticks, so the 40ms flip and the 50ms
            # poll often take an even number of flips between samples. settled
            # then treats the screen as quiet and returns at quiet (0.15s).
            # The 0.172s failure is that path, not a 0.2s maximum measured
            # short: the same monotonic clock cannot take the max path before
            # most. A new, obviously different image each step is still a
            # change no matter how many steps fall between samples.
            n = 0
            while True:
                n += 1
                screen.frame = frame(((n * 97) % 256, (n * 57) % 256, 255 - (n * 23) % 256))
                screen.updated = time.time()
                await asyncio.sleep(.02)

        task = asyncio.create_task(animate())
        try:
            started = time.monotonic()
            await screen.settled(quiet=.15, least=.01, most=.2)
            # Stay above the quiet window. 0.19 - 0.05 would accept the 0.172s return.
            assert .19 <= time.monotonic() - started < .4
        finally:
            task.cancel()
    asyncio.run(scenario())


def test_frame_snapshot_stays_paired_during_threaded_publication(monkeypatch):
    """The video thread must never expose a new timestamp with the preceding JPEG."""
    import threading
    from types import SimpleNamespace

    from hidagent import screen as screen_module

    screen = Screen()
    screen._on_ts(100)
    screen._on_jpeg(b"old")
    old = screen.snapshot
    publishing, resume = threading.Event(), threading.Event()

    def wall_time():
        # Pause after the legacy attributes changed but before the new snapshot is published.
        publishing.set()
        assert resume.wait(2)
        return time.time()

    monkeypatch.setattr(screen_module, "time", SimpleNamespace(time=wall_time, monotonic=lambda: old.arrived + 1))

    def publish():
        screen._on_ts(200)
        screen._on_jpeg(b"new")

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert publishing.wait(2)
        assert screen.frame == b"new" and screen.frame_ts == 200
        assert screen.snapshot is old
        assert (old.frame, old.board_ts) == (b"old", 100)
    finally:
        resume.set()
        thread.join(2)
    new = screen.snapshot
    assert (new.frame, new.board_ts, new.arrived) == (b"new", 200, old.arrived + 1)
    assert old.frame == b"old" and old.board_ts == 100


def test_snapshot_wait_and_liveness_survive_wall_clock_changes(monkeypatch):
    from types import SimpleNamespace

    from hidagent import screen as screen_module

    wall = [time.time()]
    monkeypatch.setattr(screen_module, "time", SimpleNamespace(time=lambda: wall[0], monotonic=time.monotonic))

    async def scenario():
        screen = Screen()
        screen._started = screen._session_ok = True
        screen._on_ts(100)
        screen._on_jpeg(b"old")
        old = screen.snapshot
        wall[0] += 3600
        assert screen.live
        started = time.monotonic()
        assert await screen.fresh_snapshot(old.arrived, .04) is None
        assert .03 <= time.monotonic() - started < .3

        wall[0] -= 7200
        screen._on_ts(200)
        screen._on_jpeg(b"new")
        new = await screen.fresh_snapshot(old.arrived, .04)
        assert (new.frame, new.board_ts) == (b"new", 200)
        assert new.arrived > old.arrived
        assert screen.live

    asyncio.run(scenario())


def test_still_screen_keeps_its_monotonic_arrival_and_stays_live_for_90_seconds(monkeypatch):
    from types import SimpleNamespace

    from hidagent import screen as screen_module

    now = [100.0]
    monkeypatch.setattr(screen_module, "time", SimpleNamespace(time=lambda: 1000.0, monotonic=lambda: now[0]))
    screen = Screen()
    screen._started = screen._session_ok = True
    screen._on_ts(50_000)
    screen._on_jpeg(b"still")
    snapshot = screen.snapshot
    now[0] += 60
    assert screen.live and screen.snapshot is snapshot
    assert snapshot.arrived == 100.0  # reading the still frame must not make it newly arrived
    now[0] += 30
    assert not screen.live


def test_legacy_fresh_timeout_uses_monotonic_clock(monkeypatch):
    from types import SimpleNamespace

    from hidagent import screen as screen_module

    # A frozen wall clock would make the original fresh() wait forever.
    monkeypatch.setattr(screen_module, "time", SimpleNamespace(time=lambda: 1000.0, monotonic=time.monotonic))

    async def scenario():
        screen = Screen()
        assert await asyncio.wait_for(screen.fresh(1000.0, .03), .3) is None

    asyncio.run(scenario())
