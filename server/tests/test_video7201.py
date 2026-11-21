"""用 PyAV 生成 H.264，按板子的 7201 记录格式回放，再解回来。不调用系统 ffmpeg。"""

import asyncio
import io
import struct
import threading
from fractions import Fraction

import av
import pytest
from PIL import Image

from hidagent.video7201 import (
    CONFIG,
    START,
    H264Decoder,
    LatestFrameGate,
    RecordBuffer,
    hello_line,
    keepalive,
    pack_access_unit,
    run_tcp,
    stream_is_dead,
)

SIZE = 160
TOKEN = "0123456789abcdef"


def annexb_access_units(count: int = 4, gop: str = "1") -> list[bytes]:
    codec = av.CodecContext.create("libx264", "w")
    codec.width = SIZE
    codec.height = SIZE
    codec.pix_fmt = "yuv420p"
    codec.time_base = Fraction(1, 10)
    codec.framerate = 10
    codec.thread_count = 1  # one slice per picture, so split_aus yields complete access units
    codec.options = {"preset": "ultrafast", "tune": "zerolatency", "profile": "baseline", "g": gop,
                     "threads": "1", "slices": "1"}
    codec.open()
    packets: list[bytes] = []
    for index in range(count):
        image = Image.new("RGB", (SIZE, SIZE), ((index * 50) % 256, 40, 80))
        frame = av.VideoFrame.from_image(image)
        frame.pts = index
        for packet in codec.encode(frame):
            packets.append(bytes(packet))
    for packet in codec.encode(None):
        packets.append(bytes(packet))
    data = b"".join(packets)
    if b"\x00\x00\x00\x01" not in data and b"\x00\x00\x01" not in data:
        pytest.skip("this PyAV build did not emit Annex-B")
    return split_aus(data)


def split_aus(data: bytes) -> list[bytes]:
    """按 VCL NAL（type 1 或 5）把 Annex-B 切成访问单元，SPS/PPS 跟在下一个单元前面。"""
    starts = []
    index = 0
    while True:
        four = data.find(b"\x00\x00\x00\x01", index)
        three = data.find(b"\x00\x00\x01", index)
        if four < 0 and three < 0:
            break
        if three >= 0 and (four < 0 or three < four):
            starts.append(three)
            index = three + 3
        else:
            starts.append(four)
            index = four + 4
    starts.append(len(data))
    nals = [data[a:b] for a, b in zip(starts, starts[1:], strict=False)]
    units, current = [], b""
    for nal in nals:
        header = nal[4] if nal.startswith(b"\x00\x00\x00\x01") else nal[3]
        current += nal
        if (header & 0x1F) in (1, 5):
            units.append(current)
            current = b""
    return units


def framed_stream(units: list[bytes]) -> bytes:
    seq = 0
    out = bytearray()
    for index, unit in enumerate(units):
        chunk, seq = pack_access_unit(unit, seq, index * 200, idr=(index == 0))
        out += chunk
        if index == 1:
            out += keepalive()
    return bytes(out)


def test_last_access_unit_is_not_held_back():
    units = annexb_access_units(3)
    decoder = H264Decoder()
    frames = []
    for record in RecordBuffer().feed(framed_stream(units)):
        if record is None:
            continue
        frames.extend(decoder.push_chunk(record))
    assert len(frames) == len(units)
    assert frames[-1].width == SIZE and frames[-1].height == SIZE


def test_frame_gate_emits_the_newest_held_frame():
    gate = LatestFrameGate(1)
    assert gate.push(b"a", 0) == b"a"
    assert gate.push(b"b", 0.1) is None
    assert gate.push(b"c", 0.2) is None
    assert gate.flush(1.2) == b"c"


def test_record_buffer_and_decoder_round_trip():
    units = annexb_access_units()
    assert units
    decoder = H264Decoder()
    frames = []
    for record in RecordBuffer().feed(framed_stream(units)):
        if record is None:
            continue
        frames.extend(decoder.push_chunk(record))
    assert frames
    assert frames[0].width == SIZE and frames[0].height == SIZE


