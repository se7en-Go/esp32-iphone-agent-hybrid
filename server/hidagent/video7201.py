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
import logging
import socket
import struct
import threading
import time

import av

log = logging.getLogger(__name__)

START, END, IDR, CONFIG, AUDIO = 1, 2, 4, 8, 0x80
PAYLOAD = 1380
STREAM_TIMEOUT_S = 12.0
MAX_AU_BYTES = 8 * 1024 * 1024


class SequenceGap(Exception):
    def __init__(self, seq: int, expected: int):
        self.seq, self.expected = seq, expected
        super().__init__(f"video seq {seq} != {expected}")


class StreamStalled(ConnectionError):
    """The video connection stopped delivering both frames and heartbeat replies."""


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


def _parameter_sets_only(au: bytes) -> bool:
    """只有 SPS/PPS/SEI/AUD，没有 VCL 图像。"""
    saw_vcl = False
    index = 0
    while index < len(au):
        four = au.find(b"\x00\x00\x00\x01", index)
        three = au.find(b"\x00\x00\x01", index)
        if four < 0 and three < 0:
            break
        if three >= 0 and (four < 0 or three < four):
            start, mark = three, 3
        else:
            start, mark = four, 4
        header_at = start + mark
        if header_at >= len(au):
            break
        nal_type = au[header_at] & 0x1F
        if nal_type in (1, 5):
            saw_vcl = True
        index = header_at + 1
    return not saw_vcl and bool(au)


def stream_is_dead(now: float, last_frame: float, last_ack: float, limit: float = STREAM_TIMEOUT_S) -> bool:
    """静屏可以很久没有新帧，只要 keepalive 的应答还在，就不是断流。"""
    return now - last_frame > limit and now - last_ack > limit


def keepalive() -> bytes:
    return b"\x00\x00"


class H264Decoder:
    """按序号重组访问单元，并用 PyAV 解码。音频块有自己的序号，直接丢掉。"""

    def __init__(self) -> None:
        self._codec = None
        self._expect: int | None = None
        self._cur: bytearray | None = None
        self._config = b""
        self._ts = 0
        self.last_ts: int | None = None  # board millis() stamp of the last AU decoded (VF timestamps)
        self.reset()

    def reset(self) -> None:
        config = self._config
        self._codec = av.CodecContext.create("h264", "r")
        self._expect = None
        self._cur = None
        self._config = config
        self.last_ts = None  # a fresh codec is in step with nothing: the next session starts from the IDR

    def reconnect(self) -> None:
        """A new TCP session: its chunks are numbered afresh and a half-received AU is gone; the codec stays."""
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
            self._ts = int.from_bytes(payload[:4], "little") if len(payload) >= 4 else 0
            self._cur = bytearray(payload[4:] if len(payload) >= 4 else b"")
        elif self._cur is not None:
            if len(self._cur) + len(payload) > MAX_AU_BYTES:
                log.warning("video access unit exceeded %d bytes without END; dropping partial frame", MAX_AU_BYTES)
                self._cur = None
                return []
            self._cur += payload
        else:
            return []
        if not (flags & END) or self._cur is None:
            return []
        au = bytes(self._cur)
        self._cur = None
        # 固件把 SPS/PPS 单独打成 CONFIG，不是一张图。解失败也不能 reset，否则后面的 IDR 没有参数集。
        if flags & CONFIG or _parameter_sets_only(au):
            if au:
                self._config = au
            return []
        if (flags & IDR) and self._config:
            au = self._config + au
        try:
            # 完整访问单元直接当一个 packet。parse() 会扣住最后一帧，静止画面就一直不出来。
            frames = list(self._codec.decode(av.Packet(au)))
            self.last_ts = self._ts
            return frames
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

    def flush(self, now: float, *, force: bool = False):
        if self.pending is None:
            return None
        if not force and self.gap and self.last is not None and now - self.last < self.gap:
            return None
        ready = self.pending
        self.pending = None
        self.last = now
        return ready

    def wait_timeout(self, now: float, maximum: float = 0.5) -> float:
        """Wake a quiet receiver when its pending frame can be published."""
        if self.pending is None or self.last is None:
            return maximum
        return max(0.001, min(maximum, self.last + self.gap - now))


