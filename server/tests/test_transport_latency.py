"""Transport-tail regressions using only in-process serial/socket fakes."""

import threading
import time

import pytest

from hidagent import video7201
from hidagent.device import SerialLink


class TimedSerial:
    """Model pyserial's size-or-timeout read with an already-arrived short ACK."""

    timeout = 0.2

    def __init__(self):
        self.data = bytearray(b"1 ok\n")

    @property
    def in_waiting(self):
        return len(self.data)

    def read(self, size):
        if not self.data:
            raise OSError("disconnected")
        if size > len(self.data):
            time.sleep(self.timeout)
        result = bytes(self.data[:size])
        del self.data[:size]
        return result


def test_short_ack_delivery_benchmark():
    samples = []
    for _ in range(5):
        link = SerialLink.__new__(SerialLink)
        link._closed = False
        link._buf = bytearray()
        link._ser = TimedSerial()
        received = []
        started = time.perf_counter()
        link._post = lambda line, out=received, start=started: out.append((line, time.perf_counter() - start))
        link._pump()
        assert [line for line, _ in received] == [b"1 ok\n", b""]
        samples.append(received[0][1] * 1000)
    print("short ACK delivery ms:", ", ".join(f"{value:.3f}" for value in samples))
    assert max(samples) < 100, "a short complete ACK must not wait for the 200 ms read timeout"


def test_serial_pump_preserves_partial_lines_and_reports_disconnect():
    class FragmentedSerial:
        def __init__(self):
            self.chunks = [bytearray(part) for part in (b"1 o", b"k\r\n2 ", b"ok\nunfinished")]
            self.sizes = []

        @property
        def in_waiting(self):
            # Simulate idle first-byte waits between fragmented arrivals.
            return 0

        def read(self, size):
            self.sizes.append(size)
            if not self.chunks:
                raise OSError("disconnected")
            result = bytes(self.chunks[0][:size])
            del self.chunks[0][:size]
            if not self.chunks[0]:
                self.chunks.pop(0)
            return result

    link = SerialLink.__new__(SerialLink)
    link._closed = False
    link._buf = bytearray()
    link._ser = FragmentedSerial()
    received = []
    link._post = received.append
    link._pump()
    assert received == [b"1 ok\r\n", b"2 ok\n", b""]
    assert all(size == 1 for size in link._ser.sizes)


def test_sparse_video_final_frame_meets_publication_deadline(monkeypatch):
    clock = [100.0]
    stop = threading.Event()
    published = []
    timestamps = []

    class Decoder:
        last_ts = None

        def push_chunk(self, record):
            self.last_ts = int(record)
            return [record]

    class Socket:
        timeout = 0.5
        reads = 0

        def setsockopt(self, *args):
            pass

        def settimeout(self, timeout):
            self.timeout = timeout

        def sendall(self, data):
            pass

        def recv(self, size):
            self.reads += 1
            if self.reads == 1:
                return b"\0\0\1\0" + b"1"
            if self.reads == 2:
                clock[0] += 0.01
                return b"\1\0" + b"2"
            clock[0] += self.timeout
            raise TimeoutError

        def close(self):
            pass

    def emit(data):
        published.append((data, clock[0]))
        if len(published) == 2:
            stop.set()

    monkeypatch.setattr(video7201.socket, "create_connection", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(video7201, "H264Decoder", Decoder)
    monkeypatch.setattr(video7201.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(video7201, "frame_to_jpeg", lambda frame, quality: frame)
    video7201.run_tcp("fake", 7201, "test-token", emit, stop, max_fps=8, on_ts=timestamps.append)
    assert timestamps == [1, 2]
    assert published[0] == (b"1", 100.0)
    assert published[1][0] == b"2"
    assert published[1][1] == pytest.approx(100.125)