def test_tcp_client_decodes_a_fake_board():
    blob = framed_stream(annexb_access_units())

    async def scenario():
        async def handle(reader, writer):
            line = await reader.readline()
            if line != f"hello {TOKEN}\n".encode():
                writer.close()
                return
            writer.write(keepalive() + blob)
            await writer.drain()
            try:
                await asyncio.wait_for(reader.read(64), 2)
            except (asyncio.TimeoutError, ConnectionError):
                pass
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        jpegs: list[bytes] = []
        stop = threading.Event()

        def on_jpeg(data: bytes) -> None:
            jpegs.append(data)
            stop.set()

        try:
            await asyncio.wait_for(asyncio.to_thread(
                run_tcp, "127.0.0.1", port, TOKEN, on_jpeg, stop, max_fps=30), 15)
        finally:
            server.close()
            await server.wait_closed()
        assert jpegs
        image = Image.open(io.BytesIO(jpegs[0]))
        assert image.size == (SIZE, SIZE)
        assert image.format == "JPEG"

    asyncio.run(scenario())


def test_wrong_token_does_not_decode():
    async def scenario():
        async def handle(reader, writer):
            await reader.readline()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        stop = threading.Event()
        with pytest.raises(ConnectionError):
            await asyncio.to_thread(run_tcp, "127.0.0.1", port, "nope", lambda _blob: None, stop)
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())


def _nals(data: bytes) -> list[bytes]:
    starts = []
    index = 0
    while True:
        four = data.find(b"\x00\x00\x00\x01", index)
        three = data.find(b"\x00\x00\x01", index)
        if four < 0 and three < 0:
            break
        if three >= 0 and (four < 0 or three < four):
            starts.append(three)
            index = three + 3
        else:
            starts.append(four)
            index = four + 4
    starts.append(len(data))
    return [data[a:b] for a, b in zip(starts, starts[1:], strict=False) if b > a]


def _nal_type(nal: bytes) -> int:
    header = nal[4] if nal.startswith(b"\x00\x00\x00\x01") else nal[3]
    return header & 0x1F


def test_separate_config_au_then_idr_and_p_frames_decode():
    """固件把 SPS/PPS 单独打成 CONFIG，后面才是 IDR、P、P。CONFIG 不能被当成坏帧清掉解码器。"""
    units = annexb_access_units(4, gop="30")
    config = bytearray()
    pictures: list[bytes] = []
    for unit in units:
        pieces = _nals(unit)
        if not pictures:
            kept = bytearray()
            for nal in pieces:
                if _nal_type(nal) in (1, 5):
                    kept += nal
                else:
                    config += nal
            if kept:
                pictures.append(bytes(kept))
        else:
            pictures.append(unit)
    assert config and len(pictures) >= 3
    assert _nal_type(_nals(pictures[0])[-1]) == 5
    seq = 0
    blob = bytearray()
    chunk, seq = pack_access_unit(bytes(config), seq, 1000, config=True)
    blob += chunk
    chunk, seq = pack_access_unit(pictures[0], seq, 1001, idr=True)
    blob += chunk
    for index, picture in enumerate(pictures[1:], start=2):
        chunk, seq = pack_access_unit(picture, seq, 1000 + index)
        blob += chunk
    decoder = H264Decoder()
    frames = []
    saw_config = False
    for record in RecordBuffer().feed(bytes(blob)):
        if record is None:
            continue
        flags = record[2]
        got = decoder.push_chunk(record)
        if flags & CONFIG:
            saw_config = True
            assert got == []
            assert decoder._config
        frames.extend(got)
    assert saw_config
    assert len(frames) == len(pictures)


def test_keepalive_ack_is_not_a_frozen_screen():
    assert stream_is_dead(12.5, last_frame=0, last_ack=12.0) is False
    assert stream_is_dead(12.5, last_frame=0, last_ack=0) is True


