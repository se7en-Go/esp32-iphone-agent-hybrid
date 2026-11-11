"""用 PyAV 生成 H.264，按板子的 7201 记录格式回放，再解回来。不调用系统 ffmpeg。"""

import asyncio
import io
import threading
from fractions import Fraction

import av
import pytest
from PIL import Image

from hidagent.video7201 import H264Decoder, LatestFrameGate, RecordBuffer, keepalive, pack_access_unit, run_tcp

SIZE = 160
TOKEN = "0123456789abcdef"


def annexb_access_units(count: int = 4) -> list[bytes]:
    codec = av.CodecContext.create("libx264", "w")
    codec.width = SIZE
    codec.height = SIZE
    codec.pix_fmt = "yuv420p"
    codec.time_base = Fraction(1, 10)
    codec.framerate = 10
    codec.options = {"preset": "ultrafast", "tune": "zerolatency", "profile": "baseline", "g": "1"}
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
