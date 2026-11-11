"""板子 TCP 7201 的记录层和 H.264 重组。

分帧以固件为准（wifi_video_tcp.cpp、src/video/gop.c 的 vchunk_next）：

- 记录：[u16 LE 长度][chunk]。长度为 0 是 hello / ka 的应答。
- chunk：[seq u16 LE][flags u8][payload]
- flags：bit0 START、bit1 END、bit2 IDR、bit3 CONFIG、bit7 音频
- START 的 payload 前 4 字节是板子时间戳（u32 LE），后面才是 Annex-B

电脑端代码不链接 GPL 的 airplay 源码，只按这个线上格式解码。
"""

from __future__ import annotations

import io
import socket
import struct
import threading
import time

import av

START, END, AUDIO = 1, 2, 0x80
PAYLOAD = 1380


class SequenceGap(Exception):
    def __init__(self, seq: int, expected: int):
        self.seq, self.expected = seq, expected
        super().__init__(f"video seq {seq} != {expected}")


class RecordBuffer:
    """把 TCP 字节流切成记录。None 表示长度为 0 的心跳记录。"""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes | None]:
        self._buf.extend(data)
        out: list[bytes | None] = []
        while len(self._buf) >= 2:
            size = int.from_bytes(self._buf[:2], "little")
            if len(self._buf) < 2 + size:
                break
            chunk = bytes(self._buf[2:2 + size])
            del self._buf[:2 + size]
            out.append(chunk if size else None)
        return out


def pack_access_unit(au: bytes, seq: int, timestamp: int, *, idr: bool = False,
                     config: bool = False, payload: int = PAYLOAD) -> tuple[bytes, int]:
    """把一个 Annex-B 访问单元打成与固件 vchunk_next 相同的记录。返回 (bytes, next_seq)。"""
    flags_base = (4 if idr else 0) | (8 if config else 0)
    remaining = au
    first = True
    out = bytearray()
    while first or remaining:
        room = payload
        body = b""
        flags = flags_base
        if first:
            flags |= START
            body = struct.pack("<I", timestamp & 0xFFFFFFFF)
            room -= 4
            first = False
        take = remaining[: max(room, 0)]
        remaining = remaining[len(take):]
        body += take
        if not remaining:
            flags |= END
        chunk = struct.pack("<HB", seq & 0xFFFF, flags) + body
        out += struct.pack("<H", len(chunk)) + chunk
        seq = (seq + 1) & 0xFFFF
        if flags & END:
            break
    return bytes(out), seq


def keepalive() -> bytes:
    return b"\x00\x00"


class H264Decoder:
    """按序号重组访问单元，并用 PyAV 解码。音频块有自己的序号，直接丢掉。"""

    def __init__(self) -> None:
        self._codec = None
        self._expect: int | None = None
        self._cur: bytearray | None = None
        self.reset()

    def reset(self) -> None:
        self._codec = av.CodecContext.create("h264", "r")
        self._expect = None
        self._cur = None

    def push_chunk(self, chunk: bytes) -> list:
        if len(chunk) < 3:
            return []
        seq, flags = struct.unpack_from("<HB", chunk)
        if flags & AUDIO:
            return []
        if self._expect is not None and seq != self._expect:
            expected = self._expect
            self._cur = None
            self._expect = None
            raise SequenceGap(seq, expected)
        self._expect = (seq + 1) & 0xFFFF
        payload = chunk[3:]
        if flags & START:
            self._cur = bytearray(payload[4:] if len(payload) >= 4 else b"")
        elif self._cur is not None:
            self._cur += payload
        else:
            return []
        if not (flags & END) or self._cur is None:
            return []
        au = bytes(self._cur)
        self._cur = None
        try:
            # 完整访问单元直接当一个 packet。parse() 会扣住最后一帧，静止画面就一直不出来。
            return list(self._codec.decode(av.Packet(au)))
        except av.error.FFmpegError:
            self.reset()
            return []


class LatestFrameGate:
    """帧率上限留最新的一帧，不把还没发出去的新帧丢掉。"""

    def __init__(self, max_fps: int):
        self.gap = 1 / max_fps if max_fps else 0
        self.last = None
        self.pending = None

    def push(self, frame, now: float):
        self.pending = frame
        return self.flush(now)

    def flush(self, now: float):
        if self.pending is None:
            return None
        if self.gap and self.last is not None and now - self.last < self.gap:
            return None
        ready = self.pending
        self.pending = None
        self.last = now
        return ready


def frame_to_jpeg(frame, quality: int = 75) -> bytes:
    image = frame.to_image()
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def run_tcp(host: str, port: int, token: str, on_jpeg, stop: threading.Event, *,
            quality: int = 75, max_fps: int = 8, connect_timeout: float = 5,
            on_ready=None, on_stall=None, sockets: list | None = None) -> None:
    """阻塞读取一路 7201，直到 stop 或对端断开。on_jpeg(jpeg_bytes) 在解码线程里调用。

    板子在画面冻住时可能不再推新帧，解码器这边也没有更新。超过约 12 秒没有新帧就调用
    on_stall，让界面提示重新选一次屏幕镜像。
    """
    sock = socket.create_connection((host, int(port)), timeout=connect_timeout)
    if sockets is not None:
        sockets.append(sock)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(0.5)
        sock.sendall(f"hello {token}\n".encode("ascii"))
        decoder = H264Decoder()
        records = RecordBuffer()
        gate = LatestFrameGate(max_fps)
        authed = False
        stalled = False
        last_ka = time.monotonic()
        last_frame = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            if authed and now - last_ka >= 1:
                sock.sendall(b"ka\n")
                last_ka = now
            if authed and not stalled and now - last_frame > 12 and on_stall:
                stalled = True
                on_stall()
            try:
                data = sock.recv(65536)
            except TimeoutError:
                ready = gate.flush(time.monotonic())
                if ready is not None:
                    on_jpeg(ready)
                continue
            if not data:
                raise ConnectionError("7201 closed")
            for record in records.feed(data):
                if record is None:
                    if not authed:
                        authed = True
                        if on_ready:
                            on_ready()
                    continue
                if not authed:
                    continue
                try:
                    frames = decoder.push_chunk(record)
                except SequenceGap:
                    decoder.reset()
                    try:
                        sock.sendall(b"resync\n")
                    except OSError:
                        raise
                    continue
                if not frames:
                    continue
                last_frame = time.monotonic()
                stalled = False
                ready = gate.push(frame_to_jpeg(frames[-1], quality), last_frame)
                if ready is not None:
                    on_jpeg(ready)
    finally:
        try:
            sock.close()
        except OSError:
            pass
        if sockets is not None and sock in sockets:
            sockets.remove(sock)