@pytest.mark.parametrize("traffic", ["ack_only", "goes_silent", "no_hello_ack"])
def test_receiver_recovers_only_when_video_and_heartbeats_are_silent(monkeypatch, traffic):
    from hidagent import video7201

    clock = [0.0]
    stop = threading.Event()
    heartbeats, stalled = [], []

    class Socket:
        closed = False

        def setsockopt(self, *_args):
            pass

        def settimeout(self, _timeout):
            pass

        def sendall(self, _data):
            pass

        def recv(self, _size):
            clock[0] += 1
            if clock[0] == 20:
                stop.set()  # the test cannot hang if the receive deadline is broken
            if traffic == "ack_only" or (traffic == "goes_silent" and clock[0] == 1):
                return keepalive()
            raise TimeoutError

        def close(self):
            self.closed = True

    sock = Socket()
    monkeypatch.setattr(video7201.socket, "create_connection", lambda *_args, **_kwargs: sock)
    monkeypatch.setattr(video7201.time, "monotonic", lambda: clock[0])

    def receive():
        run_tcp("board", 7201, TOKEN, lambda _jpeg: pytest.fail("unexpected image"), stop,
                on_heartbeat=heartbeats.append, on_stall=lambda: stalled.append(clock[0]))

    if traffic == "ack_only":
        receive()
        assert not stalled and heartbeats == list(range(1, 21))
    else:
        with pytest.raises(video7201.StreamStalled):
            receive()
        assert len(stalled) == 1 and stalled[0] < 20
        assert heartbeats == ([1] if traffic == "goes_silent" else [])
    assert sock.closed


def test_first_connection_uses_plain_hello_with_a_fresh_decoder():
    """第一次连接没有可续传的时间戳，发普通 hello，等板子重发 CONFIG/IDR。"""
    assert hello_line(TOKEN) == f"hello {TOKEN}\n".encode()
    assert b" at " not in hello_line(TOKEN)

    async def scenario():
        seen: dict[str, bytes] = {}

        async def handle(reader, writer):
            seen["hello"] = await reader.readline()
            writer.write(keepalive())
            await writer.drain()
            try:
                await asyncio.wait_for(reader.read(64), 2)
            except (asyncio.TimeoutError, ConnectionError):
                pass
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        stop = threading.Event()

        def on_ready():
            stop.set()

        try:
            await asyncio.wait_for(asyncio.to_thread(
                run_tcp, "127.0.0.1", port, TOKEN, lambda _blob: None, stop, on_ready=on_ready), 10)
        finally:
            server.close()
            await server.wait_closed()
        assert seen["hello"] == f"hello {TOKEN}\n".encode()

    asyncio.run(scenario())


def test_partial_access_unit_does_not_publish_a_resume_timestamp():
    decoder = H264Decoder()
    chunk = struct.pack("<HBI", 1, START, 2000) + b"\x00\x00\x00\x01\x09"
    assert decoder.push_chunk(chunk) == []
    assert getattr(decoder, "last_ts", None) is None


def test_screen_reconnect_goes_on_from_the_last_decoded_access_unit(monkeypatch):
    """A reconnect keeps the decoder and says `hello <token> at <ts>`, so the board does not replay the whole GOP
    since the last IDR (26 minutes of frames on a still iPhone screen, never caught up between resets)."""
    from hidagent.screen import Screen

    screen = Screen("10.0.0.8", TOKEN)
    seen: list = []

    def fake(_host, _port, _token, _on_jpeg, stop, **kwargs):
        decoder = kwargs["decoder"]
        seen.append((decoder, decoder.last_ts))
        decoder.last_ts = 2000  # as if it decoded an AU stamped 2000
        stop.set()
        if len(seen) >= 2:
            screen._stopping = True

    monkeypatch.setattr("hidagent.screen.video7201.run_tcp", fake)
    monkeypatch.setattr("hidagent.screen.time.sleep", lambda _s: None)
    screen._supervise()
    assert seen[0][1] is None and seen[1] == (seen[0][0], 2000)
    assert hello_line(TOKEN, 2000) == f"hello {TOKEN} at 2000\n".encode()