def frame_to_jpeg(frame, quality: int = 75) -> bytes:
    image = frame.to_image()
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def hello_line(token: str, resume_ts: int | None = None) -> bytes:
    """新解码器发普通 hello，板子从 CONFIG/IDR 重新给。

    同一个解码器断线重连时带上 `at <ts>`（它已经解到的最后一个访问单元）：板子接着往下发，
    不再把上一个 IDR 以来的整段 GOP 重放一遍。静屏时 iOS 很久才给一个 IDR，那一段能有几十分钟，
    重放要几十秒，期间板子还会断开重连，于是永远追不上（iPhone 15 Pro Max，2026-11-12）。
    """
    if resume_ts is None:
        return f"hello {token}\n".encode("ascii")
    return f"hello {token} at {int(resume_ts) & 0xFFFFFFFF}\n".encode("ascii")


def run_tcp(host: str, port: int, token: str, on_jpeg, stop: threading.Event, *,
            quality: int = 75, max_fps: int = 8, connect_timeout: float = 5,
            on_ready=None, on_stall=None, sockets: list | None = None, on_ts=None,
            decoder: H264Decoder | None = None, on_heartbeat=None) -> None:
    """阻塞读取一路 7201，直到 stop 或对端断开。on_jpeg(jpeg_bytes) 在解码线程里调用。

    同一板子/token 重连时保留解码器，以 hello <token> at <ts> 从已解码的最后一个 AU 续传；
    新解码器发普通 hello。静屏时板子可能不再推新帧，但 hello/ka 的零长度应答还在。
    on_heartbeat 收到的是应答到达的单调时钟时间，不代表有新帧。
    帧和应答都停约 12 秒，或迟迟没有握手应答，调用 on_stall 后退出，让上层重连。
    序号断了就发 resync，让板子从 IDR 重来。
    on_ts(board_ms) 在每个 on_jpeg 之前调用：这张图对应的访问单元到达板子时的 millis()。
    普通 hello 或续传时间戳失效时，板子可能重放上一个 IDR 以来的 GOP（参考链完整时）；
    这段时间里的图都是旧的；拿它和板子当前的 millis() 比，就知道画面落后多少。
    """
    sock = socket.create_connection((host, int(port)), timeout=connect_timeout)
    if sockets is not None:
        sockets.append(sock)
    gate = LatestFrameGate(max_fps)

    def emit(ready) -> None:
        frame, ts = ready
        if on_ts is not None:
            on_ts(ts)
        on_jpeg(frame_to_jpeg(frame, quality))

    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(0.5)
        if decoder is None:
            decoder = H264Decoder()
        else:
            decoder.reconnect()
        sock.sendall(hello_line(token, getattr(decoder, "last_ts", None)))
        records = RecordBuffer()
        authed = False
        last_ka = time.monotonic()
        last_frame = time.monotonic()
        last_ack = time.monotonic()

        def send_keepalive(now: float) -> None:
            nonlocal last_ka
            if authed and now - last_ka >= 1:
                sock.sendall(b"ka\n")
                last_ka = now

        while not stop.is_set():
            now = time.monotonic()
            ready = gate.flush(now)
            if ready is not None:
                emit(ready)
                if stop.is_set():
                    break
                now = time.monotonic()
            send_keepalive(now)
            if stream_is_dead(now, last_frame, last_ack):
                if on_stall is not None:
                    on_stall()
                raise StreamStalled("7201 did not deliver video or heartbeat replies")
            try:
                sock.settimeout(gate.wait_timeout(time.monotonic()))
                data = sock.recv(65536)
            except TimeoutError:
                ready = gate.flush(time.monotonic())
                if ready is not None:
                    emit(ready)
                continue
            if not data:
                raise ConnectionError("7201 closed")
            for record in records.feed(data):
                # 一个 recv 可能包含整个长 GOP；解码期间也必须保持一秒一次的心跳。
                send_keepalive(time.monotonic())
                if record is None:
                    last_ack = time.monotonic()
                    if not authed:
                        authed = True
                        if on_ready:
                            on_ready()
                    if on_heartbeat is not None:
                        on_heartbeat(last_ack)
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
                # 只保留最新的解码帧，真正输出时才做昂贵的 JPEG 转换。
                ready = gate.push((frames[-1], getattr(decoder, "last_ts", None)), last_frame)
                if ready is not None:
                    emit(ready)
    finally:
        try:
            sock.close()
        except OSError:
            pass
        if sockets is not None and sock in sockets:
            sockets.remove(sock)
        # last_ts already includes this decoded AU; a resumed still screen will never send it again.
        # Publish the pending picture on EOF/reset/stop even if the frame-rate interval has not elapsed.
        ready = gate.flush(time.monotonic(), force=True)
        if ready is not None:
            emit(ready)
