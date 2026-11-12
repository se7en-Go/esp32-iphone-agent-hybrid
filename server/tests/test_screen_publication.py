"""Disconnect publication cannot race an already validated HID write."""

import asyncio
import threading

import pytest

from hidagent.device import Device
from hidagent.freshness import require_fresh
from hidagent.screen import Screen


class PublicationLock:
    """Signal a receiver's lock attempt without changing RLock semantics."""

    def __init__(self, begin, reached):
        self.lock = threading.RLock()
        self.owner = threading.get_ident()
        self.begin = begin
        self.reached = reached

    def __enter__(self):
        if self.begin.is_set() and threading.get_ident() != self.owner:
            self.reached.set()
        self.lock.acquire()
        return self

    def __exit__(self, *_args):
        self.lock.release()


class EndSupervisor(BaseException):
    pass


@pytest.mark.parametrize("transition", ["stop", "receiver_return", "receiver_error", "missing_endpoint"])
def test_disconnect_and_final_click_share_publication_lock(monkeypatch, transition):
    async def run():
        screen = Screen("fake-board", "fake-token")
        screen._started = True
        screen._on_ready()
        screen._on_jpeg(b"current frame")
        begin, reached = threading.Event(), threading.Event()
        screen._frame_lock = PublicationLock(begin, reached)

        def run_tcp(*_args, **_kwargs):
            assert begin.wait(1)
            if transition == "receiver_error":
                raise OSError("fake disconnect")

        def missing_endpoint():
            assert begin.wait(1)
            return "", ""

        def end_supervisor(_seconds):
            raise EndSupervisor

        monkeypatch.setattr("hidagent.screen.video7201.run_tcp", run_tcp)
        monkeypatch.setattr("hidagent.screen.video7201.H264Decoder", object)
        monkeypatch.setattr("hidagent.screen.time.sleep", end_supervisor)
        if transition == "missing_endpoint":
            monkeypatch.setattr(screen, "_endpoint", missing_endpoint)

        def disconnect():
            try:
                if transition == "stop":
                    assert begin.wait(1)
                    screen.stop()
                else:
                    screen._supervise()
            except EndSupervisor:
                pass
            finally:
                reached.set()

        worker = threading.Thread(target=disconnect)
        worker.start()
        live_at_write = []

        class Link:
            reply = b""

            def write(self, data):
                live_at_write.append(screen.live)
                self.reply = data.split(b" ", 1)[0] + b" ok\n"

            async def readline(self):
                return self.reply

            def is_open(self):
                return True

            def close(self):
                pass

        device = Device()
        device.attach(Link())

        def validate():
            require_fresh(screen)
            begin.set()
            # The receiver either attempted publication or completed its
            # unprotected transition. Neither scheduling nor sleep is assumed.
            assert reached.wait(1)

        try:
            await device.send_guarded("click", validate, write_lock=screen._frame_lock)
        finally:
            begin.set()
            worker.join(1)
            device.close()
        assert not worker.is_alive()
        assert live_at_write == [True]
        assert not screen.live

    asyncio.run(run())


def test_stop_before_start_rejects_a_cached_frame_and_late_ready():
    screen = Screen()
    screen._on_jpeg(b"current frame")
    assert screen.live
    screen.stop()
    assert not screen.live
    screen._on_ready()
    screen._on_jpeg(b"late frame")
    assert not screen.live
    assert not screen._session_ok


@pytest.mark.parametrize("transition", ["stop", "disconnect"])
def test_completed_disconnect_rejects_click_before_any_write(transition):
    async def run():
        screen = Screen()
        screen._started = True
        screen._on_ready()
        screen._on_jpeg(b"current frame")
        if transition == "stop":
            screen.stop()
        else:
            screen._on_disconnect()

        class Link:
            writes = []

            def write(self, data):
                self.writes.append(data)

            def is_open(self):
                return True

            def close(self):
                pass

        device, link = Device(), Link()
        device.attach(link)
        try:
            with pytest.raises(RuntimeError, match="新鲜"):
                await device.send_guarded("click", lambda: require_fresh(screen),
                                          write_lock=screen._frame_lock)
            assert not link.writes
        finally:
            device.close()

    asyncio.run(run())


def test_reconnect_does_not_publish_previous_sessions_pending_timestamp():
    screen = Screen()
    screen._on_ts(123)
    screen._on_ready()
    screen._on_jpeg(b"new session frame")
    assert screen.snapshot().frame_ts is None
    screen._on_ts(456)
    screen._on_jpeg(b"new timestamped frame")
    assert screen.snapshot().frame_ts == 456