def test_resume_timestamp_only_after_a_decoded_access_unit():
    decoder = H264Decoder()
    decoder.last_ts = 5
    decoder.reset()  # a decode error starts a new codec: nothing to go on from
    assert decoder.last_ts is None
    decoder.last_ts = 7
    decoder.reconnect()  # a new session keeps the codec and its place
    assert decoder.last_ts == 7


@pytest.mark.parametrize("disconnect", [b"", ConnectionResetError("connection reset")])
def test_disconnect_publishes_pending_frame_before_resuming_a_still_screen(monkeypatch, disconnect):
    """Real IDR/P frames from review_resume_7201.py: the last decoded AU must also reach the display."""
    from hidagent import video7201

    units = annexb_access_units(2, gop="100")
    assert len(units) == 2
    blob = keepalive()
    seq = 0
    for index, unit in enumerate(units):
        chunk, seq = pack_access_unit(unit, seq, (index + 1) * 100, idr=index == 0)
        blob += chunk

    clock = [0.0]  # both AUs arrive inside one rate-limit interval
    stop = threading.Event()

    class Socket:
        def __init__(self, payload, ending):
            self.payload, self.ending = payload, ending
            self.sent = []
            self.closed = False

        def setsockopt(self, *_args):
            pass

        def settimeout(self, _timeout):
            pass

        def sendall(self, data):
            self.sent.append(data)

        def recv(self, _size):
            if self.payload is not None:
                payload, self.payload = self.payload, None
                return payload
            if self.ending is None:
                clock[0] += 1
                stop.set()
                raise TimeoutError
            if isinstance(self.ending, Exception):
                raise self.ending
            return self.ending

        def close(self):
            self.closed = True

    first = Socket(blob, disconnect)
    # A still phone has no AU newer than 200 to send after the resume request.
    second = Socket(keepalive(), None)
    connections = iter([first, second])
    monkeypatch.setattr(video7201.socket, "create_connection", lambda *_args, **_kw: next(connections))
    monkeypatch.setattr(video7201.time, "monotonic", lambda: clock[0])
    decoder = H264Decoder()
    images, timestamps = [], []

    with pytest.raises(ConnectionError):
        run_tcp("board", 7201, TOKEN, images.append, stop, decoder=decoder, on_ts=timestamps.append)
    assert first.closed and decoder.last_ts == 200
    assert timestamps == [100, 200], "EOF/reset must publish the newest decoded frame, even below max_fps"
    assert Image.open(io.BytesIO(images[0])).tobytes() != Image.open(io.BytesIO(images[1])).tobytes()

    run_tcp("board", 7201, TOKEN, images.append, stop, decoder=decoder, on_ts=timestamps.append)
    assert second.closed and second.sent[0] == hello_line(TOKEN, 200)
    assert timestamps == [100, 200] and decoder.last_ts == 200


def test_real_p_frames_resume_with_the_same_reference_chain():
    """The retained codec resumes P frames pixel-identically and accepts a new IDR fallback."""
    units = annexb_access_units(4, gop="100")
    assert len(units) == 4

    def decode(decoder, unit, seq, timestamp, *, idr=False):
        blob, _next_seq = pack_access_unit(unit, seq, timestamp, idr=idr)
        return [frame for record in RecordBuffer().feed(blob) for frame in decoder.push_chunk(record)]

    reference = H264Decoder()
    expected = [frame for index, unit in enumerate(units)
                for frame in decode(reference, unit, index, (index + 1) * 100, idr=index == 0)]
    resumed = H264Decoder()
    actual = []
    for index, unit in enumerate(units):
        if index == 2:
            resumed.reconnect()
            assert resumed.last_ts == 200
        actual.extend(decode(resumed, unit, index if index < 2 else index + 21,
                             (index + 1) * 100, idr=index == 0))
    assert len(actual) == len(expected) == 4
    assert [frame.to_image().tobytes() for frame in actual] == [frame.to_image().tobytes() for frame in expected]
    resumed.reconnect()
    fallback = decode(resumed, units[0], 200, 500, idr=True)
    assert len(fallback) == 1 and fallback[0].to_image().tobytes() == expected[0].to_image().tobytes()


def _large_replay(monkeypatch, decode_cost):
    """3171 个 AU 落在一次 recv 内；用假成本量出转换工作量，不依赖 CI 机器速度。"""
    from hidagent import video7201

    count = 3171
    clock = [0.0]
    encoded, emitted, ka_times = [], [], []
    stop = threading.Event()
    blob = bytearray(keepalive())
    seq = 0
    for index in range(count):
        record, seq = pack_access_unit(b"\x00\x00\x01\x61x", seq, index)
        blob += record
    assert len(blob) <= 65536

    class Decoder:
        def __init__(self):
            self.index = 0

        def push_chunk(self, record):
            clock[0] += decode_cost
            frame = self.index
            self.index += 1
            return [frame]

    class Socket:
        batch_elapsed = None
        received = False
        closed = False

        def setsockopt(self, *_args):
            pass

        def settimeout(self, _timeout):
            pass

        def sendall(self, data):
            if data == b"ka\n":
                ka_times.append(clock[0])
            else:
                assert data == hello_line(TOKEN)

        def recv(self, _size):
            if not self.received:
                self.received = True
                return bytes(blob)
            self.batch_elapsed = clock[0]
            clock[0] += 0.5
            stop.set()
            raise TimeoutError

        def close(self):
            self.closed = True

    def encode(frame, quality):
        assert quality == 75
        encoded.append(frame)
        clock[0] += 0.0035
        return str(frame).encode()

    sock = Socket()
    monkeypatch.setattr(video7201.socket, "create_connection", lambda *_args, **_kw: sock)
    monkeypatch.setattr(video7201.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(video7201, "H264Decoder", Decoder)
    monkeypatch.setattr(video7201, "frame_to_jpeg", encode)
    run_tcp("fake-board", 7201, TOKEN, emitted.append, stop)
    assert sock.closed
    assert emitted[-1] == str(count - 1).encode(), "flush must output the newest held frame"
    return sock.batch_elapsed, encoded, emitted, ka_times


def test_large_replay_encodes_only_rate_limited_frames(monkeypatch):
    elapsed, encoded, emitted, _ka_times = _large_replay(monkeypatch, decode_cost=0.0001)
    assert elapsed < 0.5, f"3171-AU batch took {elapsed:.4f}s of simulated work"
    assert len(encoded) == len(emitted) <= 4


def test_large_replay_sends_keepalive_during_decode(monkeypatch):
    elapsed, _encoded, _emitted, ka_times = _large_replay(monkeypatch, decode_cost=0.001)
    assert len(ka_times) >= 3, "keepalive must run while the receive batch is decoding"
    points = [0.0, *ka_times, elapsed]
    assert max(b - a for a, b in zip(points, points[1:], strict=False)) < 1.01


def test_video_access_unit_drops_unbounded_frame_without_end(monkeypatch):
    import struct

    from hidagent import video7201

    monkeypatch.setattr(video7201, "MAX_AU_BYTES", 64)
    decoder = H264Decoder()
    # START chunk with 4-byte timestamp + 30 bytes payload, no END.
    assert decoder.push_chunk(struct.pack("<HB", 0, video7201.START) + b"\x00\x00\x00\x01" + b"a" * 30) == []
    assert decoder._cur is not None and len(decoder._cur) == 30
    # Continuation pushes past MAX_AU_BYTES -> drops partial AU cleanly instead of growing unboundedly.
    assert decoder.push_chunk(struct.pack("<HB", 1, 0) + b"b" * 40) == []
    assert decoder._cur is None
